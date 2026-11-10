#!/usr/bin/env python3
"""
Preenche dados históricos de publicacoes e stories no metricas.json.

Modos de uso:

  1) Atualizar resumos trimestrais (rápido – 1 chamada de API por trimestre):
     python scripts/backfill_metrics.py --mode conteudo --start-year 2024 --end-year 2026

  2) Preencher séries diárias (1 chamada por dia – use para intervalos curtos):
     python scripts/backfill_metrics.py --mode series --start 2026-07-01 --end 2026-11-07

  3) Ambos ao mesmo tempo:
     python scripts/backfill_metrics.py --mode ambos --start-year 2024 --end-year 2026

Variáveis de ambiente necessárias:
  FB_PAGE_ACCESS_TOKEN  -- Token permanente da página
  FB_PAGE_ID            -- ID da página (padrão: 447748898661292)
"""

import os
import json
import sys
import argparse
import requests
import time
from datetime import date, timedelta

API_VERSION = "v21.0"
BASE_URL    = f"https://graph.facebook.com/{API_VERSION}"

PAGE_ID = os.environ.get("FB_PAGE_ID", "447748898661292")
TOKEN   = os.environ["FB_PAGE_ACCESS_TOKEN"]

TRI_LABELS   = {1: "1º tri", 2: "2º tri", 3: "3º tri", 4: "4º tri"}
TRI_MONTHS   = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}


# ───────────────────────── utilidades ──────────────────────────

def api_get(url, params, _retries=5):
    """Faz GET na API com retry automático em caso de rate limit (403 transitório)."""
    for attempt in range(_retries):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 403:
            try:
                err = r.json().get("error", {})
            except Exception:
                err = {}
            if err.get("is_transient") or err.get("code") == 4:
                wait = 60 * (attempt + 1)
                print(f"  Rate limit atingido – aguardando {wait}s (tentativa {attempt+1}/{_retries})...", file=sys.stderr)
                time.sleep(wait)
                continue
        if not r.ok:
            try:
                err = r.json()
                print(f"  Erro Meta API {r.status_code}: {err}", file=sys.stderr)
            except Exception:
                print(f"  Erro Meta API {r.status_code}: {r.text[:400]}", file=sys.stderr)
            r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"API error: {data['error']}")
        return data
    raise RuntimeError(f"Rate limit persistente após {_retries} tentativas.")


def count_paged(url, params):
    """Conta todos os itens seguindo paginação com pausa entre páginas."""
    total = 0
    page = 0
    while url:
        data = api_get(url, params)
        items = data.get("data", [])
        total += len(items)
        url = data.get("paging", {}).get("next")
        params = {}
        page += 1
        if url:
            time.sleep(1)  # pausa entre páginas para não estourar rate limit
    return total


def quarter_of(day_str):
    return (int(day_str[5:7]) - 1) // 3 + 1


def quarter_range(year, q):
    """Retorna (since, until) para chamar a API – since=primeiro dia, until=primeiro dia do próximo tri."""
    m1, m3 = TRI_MONTHS[q]
    last_day = date(year, m3, 1)
    # último dia do mês m3
    if m3 == 12:
        last_day = date(year, 12, 31)
    else:
        last_day = date(year, m3 + 1, 1) - timedelta(days=1)
    today = date.today()
    if last_day > today:
        last_day = today
    since = date(year, m1, 1).isoformat()
    until = (last_day + timedelta(days=1)).isoformat()   # API usa until exclusivo
    return since, until, last_day.isoformat()


# ───────────────────────── Instagram ───────────────────────────

def get_ig_user_id():
    data = api_get(
        f"{BASE_URL}/{PAGE_ID}",
        params={"fields": "instagram_business_account", "access_token": TOKEN},
    )
    ig = data.get("instagram_business_account")
    if not ig:
        raise RuntimeError("Página sem conta Instagram Business vinculada.")
    return ig["id"]


# ───────────────────────── MODO CONTEUDO ───────────────────────

def fetch_quarter_counts(year, q, ig_user_id=None):
    """Conta publicações e stories no trimestre via API (uma chamada por plataforma)."""
    since, until, _ = quarter_range(year, q)
    result = {}

    # Facebook feed posts
    try:
        fb_feed = count_paged(
            f"{BASE_URL}/{PAGE_ID}/published_posts",
            {"since": since, "until": until, "access_token": TOKEN, "fields": "id"},
        )
        result["facebook"] = {"feed": fb_feed}
        print(f"    FB feed:    {fb_feed}")
    except Exception as e:
        print(f"    Aviso FB feed: {e}", file=sys.stderr)
        result["facebook"] = {"feed": 0}

    # Facebook stories
    try:
        fb_stories = count_paged(
            f"{BASE_URL}/{PAGE_ID}/stories",
            {"since": since, "until": until, "access_token": TOKEN, "fields": "id"},
        )
        result["facebook"]["stories"] = fb_stories
        print(f"    FB stories: {fb_stories}")
    except Exception as e:
        print(f"    Aviso FB stories: {e}", file=sys.stderr)
        result["facebook"]["stories"] = 0

    # Instagram
    if ig_user_id:
        try:
            media = api_get(
                f"{BASE_URL}/{ig_user_id}/media",
                params={
                    "fields":       "media_type,timestamp",
                    "since":        since,
                    "until":        until,
                    "access_token": TOKEN,
                },
            )
            ig_feed = ig_stories = 0
            for item in media.get("data", []):
                if item.get("media_type") == "STORY":
                    ig_stories += 1
                else:
                    ig_feed += 1
            result["instagram"] = {"feed": ig_feed, "stories": ig_stories}
            print(f"    IG feed:    {ig_feed}")
            print(f"    IG stories: {ig_stories}")
        except Exception as e:
            print(f"    Aviso IG: {e}", file=sys.stderr)
            result["instagram"] = {"feed": 0, "stories": 0}

    return result


def run_conteudo(data, start_year, end_year, ig_user_id=None):
    """Atualiza data['conteudo'] com contagens reais de publicações por trimestre."""
    conteudo = data.setdefault("conteudo", {})
    today = date.today()

    for year in range(start_year, end_year + 1):
        for q in range(1, 5):
            since, until, last_day_str = quarter_range(year, q)
            if date.fromisoformat(since) > today:
                break  # trimestre no futuro

            print(f"\n  {q}º tri {year}  ({since} → {last_day_str}):")
            counts = fetch_quarter_counts(year, q, ig_user_id)

            str_year = str(year)
            conteudo.setdefault(str_year, {})

            for plat, vals in counts.items():
                plat_list = conteudo[str_year].setdefault(plat, [None, None, None, None])
                while len(plat_list) < 4:
                    plat_list.append(None)

                existing = plat_list[q - 1]
                if existing is None:
                    # Cria entrada zerada — equipe pode preencher alcance/reações manualmente
                    plat_list[q - 1] = {
                        "t":                 TRI_LABELS[q],
                        "feed":              vals.get("feed", 0),
                        "stories":           vals.get("stories", 0),
                        "alcance":           0,
                        "alcanceStories":    0,
                        "reacoes":           0,
                        "comentarios":       0,
                        "compartilhamentos": 0,
                    }
                else:
                    # Atualiza só feed/stories; preserva alcance, reações etc.
                    existing["feed"]    = vals.get("feed", existing.get("feed", 0))
                    existing["stories"] = vals.get("stories", existing.get("stories", 0))


# ───────────────────────── MODO SERIES ─────────────────────────

FB_INSIGHT_METRICS = {
    "visualizadores": "page_total_media_view_unique",
    "visualizacoes":  "page_media_view",
    "interacoes":     "page_post_engagements",
    "cliques":        "page_total_actions",
    "seguidores":     "page_daily_follows_unique",
    "visitas":        "page_views_total",
}

IG_DIRECT_METRICS = {
    "visualizacoes": "impressions",
    "alcance":       "reach",
    "visitas":       "profile_views",
    "cliques":       "website_clicks",
    "seguidores":    "follower_count",
}


def fetch_day_fb(day):
    since = day
    until = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    result = {}

    for json_key, metric_name in FB_INSIGHT_METRICS.items():
        try:
            data = api_get(
                f"{BASE_URL}/{PAGE_ID}/insights/{metric_name}/day",
                params={"since": since, "until": until, "access_token": TOKEN},
            )
            values = (data.get("data") or [{}])[0].get("values", [])
            value = 0
            for entry in values:
                if entry.get("end_time", "")[:10] == day:
                    value = entry["value"]
                    break
            else:
                value = values[-1]["value"] if values else 0
            result[json_key] = value
        except Exception as e:
            print(f"    Aviso FB {json_key}: {e}", file=sys.stderr)

    # Publicações
    try:
        result["publicacoes"] = count_paged(
            f"{BASE_URL}/{PAGE_ID}/published_posts",
            {"since": since, "until": until, "access_token": TOKEN, "fields": "id"},
        )
    except Exception as e:
        print(f"    Aviso FB publicacoes: {e}", file=sys.stderr)
        result["publicacoes"] = 0

    # Stories
    try:
        result["stories"] = count_paged(
            f"{BASE_URL}/{PAGE_ID}/stories",
            {"since": since, "until": until, "access_token": TOKEN, "fields": "id"},
        )
    except Exception as e:
        print(f"    Aviso FB stories: {e}", file=sys.stderr)
        result["stories"] = 0

    return result


def fetch_day_ig(ig_user_id, day):
    since = day
    until = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    result = {}

    try:
        data = api_get(
            f"{BASE_URL}/{ig_user_id}/insights",
            params={
                "metric":  ",".join(IG_DIRECT_METRICS.values()),
                "period":  "day",
                "since":   since,
                "until":   until,
                "access_token": TOKEN,
            },
        )
        for entry in data.get("data", []):
            json_key = next((k for k, v in IG_DIRECT_METRICS.items() if v == entry["name"]), None)
            if json_key:
                values = entry.get("values", [])
                value = 0
                for v in values:
                    if v.get("end_time", "")[:10] == day:
                        value = v["value"]
                        break
                else:
                    value = values[-1]["value"] if values else 0
                result[json_key] = value
    except Exception as e:
        print(f"    Aviso IG insights: {e}", file=sys.stderr)

    try:
        media = api_get(
            f"{BASE_URL}/{ig_user_id}/media",
            params={"fields": "media_type,timestamp", "since": since, "until": until, "access_token": TOKEN},
        )
        feed_count = stories_count = 0
        for item in media.get("data", []):
            if item.get("media_type") == "STORY":
                stories_count += 1
            else:
                feed_count += 1
        result["publicacoes"] = feed_count
        result["stories"]     = stories_count
    except Exception as e:
        print(f"    Aviso IG media: {e}", file=sys.stderr)
        result.setdefault("publicacoes", 0)
        result.setdefault("stories", 0)

    return result


def run_series(data, start_date, end_date, ig_user_id=None, only_pub_stories=False):
    """Preenche dados diários em data['series']."""
    cur = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    today = date.today()
    if end >= today:
        end = today - timedelta(days=1)

    while cur <= end:
        day = cur.isoformat()
        print(f"\n  {day}")

        fb = fetch_day_fb(day)
        for key, value in fb.items():
            if only_pub_stories and key not in ("publicacoes", "stories"):
                cur += timedelta(days=1)
                continue
            data["series"]["facebook"].setdefault(key, {})[day] = value

        if ig_user_id:
            ig = fetch_day_ig(ig_user_id, day)
            for key, value in ig.items():
                if only_pub_stories and key not in ("publicacoes", "stories"):
                    continue
                data["series"]["instagram"].setdefault(key, {})[day] = value

        cur += timedelta(days=1)


# ───────────────────────── MAIN ─────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backfill de métricas históricas")
    parser.add_argument("--mode", choices=["conteudo", "series", "ambos"],
                        default="conteudo",
                        help="conteudo=resumo trimestral (rápido) | series=séries diárias | ambos=os dois")

    # Parâmetros para modo conteudo / ambos
    parser.add_argument("--start-year", type=int, default=date.today().year - 1,
                        help="Ano inicial para modo conteudo (padrão: ano passado)")
    parser.add_argument("--end-year",   type=int, default=date.today().year,
                        help="Ano final para modo conteudo (padrão: ano corrente)")

    # Parâmetros para modo series / ambos
    parser.add_argument("--start", help="Data inicial para modo series (YYYY-MM-DD)")
    parser.add_argument("--end",   help="Data final para modo series (YYYY-MM-DD)")
    parser.add_argument("--only-pub-stories", action="store_true",
                        help="No modo series, preencher só publicacoes e stories (mais rápido)")

    parser.add_argument("--skip-instagram", action="store_true",
                        help="Pular dados do Instagram")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostrar o que seria feito sem salvar o arquivo")

    args = parser.parse_args()

    path = "dados/metricas.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Obter ID do Instagram
    ig_user_id = None
    if not args.skip_instagram:
        try:
            ig_user_id = get_ig_user_id()
            print(f"Instagram Business Account: {ig_user_id}")
        except Exception as e:
            print(f"Aviso: Instagram não disponível – {e}", file=sys.stderr)
            print("  (use --skip-instagram para suprimir este aviso)")

    # Executar modos
    if args.mode in ("conteudo", "ambos"):
        print(f"\n=== MODO CONTEUDO ({args.start_year} → {args.end_year}) ===")
        run_conteudo(data, args.start_year, args.end_year, ig_user_id)

    if args.mode in ("series", "ambos"):
        if not args.start or not args.end:
            parser.error("--start e --end são obrigatórios para modo series/ambos")
        print(f"\n=== MODO SERIES ({args.start} → {args.end}) ===")
        run_series(data, args.start, args.end, ig_user_id, args.only_pub_stories)

    # Salvar
    if not args.dry_run:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        print(f"\nmetricas.json atualizado!")
    else:
        print("\n[dry-run] Arquivo NÃO salvo.")
        # Mostra o que ficaria no conteudo
        print("\nConteudo resultante:")
        print(json.dumps(data.get("conteudo", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
