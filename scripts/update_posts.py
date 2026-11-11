#!/usr/bin/env python3
"""
Coleta dados de publicações individuais do Facebook e Instagram via Meta Graph API
e atualiza o arquivo dados/posts.json.

Roda diariamente via GitHub Actions, logo após update_metrics.py.
Estratégia: janela deslizante de 60 dias — re-busca métricas de todos os posts
recentes, pois alcance e engajamento continuam crescendo após a publicação.

Variáveis de ambiente necessárias:
  FB_PAGE_ACCESS_TOKEN  -- Token de acesso permanente da página
  FB_PAGE_ID            -- ID da página do Facebook (padrão: 447748898661292)
"""

import os
import json
import sys
import time
import requests
from datetime import date, timedelta

API_VERSION = "v21.0"
BASE_URL    = f"https://graph.facebook.com/{API_VERSION}"

PAGE_ID = os.environ.get("FB_PAGE_ID", "447748898661292")
TOKEN   = os.environ["FB_PAGE_ACCESS_TOKEN"]

# Janela deslizante: re-busca posts dos últimos N dias para capturar
# crescimento de métricas após publicação.
ROLLING_DAYS = 60

today  = date.today()
since  = (today - timedelta(days=ROLLING_DAYS)).isoformat()
until_ = today.isoformat()

POSTS_PATH = "dados/posts.json"

# Pausa entre chamadas de insights para evitar rate limit
INSIGHT_SLEEP = 0.3


def api_get(url, params, retries=3):
    """Faz GET com retry em caso de rate limit ou erro temporário."""
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
    """Busca métricas de um post individual do Facebook.

    Nota diagnóstica: loga os nomes dos campos retornados pela API para detectar
    renomeações (ex: post_impressions → views) conforme Meta vai alterando.
    """
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
        campos_disponiveis = []
        for entry in data.get("data", []):
            name = entry["name"]
            campos_disponiveis.append(name)
            if entry.get("values"):
                value = entry["values"][0].get("value", 0)
            else:
                value = entry.get("value", 0)
            result[name] = value

        # Log diagnóstico: detecta campos ausentes / renomeados
        esperados = {"post_impressions", "post_impressions_unique",
                     "post_engaged_users", "post_reactions_by_type_total"}
        ausentes = esperados - set(campos_disponiveis)
        if ausentes:
            print(f"    AVISO campos FB ausentes em {post_id}: {ausentes}", file=sys.stderr)

        return result
    except Exception as e:
        print(f"    Aviso insights FB {post_id}: {e}", file=sys.stderr)
        return {}


def fetch_fb_posts(since_date, until_date):
    """Coleta publicações do Facebook num intervalo de datas (com paginação)."""
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
            insights  = fetch_fb_post_insights(item["id"])

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

    print(f"  FB total: {len(posts)} posts na janela de {ROLLING_DAYS} dias")
    return posts


# ===== INSTAGRAM =====

def fetch_ig_media_insights(media_id):
    """Busca métricas de uma mídia individual do Instagram.

    Nota diagnóstica: loga campos ausentes para detectar renomeações da API.
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
        campos_disponiveis = []
        for entry in data.get("data", []):
            campos_disponiveis.append(entry["name"])
            if entry.get("values"):
                result[entry["name"]] = entry["values"][0].get("value", 0)
            else:
                result[entry["name"]] = entry.get("value", 0)

        ausentes = {"impressions", "reach", "saved"} - set(campos_disponiveis)
        if ausentes:
            print(f"    AVISO campos IG ausentes em {media_id}: {ausentes}", file=sys.stderr)

        return result
    except Exception as e:
        print(f"    Aviso insights IG {media_id}: {e}", file=sys.stderr)
        return {}


def fetch_ig_posts(ig_user_id, since_date, until_date):
    """Coleta publicações do Instagram num intervalo de datas. Stories ignorados.
    Reels são coletados com tipo 'REEL' — o dashboard os conta como Feed.
    """
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
            insights  = fetch_ig_media_insights(item["id"])

            cap = item.get("caption", "")
            txt = cap[:120].strip() if cap else ""

            post = {
                "id":   item["id"],
                "data": post_date,
                "tipo": item.get("media_type", ""),  # IMAGE, VIDEO, REEL, CAROUSEL_ALBUM
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

    print(f"  IG total: {len(posts)} posts na janela de {ROLLING_DAYS} dias")
    return posts


# ===== ARQUIVO posts.json =====

def load_posts():
    """Carrega dados/posts.json ou retorna estrutura vazia."""
    try:
        with open(POSTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"atualizado": "", "facebook": [], "instagram": []}


def merge_posts(existing_list, new_posts):
    """Mescla novos posts na lista existente, atualizando por ID."""
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

def main():
    print(f"Coletando publicações: janela de {ROLLING_DAYS} dias ({since} → {until_})\n")

    data = load_posts()

    print("Facebook:")
    try:
        fb_new = fetch_fb_posts(since, until_)
        data["facebook"] = merge_posts(data["facebook"], fb_new)
    except Exception as e:
        print(f"  ERRO Facebook posts: {e}", file=sys.stderr)

    print("\nInstagram:")
    try:
        ig_user_id = get_ig_user_id()
        ig_new = fetch_ig_posts(ig_user_id, since, until_)
        data["instagram"] = merge_posts(data["instagram"], ig_new)
    except Exception as e:
        print(f"  ERRO Instagram posts: {e}", file=sys.stderr)

    data["atualizado"] = today.isoformat()
    save_posts(data)


if __name__ == "__main__":
    main()
