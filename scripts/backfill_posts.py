#!/usr/bin/env python3
"""
Backfill de publicações individuais do Facebook e Instagram.
Coleta dados históricos de posts numa janela de datas e salva em dados/posts.json.

Uso:
  python scripts/backfill_posts.py --start 2024-01-01 --end 2024-12-31
  python scripts/backfill_posts.py --start 2025-01-01          # até hoje
  python scripts/backfill_posts.py --skip-instagram
  python scripts/backfill_posts.py --skip-insights             # só conta posts, sem insights

Variáveis de ambiente necessárias:
  FB_PAGE_ACCESS_TOKEN  -- Token de acesso permanente da página
  FB_PAGE_ID            -- ID da página do Facebook (padrão: 447748898661292)
"""

import argparse
import os
import json
import sys
import time
import requests
from datetime import date, datetime

API_VERSION = "v21.0"
BASE_URL    = f"https://graph.facebook.com/{API_VERSION}"

PAGE_ID = os.environ.get("FB_PAGE_ID", "447748898661292")
TOKEN   = os.environ["FB_PAGE_ACCESS_TOKEN"]

POSTS_PATH = "dados/posts.json"

# Pausa entre chamadas de insights para evitar rate limit
INSIGHT_SLEEP = 0.3


def api_get(url, params, retries=3):
    """Faz GET com retry simples em caso de erro temporário."""
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
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            raise
    raise RuntimeError(f"Falha após {retries} tentativas")


def get_ig_user_id():
    data = api_get(
        f"{BASE_URL}/{PAGE_ID}",
        params={"fields": "instagram_business_account", "access_token": TOKEN},
    )
    ig = data.get("instagram_business_account")
    if not ig:
        raise RuntimeError("Página sem conta Instagram Business vinculada.")
    return ig["id"]


# ===== FACEBOOK =====

def fetch_fb_post_insights(post_id):
    """Busca métricas de um post individual do Facebook."""
    try:
        time.sleep(INSIGHT_SLEEP)
        data = api_get(
            f"{BASE_URL}/{post_id}/insights",
            params={
                "metric": "post_impressions,post_impressions_unique,post_engaged_users,"
                          "post_reactions_by_type_total",
                "access_token": TOKEN,
            },
        )
        result = {}
        for entry in data.get("data", []):
            name = entry["name"]
            if entry.get("values"):
                value = entry["values"][0].get("value", 0)
            else:
                value = entry.get("value", 0)
            result[name] = value
        return result
    except Exception as e:
        print(f"    Aviso insights FB {post_id}: {e}", file=sys.stderr)
        return {}


def fetch_fb_posts(since_date, until_date, skip_insights=False):
    """Coleta publicações do Facebook num intervalo de datas."""
    posts = []
    url = f"{BASE_URL}/{PAGE_ID}/published_posts"
    params = {
        "fields": "id,created_time,message,type,permalink_url,"
                  "shares,comments.summary(true).limit(0)",
        "since": since_date,
        "until": until_date,
        "access_token": TOKEN,
        "limit": 100,
    }

    page_num = 0
    while url:
        page_num += 1
        data = api_get(url, params)
        items = data.get("data", [])
        print(f"  FB página {page_num}: {len(items)} posts...")

        for item in items:
            post_date = item.get("created_time", "")[:10]

            if skip_insights:
                insights = {}
            else:
                insights = fetch_fb_post_insights(item["id"])

            reac_raw   = insights.get("post_reactions_by_type_total", 0)
            reac_total = (sum(reac_raw.values())
                          if isinstance(reac_raw, dict) else int(reac_raw or 0))

            shares   = item.get("shares", {}).get("count", 0)
            comments = item.get("comments", {}).get("summary", {}).get("total_count", 0)

            msg = item.get("message", "")
            txt = msg[:120].strip() if msg else ""

            post = {
                "id":   item["id"],
                "data": post_date,
                "tipo": item.get("type", "status"),
                "txt":  txt,
                "url":  item.get("permalink_url", ""),
                "vis":  insights.get("post_impressions", 0),
                "alc":  insights.get("post_impressions_unique", 0),
                "eng":  insights.get("post_engaged_users", 0),
                "reac": reac_total,
                "com":  comments,
                "comp": shares,
            }
            posts.append(post)

        url    = data.get("paging", {}).get("next")
        params = {}

    print(f"  FB total: {len(posts)} posts coletados")
    return posts


# ===== INSTAGRAM =====

def fetch_ig_media_insights(media_id):
    """Busca métricas de uma mídia individual do Instagram.
    Nota: insights de mídias com mais de 2 anos podem não estar disponíveis.
    """
    try:
        time.sleep(INSIGHT_SLEEP)
        data = api_get(
            f"{BASE_URL}/{media_id}/insights",
            params={
                "metric": "impressions,reach,saved",
                "access_token": TOKEN,
            },
        )
        result = {}
        for entry in data.get("data", []):
            if entry.get("values"):
                result[entry["name"]] = entry["values"][0].get("value", 0)
            else:
                result[entry["name"]] = entry.get("value", 0)
        return result
    except Exception as e:
        print(f"    Aviso insights IG {media_id}: {e}", file=sys.stderr)
        return {}


def fetch_ig_posts(ig_user_id, since_date, until_date, skip_insights=False):
    """Coleta publicações do Instagram num intervalo de datas. Stories ignorados."""
    posts = []
    url = f"{BASE_URL}/{ig_user_id}/media"
    params = {
        "fields": "id,timestamp,media_type,like_count,comments_count,caption,permalink",
        "since": since_date,
        "until": until_date,
        "access_token": TOKEN,
        "limit": 100,
    }

    page_num = 0
    while url:
        page_num += 1
        data = api_get(url, params)
        items = data.get("data", [])
        print(f"  IG página {page_num}: {len(items)} mídias...")

        for item in items:
            if item.get("media_type") == "STORY":
                continue

            post_date = item.get("timestamp", "")[:10]

            if skip_insights:
                insights = {}
            else:
                insights = fetch_ig_media_insights(item["id"])

            cap = item.get("caption", "")
            txt = cap[:120].strip() if cap else ""

            post = {
                "id":   item["id"],
                "data": post_date,
                "tipo": item.get("media_type", ""),
                "txt":  txt,
                "url":  item.get("permalink", ""),
                "vis":  insights.get("impressions", 0),
                "alc":  insights.get("reach", 0),
                "curt": item.get("like_count", 0),
                "com":  item.get("comments_count", 0),
                "salv": insights.get("saved", 0),
            }
            posts.append(post)

        url    = data.get("paging", {}).get("next")
        params = {}

    # Remove stories (podem ter passado no filtro de tipo)
    posts = [p for p in posts if p.get("tipo") != "STORY"]
    print(f"  IG total: {len(posts)} posts coletados")
    return posts


# ===== ARQUIVO posts.json =====

def load_posts():
    try:
        with open(POSTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"atualizado": "", "facebook": [], "instagram": []}


def merge_posts(existing_list, new_posts):
    idx = {p["id"]: i for i, p in enumerate(existing_list)}
    added = updated = 0
    for post in new_posts:
        pid = post["id"]
        if pid in idx:
            existing_list[idx[pid]] = post
            updated += 1
        else:
            existing_list.append(post)
            added += 1
    existing_list.sort(key=lambda p: p.get("data", ""), reverse=True)
    print(f"  → {added} adicionados, {updated} atualizados")
    return existing_list


def save_posts(data):
    with open(POSTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\nposts.json salvo — {len(data['facebook'])} FB, {len(data['instagram'])} IG")


# ===== MAIN =====

def parse_args():
    parser = argparse.ArgumentParser(description="Backfill de publicações individuais")
    parser.add_argument(
        "--start", required=True,
        help="Data inicial (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end", default=date.today().isoformat(),
        help="Data final (YYYY-MM-DD, padrão: hoje)",
    )
    parser.add_argument(
        "--skip-instagram", action="store_true",
        help="Pular coleta do Instagram",
    )
    parser.add_argument(
        "--skip-insights", action="store_true",
        help="Não buscar insights por post (mais rápido, apenas campos básicos)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    since  = args.start
    until_ = args.end

    print(f"Backfill de publicações: {since} → {until_}")
    if args.skip_insights:
        print("  (modo sem insights — apenas campos básicos)")
    print()

    data = load_posts()

    print("Facebook:")
    try:
        fb_new = fetch_fb_posts(since, until_, skip_insights=args.skip_insights)
        data["facebook"] = merge_posts(data["facebook"], fb_new)
    except Exception as e:
        print(f"  ERRO Facebook posts: {e}", file=sys.stderr)

    if not args.skip_instagram:
        print("\nInstagram:")
        try:
            ig_user_id = get_ig_user_id()
            ig_new = fetch_ig_posts(ig_user_id, since, until_,
                                    skip_insights=args.skip_insights)
            data["instagram"] = merge_posts(data["instagram"], ig_new)
        except Exception as e:
            print(f"  ERRO Instagram posts: {e}", file=sys.stderr)

    data["atualizado"] = date.today().isoformat()
    save_posts(data)


if __name__ == "__main__":
    main()
