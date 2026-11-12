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
import tempfile
import copy
import hashlib
import re
from contextlib import contextmanager, nullcontext
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from contextvars import ContextVar
import requests
from datetime import date, datetime

API_VERSION = "v21.0"
BASE_URL    = f"https://graph.facebook.com/{API_VERSION}"

PAGE_ID = os.environ.get("FB_PAGE_ID", "447748898661292")
TOKEN   = os.environ["FB_PAGE_ACCESS_TOKEN"]

POSTS_PATH = "dados/posts.json"

# Pausa entre chamadas de insights para evitar rate limit
INSIGHT_SLEEP = 0.3


class CollectionError(RuntimeError):
    """Only controlled categories are exposed; never raw API messages."""
    def __init__(self, category, http_status=None, meta_code=None, meta_subcode=None):
        self.category = category
        self.http_status = safe_error_number(http_status, http=True)
        self.meta_code = safe_error_number(meta_code)
        self.meta_subcode = safe_error_number(meta_subcode)
        super().__init__(category)


def safe_error_number(value, http=False):
    # Do not print strings, booleans, nested data or unbounded identifiers.
    lower, upper = (100, 599) if http else (0, 2147483647)
    return value if type(value) is int and lower <= value <= upper else None


def error_details(error):
    if not isinstance(error, CollectionError):
        return {}
    return {"http_status": error.http_status, "meta_code": error.meta_code,
            "meta_subcode": error.meta_subcode}


def response_error_details(response):
    # Diagnostic extraction only: it must not change error handling or retries.
    code = subcode = None
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code, subcode = error.get("code"), error.get("error_subcode")
    except Exception:
        pass
    return {"http_status": safe_error_number(response.status_code, http=True),
            "meta_code": safe_error_number(code), "meta_subcode": safe_error_number(subcode)}


# Reset at the start of each execution. Avoid repeated insight calls after a
# general authentication/service failure; listings still have to succeed.
INSIGHT_BLOCKED = {}
INCOMPATIBLE_COMBINATIONS = set()
WARNING_COUNTS = {}
REQUEST_COUNTS = {"requests": 0, "queries_avoided": 0}
RUN_FIELDS = {}
DEFAULT_MAX_REQUESTS = 2000
DEFAULT_MAX_PAGES = 50
MAX_REQUESTS = DEFAULT_MAX_REQUESTS
MAX_PAGES = DEFAULT_MAX_PAGES
WRITE_SESSION = ContextVar("posts_write_session", default=None)
WARNING_CONTEXT = ContextVar("warning_context", default=None)


def reset_execution_state():
    global MAX_REQUESTS, MAX_PAGES
    MAX_REQUESTS = DEFAULT_MAX_REQUESTS
    MAX_PAGES = DEFAULT_MAX_PAGES
    INSIGHT_BLOCKED.clear()
    INCOMPATIBLE_COMBINATIONS.clear()
    WARNING_COUNTS.clear()
    REQUEST_COUNTS.update(requests=0, queries_avoided=0)
    RUN_FIELDS.clear()


def insight_context(network, object_id, account_id=None, media_type=None, endpoint_family=None):
    family = endpoint_family or ("post/insights" if network == "facebook" else "media/insights")
    # Unknown account/type is object-scoped: do not assume equivalence.
    account = account_id or (PAGE_ID if network == "facebook" else ("unknown_account", object_id))
    media = media_type if isinstance(media_type, str) and media_type else ("unknown_media", object_id)
    return (network, account, API_VERSION, family, media)


def execution_summary():
    return {**REQUEST_COUNTS,
            "warnings_repeated": sum(record["count"] - 1 for record in WARNING_COUNTS.values()),
            "fields_affected": sum(counts["unavailable"] for counts in RUN_FIELDS.values())}


def print_execution_summary():
    for record in WARNING_COUNTS.values():
        if record["count"] > 1:
            # Only controlled labels and counts; context keys/IDs stay in memory.
            print(f"rede={record['network']} etapa={record['stage']} metrica={record['metric']} "
                  f"categoria={record['category']} ocorrencias={record['count']} "
                  f"avisos_repetidos={record['count'] - 1}")
    stats = execution_summary()
    print(f"requisicoes_realizadas={stats['requests']} consultas_evitadas={stats['queries_avoided']} "
          f"avisos_repetidos={stats['warnings_repeated']} campos_afetados={stats['fields_affected']}")
    print("Requisicoes incluem retries; campos afetados sao campos indisponiveis nos posts processados.")


def log_issue(network, stage, metric, category, error=None, fatal=False, context=None):
    if not fatal:
        context = context or WARNING_CONTEXT.get() or (network, PAGE_ID, API_VERSION, "collection", "unknown")
        # Warnings may share the unknown-media cohort; compatibility decisions
        # remain object-scoped when the media type is unknown.
        if isinstance(context[-1], tuple) and context[-1][0] == "unknown_media":
            context = context[:-1] + ("unknown_media",)
        key = (context, stage, metric, category)
        if key in WARNING_COUNTS:
            WARNING_COUNTS[key]["count"] += 1
            return
        WARNING_COUNTS[key] = {"count": 1, "network": network, "stage": stage,
                               "metric": metric, "category": category}
    details = error_details(error)
    http = details.get("http_status")
    code = details.get("meta_code")
    subcode = details.get("meta_subcode")
    print(f"rede={network} etapa={stage} metrica={metric} categoria={category} "
          f"http={http if http is not None else '-'} "
          f"meta_code={code if code is not None else '-'} "
          f"meta_subcode={subcode if subcode is not None else '-'}", file=sys.stderr)


def api_get(url, params, retries=3):
    """Bounded retries; API messages are used for classification only."""
    for attempt in range(retries):
        try:
            if REQUEST_COUNTS["requests"] >= MAX_REQUESTS:
                raise CollectionError("request_limit")
            REQUEST_COUNTS["requests"] += 1
            r = requests.get(url, params=params, timeout=45, allow_redirects=False)
            if 300 <= r.status_code < 400:
                raise CollectionError("redirect_rejected", http_status=r.status_code)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries - 1:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise CollectionError("rate_limit" if r.status_code == 429 else "service",
                                      **response_error_details(r))
            try:
                data = r.json()
            except ValueError:
                raise CollectionError("invalid_response", http_status=r.status_code) from None
            if not isinstance(data, dict):
                raise CollectionError("invalid_response", http_status=r.status_code)
            error = data.get("error")
            if error is not None or not r.ok:
                error = error if isinstance(error, dict) else {}
                code = error.get("code")
                message = error.get("message")
                message = message.lower() if isinstance(message, str) else ""
                if code == 190 or r.status_code == 401:
                    category = "authentication"
                elif code in (10, 200, 294) or r.status_code == 403:
                    category = "permission"
                elif code in (4, 17, 32, 613):
                    category = "rate_limit"
                elif code in (1, 2) or error.get("is_transient") is True:
                    category = "service"
                elif code == 100 and "metric" in message and any(
                        word in message for word in ("invalid", "valid", "support", "available", "deprecated")):
                    category = "metric_incompatible"
                else:
                    category = "api_error"
                if category in ("rate_limit", "service") and attempt < retries - 1:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise CollectionError(category, http_status=r.status_code,
                                      meta_code=code, meta_subcode=error.get("error_subcode"))
            return data
        except (requests.ConnectionError, requests.Timeout):
            if attempt < retries - 1:
                time.sleep(5)
                continue
            raise CollectionError("connection") from None
        except requests.RequestException:
            raise CollectionError("request") from None
    raise CollectionError("service")


def confirmed_count(value):
    return type(value) is int and value >= 0


def response_items(data):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise CollectionError("invalid_listing")
    if any(not isinstance(item, dict) for item in data["data"]):
        raise CollectionError("invalid_listing")
    return data["data"]


def next_page(data):
    paging = data.get("paging", {})
    if not isinstance(paging, dict):
        raise CollectionError("invalid_pagination")
    value = paging.get("next")
    if value is not None and (not isinstance(value, str) or not value):
        raise CollectionError("invalid_pagination")
    return value


def validate_insights_response(data):
    """Insights are optional; their pagination never certifies the inventory."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise CollectionError("insights_invalid_structure")
    paging = data.get("paging", {})
    if not isinstance(paging, dict):
        raise CollectionError("insights_invalid_structure")
    following = paging.get("next")
    if following is not None:
        if not isinstance(following, str) or not following.strip():
            raise CollectionError("insights_invalid_structure")
        # Do not consume even apparently valid metrics from an unresolved page.
        raise CollectionError("insights_pagination_unresolved")


def parse_insights(data, names, network, reactions=False):
    result = {}
    seen = set()
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise CollectionError("insights_invalid_structure")
    for entry in data["data"]:
        if not isinstance(entry, dict):
            log_issue(network, "insights", "-", "invalid_entry")
            continue
        name = entry.get("name")
        if name not in names:
            continue
        if name in seen:
            result.pop(name, None)
            log_issue(network, "insights", name, "duplicate")
            continue
        seen.add(name)
        if "values" in entry:
            values = entry["values"]
            value = (values[0].get("value") if isinstance(values, list)
                     and len(values) == 1 and isinstance(values[0], dict) else None)
        else:
            value = entry.get("value")
        if reactions and name == "post_reactions_by_type_total" and isinstance(value, dict):
            value = sum(value.values()) if value and all(confirmed_count(v) for v in value.values()) else None
        if confirmed_count(value):
            result[name] = value
        else:
            log_issue(network, "insights", name, "missing_or_invalid")
    for name in names:
        if name not in result:
            log_issue(network, "insights", name, "unavailable")
    return result


def fetch_insights(object_id, names, reactions=False, account_id=None, media_type=None, endpoint_family=None):
    network = "facebook" if reactions else "instagram"
    context = insight_context(network, object_id, account_id, media_type, endpoint_family)
    token = WARNING_CONTEXT.set(context)
    try:
        return _fetch_insights(object_id, names, reactions, network, context)
    finally:
        WARNING_CONTEXT.reset(token)


def _fetch_insights(object_id, names, reactions, network, context):
    if network in INSIGHT_BLOCKED:
        for name in names:
            log_issue(network, "insights", name, INSIGHT_BLOCKED[network])
        return {}

    def query(requested):
        time.sleep(INSIGHT_SLEEP)
        try:
            data = api_get(f"{BASE_URL}/{object_id}/insights",
                           {"metric": ",".join(requested), "access_token": TOKEN})
        except CollectionError as error:
            if error.category == "invalid_response":
                raise CollectionError("insights_invalid_structure", **error_details(error)) from None
            raise
        validate_insights_response(data)
        return parse_insights(data, requested, network, reactions)

    general = {"authentication", "permission", "service", "rate_limit", "connection", "request"}
    optional_errors = general | {"metric_incompatible", "api_error",
                                 "insights_invalid_structure", "insights_pagination_unresolved"}
    combination = (context, frozenset(names))
    if len(names) > 1 and combination in INCOMPATIBLE_COMBINATIONS:
        REQUEST_COUNTS["queries_avoided"] += 1
    else:
        try:
            return query(names)
        except CollectionError as error:
            if error.category not in optional_errors:
                raise
            for name in names:
                log_issue(network, "insights", name, error.category, error=error)
            if error.category in general:
                INSIGHT_BLOCKED[network] = error.category
            # Cache only a classified incompatibility of a joint query.
            if error.category != "metric_incompatible" or len(names) == 1:
                return {}
            INCOMPATIBLE_COMBINATIONS.add(combination)

    result = {}
    # At most one individual query per requested metric; each keeps API retries.
    for name in names:
        try:
            result.update(query((name,)))
        except CollectionError as error:
            log_issue(network, "insights_individual", name, error.category, error=error)
            if error.category not in optional_errors:
                raise
            if error.category in general:
                INSIGHT_BLOCKED[network] = error.category
                break
    return result


def valid_date(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def local_zone():
    try:
        return ZoneInfo("America/Sao_Paulo")
    except ZoneInfoNotFoundError:
        raise CollectionError("timezone_unavailable") from None


def publication_date(value, network):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})", value):
        log_issue(network, "listing", "data", "missing_or_invalid")
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(local_zone()).date().isoformat()
    except (ValueError, OverflowError):
        log_issue(network, "listing", "data", "missing_or_invalid")
        return None


def publication_id(item):
    value = item.get("id")
    if not isinstance(value, str) or not value.strip():
        raise CollectionError("invalid_publication_id")
    return value


def fetch_listing_page(url, params, network):
    try:
        data = api_get(url, params)
        response_items(data)
        next_page(data)
        return data
    except CollectionError as error:
        log_issue(network, "listing" if params else "pagination", "-", error.category, error=error, fatal=True)
        raise


def put_count(post, key, value, network="unknown", context=None):
    if confirmed_count(value):
        post[key] = value
    else:
        log_issue(network, "listing_fields", key, "missing_or_invalid", context=context)


def put_text(post, key, value, truncate=False):
    if isinstance(value, str) and value.strip():
        post[key] = value[:120].strip() if truncate else value


def nested_count(item, *keys):
    for key in keys:
        if not isinstance(item, dict):
            return None
        item = item.get(key)
    return item


def get_ig_user_id():
    try:
        data = api_get(f"{BASE_URL}/{PAGE_ID}",
                       {"fields": "instagram_business_account", "access_token": TOKEN})
        ig = data.get("instagram_business_account")
        if not isinstance(ig, dict) or not isinstance(ig.get("id"), str) or not ig["id"].strip():
            raise CollectionError("invalid_account")
        return ig["id"]
    except CollectionError as error:
        log_issue("instagram", "account", "-", error.category, error=error, fatal=True)
        raise


# ===== FACEBOOK =====

def fetch_fb_post_insights(post_id, media_type=None):
    return fetch_insights(post_id, (
        "post_impressions", "post_impressions_unique", "post_engaged_users",
        "post_reactions_by_type_total"), reactions=True, account_id=PAGE_ID, media_type=media_type)


def fetch_fb_posts(since_date, until_date, skip_insights=False):
    """Coleta publicações do Facebook num intervalo de datas."""
    posts = []
    url = f"{BASE_URL}/{PAGE_ID}/published_posts"
    params = {
        # Validated by the read-only diagnostic. type and comments summary are
        # incompatible for this account; do not request or synthesize them.
        "fields": "id,created_time,message,permalink_url,shares",
        "since": since_date,
        "until": until_date,
        "access_token": TOKEN,
        "limit": 100,
    }

    page_num = 0
    visited = set()
    while url:
        if url in visited:
            raise CollectionError("invalid_pagination")
        visited.add(url)
        if page_num >= MAX_PAGES:
            raise CollectionError("page_limit")
        page_num += 1
        data = fetch_listing_page(url, params, "facebook")
        items = response_items(data)
        print(f"  FB página {page_num}: {len(items)} posts...")

        for item in items:
            publication_id(item)
            context = insight_context("facebook", item["id"], PAGE_ID, item.get("type"))
            post_date = publication_date(item.get("created_time"), "facebook")

            if skip_insights:
                insights = {}
            else:
                insights = fetch_fb_post_insights(item["id"], media_type=item.get("type"))

            post = {"id": item["id"]}
            put_text(post, "data", post_date)
            # Optional only: absent fields must preserve historical tipo/com.
            put_text(post, "tipo", item.get("type"))
            put_text(post, "txt", item.get("message"), truncate=True)
            put_text(post, "url", item.get("permalink_url"))
            for key, name in (("vis", "post_impressions"), ("alc", "post_impressions_unique"),
                              ("eng", "post_engaged_users"), ("reac", "post_reactions_by_type_total")):
                if name in insights:
                    put_count(post, key, insights[name], "facebook" if name.startswith("post_") else "instagram")
            put_count(post, "comp", nested_count(item, "shares", "count"), "facebook", context=context)
            put_count(post, "com", nested_count(item, "comments", "summary", "total_count"), "facebook", context=context)
            posts.append(post)

        url    = next_page(data)
        params = {}

    print(f"  FB total: {len(posts)} posts coletados")
    return posts


# ===== INSTAGRAM =====

def fetch_ig_media_insights(media_id, account_id=None, media_type=None):
    result = fetch_insights(media_id, ("impressions", "reach", "saved"),
                            account_id=account_id, media_type=media_type)
    # Independent metric: never substitute views for legacy impressions.
    # fetch_insights retains the existing general-failure suspension policy.
    result.update(fetch_insights(media_id, ("views",),
                                 account_id=account_id, media_type=media_type))
    return result


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
    visited = set()
    while url:
        if url in visited:
            raise CollectionError("invalid_pagination")
        visited.add(url)
        if page_num >= MAX_PAGES:
            raise CollectionError("page_limit")
        page_num += 1
        data = fetch_listing_page(url, params, "instagram")
        items = response_items(data)
        print(f"  IG página {page_num}: {len(items)} mídias...")

        for item in items:
            publication_id(item)
            if item.get("media_type") == "STORY":
                continue

            context = insight_context("instagram", item["id"], ig_user_id, item.get("media_type"))
            post_date = publication_date(item.get("timestamp"), "instagram")

            if skip_insights:
                insights = {}
            else:
                insights = fetch_ig_media_insights(item["id"], account_id=ig_user_id, media_type=item.get("media_type"))

            post = {"id": item["id"]}
            put_text(post, "data", post_date)
            put_text(post, "tipo", item.get("media_type"))
            put_text(post, "txt", item.get("caption"), truncate=True)
            put_text(post, "url", item.get("permalink"))
            for key, name in (("vis", "impressions"), ("alc", "reach"), ("salv", "saved"), ("views", "views")):
                if name in insights:
                    put_count(post, key, insights[name], "facebook" if name.startswith("post_") else "instagram")
            put_count(post, "curt", item.get("like_count"), "instagram", context=context)
            put_count(post, "com", item.get("comments_count"), "instagram", context=context)
            posts.append(post)

        url    = next_page(data)
        params = {}

    # Remove stories (podem ter passado no filtro de tipo)
    posts = [p for p in posts if p.get("tipo") != "STORY"]
    print(f"  IG total: {len(posts)} posts coletados")
    return posts


# ===== ARQUIVO posts.json =====

def validate_document(data):
    """Strict known fields; unknown JSON fields are retained without reinterpretation."""
    if not isinstance(data, dict) or not {"atualizado", "facebook", "instagram"} <= data.keys():
        raise CollectionError("invalid_document")
    if data["atualizado"] != "" and not valid_date(data["atualizado"]):
        raise CollectionError("invalid_document")
    numeric = {"vis", "views", "alc", "eng", "reac", "curt", "com", "comp", "salv"}
    for network in ("facebook", "instagram"):
        if not isinstance(data[network], list):
            raise CollectionError("invalid_document")
        seen = set()
        for post in data[network]:
            if (not isinstance(post, dict) or not isinstance(post.get("id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", post["id"])
                    or post["id"] in seen or not valid_date(post.get("data"))):
                raise CollectionError("invalid_document")
            seen.add(post["id"])
            if any(key in post and not confirmed_count(post[key]) for key in numeric):
                raise CollectionError("invalid_document")
            if any(key in post and not isinstance(post[key], str) for key in ("tipo", "txt", "url")):
                raise CollectionError("invalid_document")
    # This also rejects non-finite numbers inside unknown fields, without deleting them.
    try:
        json.dumps(data, allow_nan=False)
    except (TypeError, ValueError):
        raise CollectionError("invalid_document") from None
    return data


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CollectionError("invalid_document")
        result[key] = value
    return result


def file_revision(path):
    try:
        with open(path, "rb") as file:
            raw = file.read()
            stat = os.fstat(file.fileno())
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
                hashlib.sha256(raw).digest())
    except FileNotFoundError:
        return None


@contextmanager
def writer_session():
    """Fail closed; an interrupted process may leave a lock for manual review."""
    path = os.path.abspath(POSTS_PATH)
    lock = path + ".lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise CollectionError("locked") from None
    except OSError:
        raise CollectionError("lock_unavailable") from None
    identity = os.fstat(descriptor)
    token = None
    try:
        token = WRITE_SESSION.set((path, file_revision(path)))
        yield
    finally:
        if token is not None:
            WRITE_SESSION.reset(token)
        os.close(descriptor)
        try:
            current = os.stat(lock)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                os.unlink(lock)
        except FileNotFoundError:
            pass


def check_revision(path):
    session = WRITE_SESSION.get()
    if session is None or session[0] != path or session[1] != file_revision(path):
        raise CollectionError("concurrent_change")


def load_posts():
    try:
        with open(POSTS_PATH, "r", encoding="utf-8") as file:
            data = json.load(file, object_pairs_hook=unique_object)
    except FileNotFoundError:
        data = {"atualizado": "", "facebook": [], "instagram": []}
    except (ValueError, UnicodeError):
        raise CollectionError("invalid_document") from None
    return validate_document(data)


def merge_posts(existing_list, new_posts, network="unknown", skip_insights=False, summary=None):
    """Mescla campos confirmados numa copia, preservando o historico."""
    merged = copy.deepcopy(existing_list)
    idx = {p["id"]: i for i, p in enumerate(merged)}
    numeric = {"vis", "alc", "eng", "reac", "com", "comp", "curt", "salv", "views"}
    counts = {"updated": 0, "preserved": 0, "unavailable": 0}
    expected = ({"vis", "alc", "eng", "reac", "com", "comp"} if network == "facebook"
                else {"vis", "alc", "curt", "com", "salv", "views"}) | {"data"}
    if skip_insights:
        expected -= {"vis", "alc", "eng", "reac", "salv", "views"}
    for post in new_posts:
        pid = post.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise CollectionError("invalid_publication_id")
        fields = {k: v for k, v in post.items()
                  if (confirmed_count(v) if k in numeric else isinstance(v, str) and bool(v.strip()))}
        if "data" in fields and not valid_date(fields["data"]):
            fields.pop("data")
            log_issue(network, "merge", "data", "invalid_date")
        counts["updated"] += len(set(fields) - {"id"})
        counts["unavailable"] += len(expected - set(fields))
        if pid in idx:
            counts["preserved"] += len(set(merged[idx[pid]]) - set(fields) - {"id"})
            previous_date = merged[idx[pid]].get("data")
            if "data" in fields and valid_date(previous_date) and fields["data"] != previous_date:
                fields.pop("data")
                counts["updated"] -= 1
                counts["preserved"] += 1
                log_issue(network, "merge", "data", "historical_date_conflict")
            merged[idx[pid]].update(fields)
        else:
            if "data" not in fields:
                raise CollectionError("new_publication_without_date")
            idx[pid] = len(merged)
            merged.append(fields)
    merged.sort(key=lambda p: p.get("data", ""), reverse=True)
    if summary is not None:
        summary[network] = counts
    return merged


def save_posts(data):
    """Serializa antes de escrever e substitui atomicamente no mesmo diretorio."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    validate_document(data)
    destination = os.path.abspath(POSTS_PATH)
    if WRITE_SESSION.get() is None:
        with writer_session():
            load_posts()  # Validate existing storage before a standalone save.
            return save_posts(data)
    check_revision(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=os.path.dirname(destination),
                                         prefix=".posts-", suffix=".tmp", delete=False) as f:
            temporary = f.name
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        check_revision(destination)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    print("posts.json salvo com sucesso")


# ===== MAIN =====

def diagnostic_summary(network, collected, existing, counts):
    """Read-only diagnostics: only counts and fixed metric names, no raw posts."""
    metric_map = ({"vis": "post_impressions", "alc": "post_impressions_unique",
                   "eng": "post_engaged_users", "reac": "post_reactions_by_type_total",
                   "com": "comments", "comp": "shares"} if network == "facebook" else
                  {"vis": "impressions", "alc": "reach", "salv": "saved", "views": "views",
                   "curt": "like_count", "com": "comments_count"})
    old = {post["id"]: post for post in existing}
    # Report each publication once, consistent with the ID-based merge.
    posts = {post["id"]: post for post in collected}
    metrics = {}
    for key, source in metric_map.items():
        confirmed = [post[key] for post in posts.values() if confirmed_count(post.get(key))]
        unavailable = sum(not confirmed_count(post.get(key)) for post in posts.values())
        preserved = sum(not confirmed_count(post.get(key)) and
                        confirmed_count(old.get(pid, {}).get(key)) for pid, post in posts.items())
        metrics[key] = {"source": source, "confirmed": len(confirmed),
                        "unavailable": unavailable, "preserved": preserved,
                        "zero": confirmed.count(0), "positive": sum(v > 0 for v in confirmed)}
    report = {"publications": len(posts), "fields": dict(counts), "metrics": metrics}
    print(f"[dry-run] rede={network} publicacoes={len(posts)} "
          f"metricas_confirmadas={sum(m['confirmed'] for m in metrics.values())} "
          f"metricas_indisponiveis={sum(m['unavailable'] for m in metrics.values())} "
          f"metricas_preservadas={sum(m['preserved'] for m in metrics.values())}")
    for key, metric in metrics.items():
        print(f"[dry-run] rede={network} campo={key} origem={metric['source']} "
              f"confirmadas={metric['confirmed']} indisponiveis={metric['unavailable']} "
              f"preservadas={metric['preserved']} zeros={metric['zero']} positivos={metric['positive']}")
    return report


def positive_limit(value):
    try:
        number = int(value)
        if str(number) != value or number < 1:
            raise ValueError
        return number
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("Expected a positive integer.") from None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Backfill de publicações individuais")
    parser.add_argument(
        "--start", required=True,
        help="Data inicial (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end", default=None,
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
    parser.add_argument("--dry-run", action="store_true",
                        help="Consulta a API real no intervalo solicitado, sem gravar arquivos.")
    parser.add_argument("--max-requests", type=positive_limit, default=DEFAULT_MAX_REQUESTS,
                        help="Global GET budget including retries (default: 2000).")
    parser.add_argument("--max-pages", type=positive_limit, default=DEFAULT_MAX_PAGES,
                        help="Maximum listing pages per network (default: 50).")
    args = parser.parse_args(argv)
    if args.end is None:
        args.end = datetime.now(local_zone()).date().isoformat()
    if not valid_date(args.start) or not valid_date(args.end) or args.start > args.end:
        parser.error("Invalid date interval; use ordered YYYY-MM-DD dates.")
    return args


def _main(argv=None, args=None):
    args = parse_args(argv) if args is None else args
    since  = args.start
    until_ = args.end

    print(f"Backfill de publicações: {since} → {until_}")
    if args.skip_insights:
        print("  (modo sem insights — apenas campos básicos)")
    print()

    if args.dry_run:
        print("[dry-run] Consultas reais; nenhuma gravacao. Merge e preservacao simulados em memoria.")
    summary = RUN_FIELDS
    diagnostics = {}
    data = validate_document(load_posts())

    print("Facebook:")
    try:
        fb_new = fetch_fb_posts(since, until_, skip_insights=args.skip_insights)
        previous = data["facebook"]
        data["facebook"] = merge_posts(data["facebook"], fb_new, "facebook", args.skip_insights, summary)
        if args.dry_run:
            diagnostics["facebook"] = diagnostic_summary("facebook", fb_new, previous, summary["facebook"])
    except Exception as error:
        category = error.category if isinstance(error, CollectionError) else "internal"
        log_issue("facebook", "collection", "-", category, error=error, fatal=True)
        raise CollectionError(category, **error_details(error)) from None

    if not args.skip_instagram:
        print("\nInstagram:")
        try:
            ig_user_id = get_ig_user_id()
            ig_new = fetch_ig_posts(ig_user_id, since, until_,
                                    skip_insights=args.skip_insights)
            previous = data["instagram"]
            data["instagram"] = merge_posts(data["instagram"], ig_new, "instagram", args.skip_insights, summary)
            if args.dry_run:
                diagnostics["instagram"] = diagnostic_summary("instagram", ig_new, previous, summary["instagram"])
        except Exception as error:
            category = error.category if isinstance(error, CollectionError) else "internal"
            log_issue("instagram", "collection", "-", category, error=error, fatal=True)
            raise CollectionError(category, **error_details(error)) from None

    if not args.dry_run:
        data["atualizado"] = datetime.now(local_zone()).date().isoformat()
        try:
            save_posts(data)
        except CollectionError as error:
            log_issue("all", "write", "-", error.category, fatal=True)
            raise
        except Exception:
            log_issue("all", "write", "-", "storage", fatal=True)
            raise CollectionError("storage") from None
    result = {"status": "partial" if any(c["unavailable"] for c in summary.values()) else "complete",
              "fields": summary,
              "updated_means": "inventory_and_available_fields_not_all_insights"}
    if args.dry_run:
        result["dry_run"] = True
        result["diagnostics"] = diagnostics
        print("[dry-run] Nenhum arquivo ou data de atualizacao foi gravado.")
    for network, counts in summary.items():
        print(f"rede={network} etapa=result updated={counts['updated']} "
              f"preserved={counts['preserved']} unavailable={counts['unavailable']}")
    print("Contadores referem-se aos posts retornados; preservados e indisponiveis podem se sobrepor.")
    print("atualizado indica inventario/campos disponiveis; nao confirma renovacao de todos os insights.")
    return result


def main(argv=None):
    global MAX_REQUESTS, MAX_PAGES
    started = False
    try:
        args = parse_args(argv)
        local_zone()  # Fail before any API call if timezone data is unavailable.
        with nullcontext() if args.dry_run else writer_session():
            reset_execution_state()
            started = True
            MAX_REQUESTS = getattr(args, "max_requests", DEFAULT_MAX_REQUESTS)
            MAX_PAGES = getattr(args, "max_pages", DEFAULT_MAX_PAGES)
            result = _main(argv, args=args)
            result["fields"] = copy.deepcopy(result["fields"])
            result["operations"] = execution_summary()
        return result
    finally:
        if started:
            print_execution_summary()


def run():
    try:
        main()
    except Exception as error:
        category = error.category if isinstance(error, CollectionError) else "internal"
        log_issue("all", "execution", "-", category, error=error, fatal=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
