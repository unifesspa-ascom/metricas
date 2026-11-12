#!/usr/bin/env python3
"""
Backfill de métricas diárias de conta para um ano completo.

Percorre o ano mês a mês (a API limita janelas a ~90 dias por chamada),
coleta métricas de perfil do Facebook e do Instagram e preenche o arquivo
dados/metricas.json com as séries históricas no mesmo formato que o
update_metrics.py usa para dados correntes.

Ao final, recalcula os resumos trimestrais (conteudo) do ano.

Uso:
  python scripts/backfill_metrics.py --year 2025
  python scripts/backfill_metrics.py --year 2025 --skip-facebook
  python scripts/backfill_metrics.py --year 2025 --skip-instagram
  python scripts/backfill_metrics.py --year 2025 --dry-run   # mostra o que seria coletado sem salvar
  python scripts/backfill_metrics.py --start 2026-10-07 --end 2026-11-07 --skip-instagram

Variáveis de ambiente necessárias:
  FB_PAGE_ACCESS_TOKEN  -- Token de acesso permanente da página
  FB_PAGE_ID            -- ID da página do Facebook (padrão: 447748898661292)

Notas importantes:
  - Alcance (reach) diário já é deduplificado por dia pela Meta, mas a soma
    trimestral ainda pode contar a mesma pessoa em dias diferentes.
    O Meta Business Suite mostra alcance único trimestral deduplificado, que
    não está disponível diretamente via API — a divergência é esperada.
  - Stories de mais de 24 horas já expiraram na API; o backfill retornará
    0 para visualizações de stories antigos (normal).
  - Posts colaborativos onde a Unifesspa é colaboradora NÃO aparecem no
    endpoint /media — limitação da API, reflete na contagem de publicações.
"""

import argparse
import calendar
import os
import json
import sys
import time
import requests
from datetime import date, timedelta, datetime, timezone

API_VERSION = "v21.0"
BASE_URL    = f"https://graph.facebook.com/{API_VERSION}"

PAGE_ID = os.environ.get("FB_PAGE_ID", "447748898661292")
TOKEN   = os.environ["FB_PAGE_ACCESS_TOKEN"]

METRICS_PATH = "dados/metricas.json"

# Horário de Brasília (UTC-3) — mantém consistência com update_metrics.py
BRT = timezone(timedelta(hours=-3))

TRI_LABELS = {1: "1º tri", 2: "2º tri", 3: "3º tri", 4: "4º tri"}

# --- Métricas a coletar (mesmo conjunto de update_metrics.py) ---

FB_METRICS = {
    "visualizadores": "page_total_media_view_unique",
    "visualizacoes":  "page_media_view",
    "interacoes":     "page_post_engagements",
    "cliques":        "page_total_actions",
    "seguidores":     "page_daily_follows_unique",
    "visitas":        "page_views_total",
}

FB_METRICS_FALLBACK = {
    "page_total_media_view_unique": "page_impressions_unique",
    "page_media_view":              "page_impressions",
}

# Métricas IG com suporte a period=day em v21 — série diária via "values"
# impressions, profile_views, website_clicks e follower_count foram removidas:
# impressions e follower_count não aceitam period=day em v21;
# profile_views foi renomeada para profile_visits (ainda instável);
# website_clicks foi substituída por profile_links_taps (ver IG_TOTAL_VALUE_METRICS).
IG_DIRECT_METRICS = {
    "alcance": "reach",
}

# Métricas IG que exigem metric_type=total_value em v21.
# A API retorna um único inteiro por janela solicitada, não uma série.
# Para obter granularidade diária, o backfill itera dia a dia.
# ATENÇÃO: "cliques" mapeado aqui representa profile_links_taps (toques no link
# da bio do perfil). Dados anteriores a esta mudança (chave "cliques" no JSON)
# podem representar website_clicks, que é uma métrica diferente — não compare
# os dois períodos diretamente.
IG_TOTAL_VALUE_METRICS = {
    "interacoes": "accounts_engaged",
    "cliques":    "profile_links_taps",
}


# =============================================================================
# Utilitários
# =============================================================================

def utc_to_brt_date(utc_str: str) -> str:
    """Converte timestamp UTC da API para data no horário de Brasília (UTC-3)."""
    if not utc_str:
        return ""
    try:
        normalized = utc_str.replace("+0000", "+00:00").replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(BRT).date().isoformat()
    except Exception:
        return utc_str[:10]


def api_get(url, params, retries=3):
    """GET com retry para rate limit e erros temporários."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=45)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 10 * (attempt + 1)
                print(f"  Rate limit / erro {r.status_code}, aguardando {wait}s...",
                      file=sys.stderr)
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
        except (requests.ConnectionError, requests.Timeout):
            if attempt < retries - 1:
                time.sleep(5)
                continue
            raise
    raise RuntimeError(f"Falha após {retries} tentativas")


def generate_months(year: int):
    """Gera pares (since, until) para cada mês do ano, em datas ISO."""
    for month in range(1, 13):
        last_day = calendar.monthrange(year, month)[1]
        since  = date(year, month, 1).isoformat()
        # until é o primeiro dia do mês SEGUINTE (a API usa [since, until))
        if month < 12:
            until_ = date(year, month + 1, 1).isoformat()
        else:
            until_ = date(year + 1, 1, 1).isoformat()
        yield since, until_, month, last_day


def generate_windows(start: date, end: date):
    """
    Divide o intervalo [start, end] em janelas mensais para a API.
    A API usa intervalos semi-abertos [since, until), então until do
    último chunk é end + 1 dia para incluir o próprio end.
    Yields (since_str, until_str, month_int, last_day_int).
    """
    cur = date(start.year, start.month, 1)
    end_excl = end + timedelta(days=1)   # torna o intervalo exclusivo no lado direito
    while cur <= end:
        y, m = cur.year, cur.month
        last_day = calendar.monthrange(y, m)[1]
        since  = max(cur, start).isoformat()
        # until: primeiro dia do mês seguinte, mas não além de end+1
        if m < 12:
            month_end = date(y, m + 1, 1)
        else:
            month_end = date(y + 1, 1, 1)
        until_ = min(month_end, end_excl).isoformat()
        yield since, until_, m, last_day
        cur = month_end


def quarter_of(day_str: str) -> int:
    return (int(day_str[5:7]) - 1) // 3 + 1


# =============================================================================
# Facebook
# =============================================================================

def get_ig_user_id() -> str:
    data = api_get(
        f"{BASE_URL}/{PAGE_ID}",
        params={"fields": "instagram_business_account", "access_token": TOKEN},
    )
    ig = data.get("instagram_business_account")
    if not ig:
        raise RuntimeError("Página sem conta Instagram Business vinculada.")
    return ig["id"]


def fetch_fb_insights_month(since: str, until_: str) -> dict:
    """
    Coleta métricas diárias do Facebook para o mês [since, until_).
    Retorna dict: {json_key: {date_brt: value}}.
    """
    result = {k: {} for k in FB_METRICS}

    for json_key, metric_name in FB_METRICS.items():
        fetched = False
        for name in [metric_name, FB_METRICS_FALLBACK.get(metric_name)]:
            if name is None:
                continue
            try:
                data = api_get(
                    f"{BASE_URL}/{PAGE_ID}/insights/{name}/day",
                    params={"since": since, "until": until_, "access_token": TOKEN},
                )
                # A resposta pode ter paginação; precisamos de todos os dias do mês
                while data:
                    for entry in data.get("data", []):
                        for val in entry.get("values", []):
                            day = utc_to_brt_date(val.get("end_time", ""))
                            if day and day >= since and day < until_:
                                result[json_key][day] = val.get("value", 0)
                    next_url = data.get("paging", {}).get("next")
                    if not next_url:
                        break
                    data = api_get(next_url, {})
                fetched = True
                if name != metric_name:
                    print(f"    FB {json_key}: usando '{name}' (fallback de '{metric_name}')",
                          file=sys.stderr)
                break
            except Exception as e:
                print(f"    AVISO FB {json_key} ({name}): {e}", file=sys.stderr)

        if not fetched:
            print(f"    AVISO FB {json_key}: todas as tentativas falharam para {since}",
                  file=sys.stderr)
        time.sleep(0.3)

    return result


def count_fb_publications_month(since: str, until_: str) -> tuple[dict, dict]:
    """
    Conta posts e stories do Facebook publicados por dia no mês.
    Retorna (publicacoes_por_dia, stories_por_dia).
    """
    pub_by_day = {}
    sto_by_day = {}

    # Posts do feed
    url = f"{BASE_URL}/{PAGE_ID}/published_posts"
    params = {
        "fields": "id,created_time",
        "since": since,
        "until": until_,
        "access_token": TOKEN,
        "limit": 100,
    }
    while url:
        data = api_get(url, params)
        for item in data.get("data", []):
            day = utc_to_brt_date(item.get("created_time", ""))
            if day:
                pub_by_day[day] = pub_by_day.get(day, 0) + 1
        url    = data.get("paging", {}).get("next")
        params = {}
        time.sleep(0.2)

    # Stories
    url = f"{BASE_URL}/{PAGE_ID}/stories"
    params = {
        "fields": "id,created_time",
        "since": since,
        "until": until_,
        "access_token": TOKEN,
        "limit": 100,
    }
    while url:
        try:
            data = api_get(url, params)
            for item in data.get("data", []):
                day = utc_to_brt_date(item.get("created_time", ""))
                if day:
                    sto_by_day[day] = sto_by_day.get(day, 0) + 1
            url    = data.get("paging", {}).get("next")
            params = {}
            time.sleep(0.2)
        except Exception as e:
            print(f"    AVISO FB stories: {e}", file=sys.stderr)
            break

    return pub_by_day, sto_by_day


# =============================================================================
# Instagram
# =============================================================================

def fetch_ig_insights_month(ig_user_id: str, since: str, until_: str) -> dict:
    """
    Coleta métricas diárias do Instagram para o intervalo [since, until_).
    Retorna dict: {json_key: {date_str: value}}.

    - reach: série diária nativa via period=day (uma chamada por mês).
    - accounts_engaged / profile_links_taps: exigem metric_type=total_value em v21,
      que retorna um único inteiro por janela. Para obter granularidade diária,
      iteramos dia a dia (since=D, until=D+1).
      Regra: se a API retornar erro ou resposta vazia para um dia, esse dia
      NÃO é gravado (não inserimos zero para falhas).
    """
    result = {k: {} for k in IG_DIRECT_METRICS}
    for k in IG_TOTAL_VALUE_METRICS:
        result[k] = {}

    # --- reach: série diária nativa ---
    try:
        data = api_get(
            f"{BASE_URL}/{ig_user_id}/insights",
            params={
                "metric": "reach",
                "period": "day",
                "since":  since,
                "until":  until_,
                "access_token": TOKEN,
            },
        )
        while data:
            for entry in data.get("data", []):
                if entry.get("name") == "reach":
                    for val in entry.get("values", []):
                        day = utc_to_brt_date(val.get("end_time", ""))
                        if day and day >= since and day < until_:
                            result["alcance"][day] = val.get("value", 0)
            next_url = data.get("paging", {}).get("next")
            if not next_url:
                break
            data = api_get(next_url, {})
    except Exception as e:
        print(f"    AVISO IG alcance (reach) {since}: {e}", file=sys.stderr)
    time.sleep(0.3)

    # --- accounts_engaged e profile_links_taps: total_value, dia a dia ---
    # Gera lista de dias do intervalo [since, until_)
    cur = date.fromisoformat(since)
    end = date.fromisoformat(until_)
    while cur < end:
        day_since = cur.isoformat()
        day_until = (cur + timedelta(days=1)).isoformat()
        for json_key, metric_name in IG_TOTAL_VALUE_METRICS.items():
            try:
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
                if entries:
                    tv = entries[0].get("total_value", {})
                    if "value" in tv:
                        result[json_key][day_since] = tv["value"]
                    # Se "value" ausente: não grava — dado indisponível, não zero
            except Exception as e:
                print(f"    AVISO IG {json_key} ({metric_name}) {day_since}: {e}",
                      file=sys.stderr)
                # Não grava nada para este dia — falha não vira zero
            time.sleep(0.15)
        cur += timedelta(days=1)

    return result


def count_ig_publications_month(ig_user_id: str, since: str, until_: str) -> tuple[dict, dict]:
    """
    Conta posts (feed + reels) e stories do Instagram por dia no mês.
    Retorna (publicacoes_por_dia, stories_por_dia).
    """
    pub_by_day = {}
    sto_by_day = {}

    url = f"{BASE_URL}/{ig_user_id}/media"
    params = {
        "fields": "id,timestamp,media_type",
        "since": since,
        "until": until_,
        "access_token": TOKEN,
        "limit": 100,
    }
    while url:
        data = api_get(url, params)
        for item in data.get("data", []):
            day = utc_to_brt_date(item.get("timestamp", ""))
            if not day:
                continue
            if item.get("media_type") == "STORY":
                sto_by_day[day] = sto_by_day.get(day, 0) + 1
            else:
                # IMAGE, VIDEO, REEL, CAROUSEL_ALBUM — todos contam como feed
                pub_by_day[day] = pub_by_day.get(day, 0) + 1
        url    = data.get("paging", {}).get("next")
        params = {}
        time.sleep(0.2)

    return pub_by_day, sto_by_day


# =============================================================================
# JSON — atualização e recálculo trimestral
# =============================================================================

def merge_series(existing: dict, new_data: dict) -> int:
    """
    Mescla new_data (dict de {data: valor}) em existing.
    Retorna a quantidade de datas gravadas.
    """
    count = 0
    for day, value in new_data.items():
        existing[day] = value
        count += 1
    return count


def recalcular_conteudo(data: dict, year_str: str):
    """
    Recalcula os resumos trimestrais de feed/stories para o ano.
    Preserva campos manuais (alcance, reações etc.) já preenchidos.
    """
    conteudo = data.setdefault("conteudo", {})
    conteudo.setdefault(year_str, {})

    for plat in ("facebook", "instagram"):
        series_plat = data["series"].get(plat, {})
        pub_series  = series_plat.get("publicacoes") or {}
        str_series  = series_plat.get("stories")     or {}

        for q in range(1, 5):
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


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Backfill de métricas diárias de conta para um ano completo ou intervalo específico."
    )
    parser.add_argument("--year", type=int, default=None,
                        help="Ano a backfillar (padrão: 2025 quando --start/--end não usados)")
    parser.add_argument("--start", type=str, default=None,
                        help="Data inicial do intervalo ISO (AAAA-MM-DD). "
                             "Requer --end. Substitui --year.")
    parser.add_argument("--end", type=str, default=None,
                        help="Data final do intervalo ISO (AAAA-MM-DD, inclusive). "
                             "Requer --start. Substitui --year.")
    parser.add_argument("--skip-facebook", action="store_true",
                        help="Pula coleta do Facebook")
    parser.add_argument("--skip-instagram", action="store_true",
                        help="Pula coleta do Instagram")
    parser.add_argument("--dry-run", action="store_true",
                        help="Exibe o que seria coletado sem gravar no JSON")
    args = parser.parse_args()

    # Resolve intervalo de datas
    if (args.start is None) != (args.end is None):
        parser.error("--start e --end devem ser usados juntos.")

    if args.start:
        try:
            start_date = date.fromisoformat(args.start)
            end_date   = date.fromisoformat(args.end)
        except ValueError as e:
            parser.error(f"Data inválida: {e}")
        if start_date > end_date:
            parser.error("--start deve ser anterior ou igual a --end.")
        # anos cobertos pelo intervalo (pode ser mais de um)
        years_covered = sorted({d.year for d in
                                 (start_date + timedelta(days=i)
                                  for i in range((end_date - start_date).days + 1))})
        windows = list(generate_windows(start_date, end_date))
        mode_desc = f"{args.start} → {args.end}"
    else:
        year = args.year if args.year is not None else 2025
        start_date = date(year, 1, 1)
        end_date   = date(year, 12, 31)
        years_covered = [year]
        windows = list(generate_months(year))
        mode_desc = f"ano {year}"

    print(f"Backfill de métricas — {mode_desc}")
    print(f"  Facebook : {'IGNORADO' if args.skip_facebook  else 'SIM'}")
    print(f"  Instagram: {'IGNORADO' if args.skip_instagram else 'SIM'}")
    if args.dry_run:
        print("  MODO DRY-RUN: nada será salvo.\n")

    # Carrega JSON existente
    try:
        with open(METRICS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Arquivo {METRICS_PATH} não encontrado. Iniciando estrutura vazia.")
        data = {"atualizado": "", "series": {"facebook": {}, "instagram": {}}, "conteudo": {}}

    data.setdefault("series", {})
    data["series"].setdefault("facebook", {})
    data["series"].setdefault("instagram", {})

    ig_user_id = None
    if not args.skip_instagram:
        print("\nBuscando ID da conta Instagram...")
        ig_user_id = get_ig_user_id()
        print(f"  IG User ID: {ig_user_id}")

    total_dias = {"facebook": 0, "instagram": 0}

    for since, until_, month, _ in windows:
        print(f"\n--- {since[:7]} ({since} → {until_}) ---")

        # ===== FACEBOOK =====
        if not args.skip_facebook:
            print("  Facebook — métricas de conta...")
            fb_insights = fetch_fb_insights_month(since, until_)

            print("  Facebook — contagem de publicações...")
            fb_pub, fb_sto = count_fb_publications_month(since, until_)

            if not args.dry_run:
                fb_series = data["series"]["facebook"]
                for json_key, day_values in fb_insights.items():
                    fb_series.setdefault(json_key, {})
                    n = merge_series(fb_series[json_key], day_values)
                    total_dias["facebook"] += n

                fb_series.setdefault("publicacoes", {})
                n = merge_series(fb_series["publicacoes"], fb_pub)
                total_dias["facebook"] += n

                fb_series.setdefault("stories", {})
                merge_series(fb_series["stories"], fb_sto)

            fb_posts_sum = sum(fb_pub.values())
            fb_sto_sum   = sum(fb_sto.values())
            print(f"    dias com métricas: {len(next(iter(fb_insights.values()), {}))}")
            print(f"    posts: {fb_posts_sum}  |  stories: {fb_sto_sum}")

        # ===== INSTAGRAM =====
        if not args.skip_instagram and ig_user_id:
            print("  Instagram — métricas de conta...")
            ig_insights = fetch_ig_insights_month(ig_user_id, since, until_)

            print("  Instagram — contagem de publicações...")
            ig_pub, ig_sto = count_ig_publications_month(ig_user_id, since, until_)

            if not args.dry_run:
                ig_series = data["series"]["instagram"]
                for json_key, day_values in ig_insights.items():
                    ig_series.setdefault(json_key, {})
                    n = merge_series(ig_series[json_key], day_values)
                    total_dias["instagram"] += n

                ig_series.setdefault("publicacoes", {})
                merge_series(ig_series["publicacoes"], ig_pub)

                ig_series.setdefault("stories", {})
                merge_series(ig_series["stories"], ig_sto)

            ig_posts_sum = sum(ig_pub.values())
            ig_sto_sum   = sum(ig_sto.values())
            print(f"    dias com métricas: {len(next(iter(ig_insights.values()), {}))}")
            print(f"    posts: {ig_posts_sum}  |  stories: {ig_sto_sum}")

            if args.dry_run:
                preview_keys = ["alcance", "interacoes", "cliques"]
                all_days = sorted({d for k in preview_keys
                                   for d in ig_insights.get(k, {})})
                if all_days:
                    print("    [dry-run] valores que seriam gravados:")
                    for day in all_days:
                        vals = "  ".join(
                            f"{k}={ig_insights[k][day]}"
                            for k in preview_keys
                            if day in ig_insights.get(k, {})
                        )
                        print(f"      {day}: {vals}")

    # ===== RECALCULA TRIMESTRES E SALVA =====
    if not args.dry_run:
        for yr in years_covered:
            print(f"\nRecalculando resumos trimestrais de {yr}...")
            recalcular_conteudo(data, str(yr))

        with open(METRICS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

        print(f"\nmetricas.json atualizado.")
        print(f"  Dias gravados — FB: {total_dias['facebook']}  |  IG: {total_dias['instagram']}")
    else:
        print("\nDry-run concluído — nenhuma alteração gravada.")


if __name__ == "__main__":
    main()
