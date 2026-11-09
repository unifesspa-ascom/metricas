#!/usr/bin/env python3
"""
Coleta metricas diarias do Facebook e Instagram via Meta Graph API
e atualiza o arquivo dados/metricas.json.

Variaveis de ambiente necessarias:
  FB_PAGE_ACCESS_TOKEN  -- Token de acesso permanente da pagina
  FB_PAGE_ID            -- ID da pagina do Facebook (padrao: 447748898661292)
"""

import os
import jsonh
import sys
import requests
from datetime import date, timedelta

API_VERSION = "v21.0"
BASE_URL    = f"https://graph.facebook.com/{API_VERSION}"

PAGE_ID = os.environ.get("FB_PAGE_ID", "447748898661292")
TOKEN   = os.environ["FB_PAGE_ACCESS_TOKEN"]

yesterday = (date.today() - timedelta(days=1)).isoformat()
since     = yesterday
until_    = date.today().isoformat()

# --- Facebook Page Insights ---
FB_METRICS = {
    "visualizadores": "page_total_media_view_unique",
    "visualizacoes":  "page_media_view",
    "interacoes":     "page_post_engagements",
    "cliques":        "page_total_actions",
    "seguidores":     "page_daily_follows_unique",
    "visitas":        "page_views_total",
}

# --- Instagram User Insights ---
IG_CONTACT_METRICS = [
    "email_contacts",
    "get_directions_clicks",
    "phone_call_clicks",
    "text_message_clicks",
]

IG_DIRECT_METRICS = {
    "visualizacoes": "impressions",
    "alcance":       "reach",
    "visitas":       "profile_views",
    "cliques":       "website_clicks",
    "seguidores":    "follower_count",
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

def fetch_facebook_metrics():
    result = {}
    for json_key, metric_name in FB_METRICS.items():
        data = api_get(
            f"{BASE_URL}/{PAGE_ID}/insights/{metric_name}/day",
            params={"since": since, "until": until_, "access_token": TOKEN},
        )
        result[json_key] = extract_day_value(data["data"][0], yesterday) if data.get("data") else 0
        print(f"  FB {json_key}: {result[json_key]}")

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

    # Interacoes = soma de acoes de contato no perfil
    try:
        contact = fetch_ig_insights(ig_user_id, IG_CONTACT_METRICS)
        result["interacoes"] = sum(extract_day_value(e, yesterday) for e in contact.get("data", []))
        print(f"  IG interacoes: {result['interacoes']}")
    except Exception as e:
        print(f"  Aviso IG interacoes: {e}", file=sys.stderr)
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

def update_json(fb_metrics, ig_metrics):
    path = "dados/metricas.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for key, value in fb_metrics.items():
        data["series"]["facebook"].setdefault(key, {})[yesterday] = value

    for key, value in ig_metrics.items():
        data["series"]["instagram"].setdefault(key, {})[yesterday] = value

    data["atualizado"] = yesterday

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
