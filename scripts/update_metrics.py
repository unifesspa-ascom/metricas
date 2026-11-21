#!/usr/bin/env python3
"""
Candidato de observacao de conta Facebook e Instagram via Meta Graph API.
Gravacao de dados/metricas.json suspensa; --dry-run legado ainda faz consultas.

Variaveis de ambiente necessarias:
  FB_PAGE_ACCESS_TOKEN  -- Token de acesso permanente da pagina
  FB_PAGE_ID            -- ID da pagina do Facebook (padrao: 447748898661292)
"""

import argparse
import os
import json
import sys
import requests
from facebook_account_metrics import Client as FacebookClient, METRICS as FB_METRICS, collect_series, require_publication_disabled
from datetime import date, timedelta, datetime, timezone

API_VERSION = "v21.0"
BASE_URL    = f"https://graph.facebook.com/{API_VERSION}"

PAGE_ID = os.environ.get("FB_PAGE_ID", "447748898661292")
TOKEN   = os.environ["FB_PAGE_ACCESS_TOKEN"]

# Horário de Brasília (UTC-3) — corrige atribuição de datas em posts noturnos
# (ex: post às 21h BRT = 00h UTC do dia seguinte → seria contado no dia errado)
BRT = timezone(timedelta(hours=-3))
now_brt   = datetime.now(BRT)
yesterday = (now_brt - timedelta(days=1)).date().isoformat()
since     = yesterday
until_    = now_brt.date().isoformat()

# --- Facebook Page Insights ---
# Facebook metric names come from the shared contract; none is a fallback.

# No fallback metrics: availability and semantic equivalence require separate review.

# --- Instagram User Insights ---
# Métricas com série diária nativa (period=day retorna values[{end_time, value}]).
# impressions, profile_views, website_clicks e follower_count foram removidas:
# impressions e follower_count não aceitam period=day em v21;
# profile_views foi renomeada/instabilizada em v20+;
# website_clicks foi substituída por profile_links_taps (ver IG_TOTAL_VALUE_METRICS).
IG_DIRECT_METRICS = {
    "alcance": "reach",
}

# Métricas que exigem metric_type=total_value em v21 — retornam um inteiro
# por janela solicitada. Para granularidade diária, chamamos com since=D, until=D+1.
# ATENÇÃO: "cliques" representa profile_links_taps (toques no link da bio).
# Dados históricos de "cliques" anteriores a esta mudança podem representar
# website_clicks — métrica diferente; não compare os dois períodos diretamente.
IG_TOTAL_VALUE_METRICS = {
    "interacoes": "accounts_engaged",
    "cliques":    "profile_links_taps",
}


def api_get(url, params):
    r = requests.get(url, params=params, timeout=30)
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


def extract_day_value(metric_data, target_day):
    for entry in metric_data.get("values", []):
        if entry["end_time"][:10] == target_day:
            return entry["value"]
    values = metric_data.get("values", [])
    return values[-1]["value"] if values else 0


def count_paged_results(url, params):
    """Conta todos os itens retornados (seguindo paginacao)."""
    total = 0
    while url:
        data = api_get(url, params)
        total += len(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = {}  # proximas paginas ja tem os params na URL
    return total


# ===== FACEBOOK =====

def fetch_facebook_metrics(*, day_zone=None):
    """Insights only; incomplete/invalid/missing values are never replaced with zero.

    No default day policy is approved. This candidate cannot enable publication.
    Account post/story counts are left to the separate production automations.
    """
    client = FacebookClient(TOKEN)
    series, statuses = collect_series(client, PAGE_ID, since, until_, day_zone=day_zone)
    for key, status in statuses.items():
        print(f"  FB {key}: {status}")
    return {key: days[yesterday] for key, days in series.items() if yesterday in days}


# ===== INSTAGRAM =====

def get_ig_user_id():
    data = api_get(
        f"{BASE_URL}/{PAGE_ID}",
        params={"fields": "instagram_business_account", "access_token": TOKEN},
    )
    ig = data.get("instagram_business_account")
    if not ig:
        raise RuntimeError("Pagina sem conta Instagram Business vinculada.")
    return ig["id"]


def fetch_ig_insights_reach(ig_user_id):
    """Coleta reach (série diária nativa) para ontem."""
    return api_get(
        f"{BASE_URL}/{ig_user_id}/insights",
        params={
            "metric": "reach",
            "period": "day",
            "since":  since,
            "until":  until_,
            "access_token": TOKEN,
        },
    )


def fetch_ig_total_value(ig_user_id, metric_name, day_since, day_until):
    """
    Coleta métrica que exige metric_type=total_value para uma janela de exatamente
    um dia (since=D, until=D+1). Retorna o inteiro ou None se indisponível.
    Nunca retorna 0 por falha — somente retorna 0 se a API explicitamente informar 0.
    """
    data = api_get(
        f"{BASE_URL}/{ig_user_id}/insights",
        params={
            "metric":      metric_name,
            "period":      "day",
            "metric_type": "total_value",
            "since":       day_since,
            "until":       day_until,
            "access_token": TOKEN,
        },
    )
    entries = data.get("data", [])
    if not entries:
        return None
    tv = entries[0].get("total_value", {})
    return tv.get("value")  # None se "value" ausente


def fetch_instagram_metrics():
    ig_user_id = get_ig_user_id()
    result     = {}

    # --- reach: série diária nativa ---
    try:
        data = fetch_ig_insights_reach(ig_user_id)
        for entry in data.get("data", []):
            if entry.get("name") == "reach":
                value = extract_day_value(entry, yesterday)
                result["alcance"] = value
                print(f"  IG alcance (reach): {value}")
    except Exception as e:
        print(f"  AVISO IG alcance (reach): {e}", file=sys.stderr)

    # --- accounts_engaged e profile_links_taps: total_value, janela de 1 dia ---
    for json_key, metric_name in IG_TOTAL_VALUE_METRICS.items():
        try:
            value = fetch_ig_total_value(ig_user_id, metric_name, since, until_)
            if value is not None:
                result[json_key] = value
                print(f"  IG {json_key} ({metric_name}): {value}")
            else:
                print(f"  AVISO IG {json_key} ({metric_name}): resposta vazia, dia não gravado",
                      file=sys.stderr)
        except Exception as e:
            print(f"  AVISO IG {json_key} ({metric_name}): {e}", file=sys.stderr)
            # Não grava — falha não vira zero

    # Contagem de publicacoes e stories via /media
    try:
        media = api_get(
            f"{BASE_URL}/{ig_user_id}/media",
            params={
                "fields":       "media_type,timestamp",
                "since":        since,
                "until":        until_,
                "access_token": TOKEN,
            },
        )
        feed_count    = 0
        stories_count = 0
        story_ids     = []
        for item in media.get("data", []):
            if item.get("media_type") == "STORY":
                stories_count += 1
                story_ids.append(item["id"])
            else:
                feed_count += 1

        result["publicacoes"] = feed_count
        result["stories"]     = stories_count
        print(f"  IG publicacoes (feed): {feed_count}")
        print(f"  IG stories: {stories_count}")

        # Visualizacoes de stories (soma das impressoes individuais)
        story_views = 0
        for story_id in story_ids:
            try:
                ins = api_get(
                    f"{BASE_URL}/{story_id}/insights",
                    params={"metric": "impressions", "access_token": TOKEN},
                )
                for entry in ins.get("data", []):
                    if entry["name"] == "impressions":
                        story_views += entry.get("values", [{}])[0].get("value", 0)
            except Exception:
                pass
        result["visualizacoes_stories"] = story_views
        print(f"  IG visualizacoes_stories: {story_views}")

    except Exception as e:
        print(f"  Aviso IG publicacoes/stories: {e}", file=sys.stderr)
        result.setdefault("publicacoes", 0)
        # stories e visualizacoes_stories omitidos em falha — nao gravam zero

    return result


# ===== ATUALIZA JSON =====

TRI_LABELS = {1: "1º tri", 2: "2º tri", 3: "3º tri", 4: "4º tri"}


def quarter_of(day_str):
    return (int(day_str[5:7]) - 1) // 3 + 1


def recalcular_conteudo(data, year_str, q):
    """
    Recalcula feed/stories do trimestre a partir das séries diárias e
    atualiza data['conteudo'][year][plat][q-1]. Preserva os demais campos
    (alcance, reações etc.) que podem ter sido preenchidos manualmente.
    """
    conteudo = data.setdefault("conteudo", {})
    conteudo.setdefault(year_str, {})

    for plat in ("facebook", "instagram"):
        series_plat = data["series"].get(plat, {})
        pub_series  = series_plat.get("publicacoes") or {}
        str_series  = series_plat.get("stories") or {}

        feed_total    = sum(v for d, v in pub_series.items()
                            if d[:4] == year_str and quarter_of(d) == q)
        stories_total = sum(v for d, v in str_series.items()
                            if d[:4] == year_str and quarter_of(d) == q)

        if feed_total == 0 and stories_total == 0:
            continue

        plat_list = conteudo[year_str].setdefault(plat, [None, None, None, None])
        while len(plat_list) < 4:
            plat_list.append(None)

        existing = plat_list[q - 1]
        if existing is None:
            plat_list[q - 1] = {
                "t":                 TRI_LABELS[q],
                "feed":              feed_total,
                "stories":           stories_total,
                "alcance":           0,
                "alcanceStories":    0,
                "reacoes":           0,
                "comentarios":       0,
                "compartilhamentos": 0,
            }
        else:
            existing["feed"]    = feed_total
            existing["stories"] = stories_total


def update_json(fb_metrics, ig_metrics, dry_run=False):
    if not dry_run:
        require_publication_disabled()
    path = "dados/metricas.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for key, value in fb_metrics.items():
        data["series"]["facebook"].setdefault(key, {})[yesterday] = value

    for key, value in ig_metrics.items():
        data["series"]["instagram"].setdefault(key, {})[yesterday] = value

    data["atualizado"] = yesterday

    # Recalcula o resumo trimestral (conteudo) a partir das séries acumuladas
    year_str = yesterday[:4]
    q        = quarter_of(yesterday)
    recalcular_conteudo(data, year_str, q)
    print(f"  Conteudo {year_str} {TRI_LABELS[q]} atualizado.")

    if dry_run:
        print(f"\n[dry-run] registros que seriam gravados em {yesterday}:")
        print("  Facebook:")
        for k, v in fb_metrics.items():
            print(f"    {k}: {v}")
        print("  Instagram:")
        for k, v in ig_metrics.items():
            print(f"    {k}: {v}")
        print("\n[dry-run] metricas.json NAO foi modificado.")
        return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\nmetricas.json atualizado para {yesterday}")


def main():
    parser = argparse.ArgumentParser(
        description="Coleta métricas diárias de Facebook e Instagram e atualiza metricas.json."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Faz as consultas à API mas não grava no metricas.json.")
    args = parser.parse_args()

    if not args.dry_run:
        parser.error("account_publication_suspended: somente observacao sem gravacao")

    if args.dry_run:
        print("MODO DRY-RUN: nenhum dado sera gravado.\n")

    print(f"Coletando metricas de {yesterday}...\n")
    print("Facebook:")
    fb = fetch_facebook_metrics()

    print("\nInstagram:")
    try:
        ig = fetch_instagram_metrics()
    except Exception as e:
        print(f"  ERRO Instagram: {e}", file=sys.stderr)
        ig = {}

    update_json(fb, ig, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
