#!/usr/bin/env python3
"""
Coleta metricas diarias do Facebook e Instagram via Meta Graph API
e atualiza o arquivo dados/metricas.json.

Variaveis de ambiente necessarias:
  FB_PAGE_ACCESS_TOKEN  -- Token de acesso permanente da pagina
  FB_PAGE_ID            -- ID da pagina do Facebook (padrao: 447748898661292)
"""

import os
import json
import sys
import requests
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
# NOTA: alguns nomes de métricas foram alterados na Graph API v18.0+.
# O script tenta o nome principal; se falhar, registra aviso com o nome que falhou
# para facilitar diagnóstico. Nomes alternativos conhecidos estão nos comentários.
FB_METRICS = {
    # viewers únicos — se falhar, tentar page_impressions_unique
    "visualizadores": "page_total_media_view_unique",
    # visualizações totais — se falhar, tentar page_impressions
    "visualizacoes":  "page_media_view",
    # engajamentos — se falhar, tentar page_actions_post_reactions_total
    "interacoes":     "page_post_engagements",
    # ações totais no perfil
    "cliques":        "page_total_actions",
    # novos seguidores
    "seguidores":     "page_daily_follows_unique",
    # visitas ao perfil
    "visitas":        "page_views_total",
}

# Nomes alternativos para métricas FB potencialmente depreciadas (v18.0+).
# Se a métrica principal falhar, tenta o alternativo (quando disponível).
FB_METRICS_FALLBACK = {
    "page_total_media_view_unique": "page_impressions_unique",
    "page_media_view":              "page_impressions",
}

# --- Instagram User Insights ---
IG_DIRECT_METRICS = {
    "visualizacoes": "impressions",
    "alcance":       "reach",      # alcance diário da conta (único por dia, não soma por post)
    "visitas":       "profile_views",
    "cliques":       "website_clicks",
    "seguidores":    "follower_count",
}

# Métricas de engajamento com conteúdo (ponto 1 da análise ASCOM):
# "accounts_engaged" = contas únicas que interagiram com qualquer conteúdo
#   (curtidas + comentários + compartilhamentos + salvamentos)
# Corresponde ao campo "Interações com o Conteúdo" do Meta Business Suite.
# Anteriormente o script usava cliques de contato (email, telefone etc.),
# que é uma métrica diferente e incorreta para este relatório.
IG_ENGAGEMENT_METRICS = ["accounts_engaged"]


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

def fetch_facebook_metrics():
    result = {}
    for json_key, metric_name in FB_METRICS.items():
        value = None
        # Tenta o nome principal; em caso de erro, tenta o alternativo (se houver)
        for name in [metric_name, FB_METRICS_FALLBACK.get(metric_name)]:
            if name is None:
                continue
            try:
                data = api_get(
                    f"{BASE_URL}/{PAGE_ID}/insights/{name}/day",
                    params={"since": since, "until": until_, "access_token": TOKEN},
                )
                value = extract_day_value(data["data"][0], yesterday) if data.get("data") else 0
                if name != metric_name:
                    print(f"  AVISO FB {json_key}: '{metric_name}' falhou, usando '{name}'",
                          file=sys.stderr)
                break
            except Exception as e:
                print(f"  AVISO FB {json_key} ({name}): {e}", file=sys.stderr)

        if value is None:
            value = 0
            print(f"  AVISO FB {json_key}: todas as tentativas falharam, usando 0",
                  file=sys.stderr)
        result[json_key] = value
        print(f"  FB {json_key}: {value}")

    # Publicacoes no feed (posts publicados ontem)
    feed_count = count_paged_results(
        f"{BASE_URL}/{PAGE_ID}/published_posts",
        {"since": since, "until": until_, "access_token": TOKEN, "fields": "id"},
    )
    result["publicacoes"] = feed_count
    print(f"  FB publicacoes (feed): {feed_count}")

    # Stories publicados ontem
    stories_count = count_paged_results(
        f"{BASE_URL}/{PAGE_ID}/stories",
        {"since": since, "until": until_, "access_token": TOKEN, "fields": "id"},
    )
    result["stories"] = stories_count
    print(f"  FB stories: {stories_count}")

    return result


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


def fetch_ig_insights(ig_user_id, metric_names):
    return api_get(
        f"{BASE_URL}/{ig_user_id}/insights",
        params={
            "metric": ",".join(metric_names),
            "period": "day",
            "since":  since,
            "until":  until_,
            "access_token": TOKEN,
        },
    )


def fetch_instagram_metrics():
    ig_user_id = get_ig_user_id()
    result     = {}

    # Metricas diretas
    data = fetch_ig_insights(ig_user_id, list(IG_DIRECT_METRICS.values()))
    for entry in data.get("data", []):
        json_key = next((k for k, v in IG_DIRECT_METRICS.items() if v == entry["name"]), None)
        if json_key:
            value = extract_day_value(entry, yesterday)
            result[json_key] = value
            print(f"  IG {json_key}: {value}")

    # Interações com o conteúdo (ponto 1 da análise ASCOM):
    # "accounts_engaged" = contas únicas que interagiram com qualquer conteúdo
    # (curtidas + comentários + compartilhamentos + salvamentos + respostas).
    # Corresponde ao campo "Interações" do Meta Business Suite.
    # NOTA: se accounts_engaged não estiver disponível nesta conta/token,
    # o script loga o aviso e usa 0. Verificar no log da Action e, se necessário,
    # testar "total_interactions" como alternativa.
    try:
        eng = fetch_ig_insights(ig_user_id, IG_ENGAGEMENT_METRICS)
        result["interacoes"] = sum(extract_day_value(e, yesterday) for e in eng.get("data", []))
        print(f"  IG interacoes (accounts_engaged): {result['interacoes']}")
    except Exception as e:
        print(f"  AVISO IG interacoes (accounts_engaged indisponivel): {e}", file=sys.stderr)
        # Fallback: tenta total_interactions
        try:
            eng2 = fetch_ig_insights(ig_user_id, ["total_interactions"])
            result["interacoes"] = sum(extract_day_value(e, yesterday) for e in eng2.get("data", []))
            print(f"  IG interacoes (total_interactions fallback): {result['interacoes']}")
        except Exception as e2:
            print(f"  AVISO IG interacoes fallback falhou: {e2}", file=sys.stderr)
            result.setdefault("interacoes", 0)

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
        result.setdefault("stories", 0)
        result.setdefault("visualizacoes_stories", 0)

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


def update_json(fb_metrics, ig_metrics):
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

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\nmetricas.json atualizado para {yesterday}")


def main():
    print(f"Coletando metricas de {yesterday}...\n")
    print("Facebook:")
    fb = fetch_facebook_metrics()

    print("\nInstagram:")
    try:
        ig = fetch_instagram_metrics()
    except Exception as e:
        print(f"  ERRO Instagram: {e}", file=sys.stderr)
        ig = {}

    update_json(fb, ig)


if __name__ == "__main__":
    main()
