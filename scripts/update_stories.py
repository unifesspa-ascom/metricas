#!/usr/bin/env python3
"""Manual Instagram story observations; no scheduler is installed by this file.

Only dados/stories.json is written, with a transient adjacent writer lock.
Exit 0: complete observation, 2: persisted partial/failed observation,
1: configuration/internal/storage failure.
An empty, exhausted listing is not proof of zero daily production. Snapshots
are append-only; latest_confirmed selects a value, never adds observations.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests.adapters import HTTPAdapter


API_VERSION = "v21.0"
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"
DEFAULT_PAGE_ID = "447748898661292"
STORIES_PATH = Path(__file__).resolve().parents[1] / "dados" / "stories.json"
LOCAL_ZONE = "America/Sao_Paulo"
METRICS = ("views", "reach")
PAGE_SIZE = 100
DEFAULT_REQUESTS = 100
HARD_MAX_REQUESTS = 500
DEFAULT_PAGES = 10
HARD_MAX_PAGES = 50
TIMEOUT = (5, 20)
GENERAL_FAILURES = {"authentication", "permission", "service", "connection", "redirect_rejected"}
STATUSES = {"confirmed", "unavailable", "invalid"}
CATEGORIES = {"authentication", "permission", "service", "connection", "redirect_rejected",
              "api_error", "metric_incompatible", "invalid_response", "invalid_listing",
              "invalid_pagination", "pagination_cycle", "page_limit", "budget_exhausted",
              "invalid_timestamp", "conflicting_timestamp", "invalid_id", "invalid_account",
              "account_mismatch", "empty_response", "invalid_structure", "invalid_value",
              "unexpected_period", "insights_pagination_unresolved", "network_stopped",
              "storage", "concurrent_change", "locked", "invalid_document", "timezone_unavailable",
              "invalid_environment", "internal"}
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})")


class Problem(Exception):
    """Controlled error, never carrying URLs, payloads or raw exceptions."""
    def __init__(self, category, http=None, code=None, subcode=None):
        self.category = category if category in CATEGORIES else "internal"
        self.http = safe_number(http, http=True)
        self.code = safe_number(code)
        self.subcode = safe_number(subcode)
        super().__init__(self.category)


def safe_number(value, http=False):
    low, high = (100, 599) if http else (0, 2147483647)
    return value if type(value) is int and low <= value <= high else None


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]{1,64}", value) is not None


def count(value):
    return type(value) is int and value >= 0


def parse_stamp(value):
    if not isinstance(value, str) or STAMP.fullmatch(value) is None:
        raise Problem("invalid_timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise Problem("invalid_timestamp") from None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def local_date(timestamp, zone):
    return parse_stamp(timestamp).astimezone(zone).date().isoformat()


class Issues:
    def __init__(self):
        self.records = {}

    def add(self, stage, metric, problem):
        # Labels are supplied by code, never derived from response strings.
        key = (stage, metric, problem.category, problem.http, problem.code, problem.subcode)
        if key in self.records:
            self.records[key]["count"] += 1
            return
        record = {"stage": stage, "metric": metric, "category": problem.category,
                  "http": problem.http, "meta_code": problem.code,
                  "meta_subcode": problem.subcode, "count": 1}
        self.records[key] = record
        print(f"rede=instagram etapa={stage} metrica={metric} categoria={problem.category} "
              f"http={problem.http if problem.http is not None else '-'} "
              f"meta_code={problem.code if problem.code is not None else '-'} "
              f"meta_subcode={problem.subcode if problem.subcode is not None else '-'}")


def response_problem(status, payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    info = error if isinstance(error, dict) else {}
    code = safe_number(info.get("code"))
    message = info.get("message")
    message = message.lower() if isinstance(message, str) else ""
    if status == 401 or code in (102, 190):
        category = "authentication"
    elif status == 403 or code in (10, 200, 294):
        category = "permission"
    elif status == 429 or (status is not None and status >= 500) or code in (1, 2, 4, 17, 32, 613) or info.get("is_transient") is True:
        category = "service"
    elif status is not None and 300 <= status < 400:
        category = "redirect_rejected"
    elif code == 100 and "metric" in message and any(word in message for word in ("invalid", "support", "available", "deprecated")):
        category = "metric_incompatible"
    elif not isinstance(payload, dict):
        category = "invalid_response"
    elif "error" in payload or status is None or not 200 <= status < 300:
        category = "api_error"
    else:
        return None
    return Problem(category, status, code, info.get("error_subcode"))


class Client:
    def __init__(self, session, token, max_requests):
        if type(max_requests) is not int or not 1 <= max_requests <= HARD_MAX_REQUESTS:
            raise Problem("invalid_environment")
        self.session = session
        self._token = token
        self.max_requests = max_requests
        self.used = 0
        self.stopped = False

    def get(self, path, params):
        if self.used >= self.max_requests:
            raise Problem("budget_exhausted")
        if self.stopped:
            raise Problem("network_stopped")
        self.used += 1
        try:
            with self.session.get(BASE_URL + "/" + path,
                                  params={**params, "access_token": self._token},
                                  timeout=TIMEOUT, allow_redirects=False) as response:
                status = safe_number(response.status_code, http=True)
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
        except requests.RequestException:
            self.stopped = True
            raise Problem("connection") from None
        problem = response_problem(status, payload)
        if problem:
            if problem.category in GENERAL_FAILURES:
                self.stopped = True
            raise problem
        return payload


def next_cursor(payload, path):
    paging = payload.get("paging", {})
    if not isinstance(paging, dict):
        raise Problem("invalid_pagination")
    following = paging.get("next")
    if following is None:
        return None
    try:
        if not isinstance(following, str) or not following.strip():
            raise ValueError()
        url = urlsplit(following)
        if (url.scheme != "https" or url.netloc != "graph.facebook.com"
                or url.path != f"/{API_VERSION}/{path}" or url.fragment):
            raise ValueError()
        query = parse_qs(url.query, keep_blank_values=True)
        after = query.get("after")
        if after is None:
            cursors = paging.get("cursors")
            after = [cursors.get("after")] if isinstance(cursors, dict) else None
        if not after or len(after) != 1 or not isinstance(after[0], str) or not after[0].strip():
            raise ValueError()
        return after[0]
    except (ValueError, TypeError):
        raise Problem("invalid_pagination") from None


def parse_metric(payload, metric):
    entries = payload.get("data")
    paging = payload.get("paging", {})
    if not isinstance(entries, list) or any(not isinstance(x, dict) for x in entries) or not isinstance(paging, dict):
        raise Problem("invalid_structure")
    if paging.get("next") is not None:
        following = paging["next"]
        if not isinstance(following, str) or not following.strip():
            raise Problem("invalid_pagination")
        raise Problem("insights_pagination_unresolved")
    if not entries:
        raise Problem("empty_response")
    matches = [x for x in entries if x.get("name") == metric]
    if len(matches) != 1:
        raise Problem("invalid_structure")
    entry = matches[0]
    if "period" in entry and entry["period"] != "lifetime":
        raise Problem("unexpected_period")
    shapes = [key for key in ("values", "value", "total_value") if key in entry]
    if len(shapes) != 1:
        raise Problem("invalid_structure")
    shape = shapes[0]
    if shape == "value":
        value = entry[shape]
    elif shape == "total_value":
        if not isinstance(entry[shape], dict) or "value" not in entry[shape]:
            raise Problem("invalid_structure")
        value = entry[shape]["value"]
    else:
        values = entry[shape]
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict) or "value" not in values[0]:
            raise Problem("invalid_structure")
        value = values[0]["value"]
    if not count(value):
        raise Problem("invalid_value")
    return {"status": "confirmed", "value": value, "api_metric": metric,
            "api_version": API_VERSION, "response_structure": shape,
            "period": entry.get("period")}


def empty_document():
    return {"schema_version": 1,
            "source": {"provider": "meta_graph_api", "api_version": API_VERSION,
                       "inventory_endpoint": "/{ig-user-id}/stories", "account_fingerprint": None},
            "timezone": LOCAL_ZONE, "stories": {}, "runs": []}


def latest_confirmed(story, metric):
    """Select evidence rather than sum it; a later confirmed decrease is valid."""
    latest = None
    for snapshot in story["snapshots"]:
        item = snapshot["metrics"].get(metric)
        if item and item["status"] == "confirmed":
            if latest is None or parse_stamp(item["checked_at"]) >= parse_stamp(latest["checked_at"]):
                latest = item
    return copy.deepcopy(latest)


def append_snapshot(story, snapshot):
    """Single append boundary for future *explicit* compaction; no pruning now."""
    story["snapshots"].append(copy.deepcopy(snapshot))


def validate_document(data, zone):
    """Fail closed on damaged/unsupported ledgers instead of rebuilding them."""
    try:
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1
                or data.get("timezone") != LOCAL_ZONE or not isinstance(data.get("source"), dict)
                or not isinstance(data.get("stories"), dict) or not isinstance(data.get("runs"), list)):
            raise ValueError()
        source = data["source"]
        if source.get("provider") != "meta_graph_api" or source.get("inventory_endpoint") != "/{ig-user-id}/stories" or source.get("api_version") != API_VERSION:
            raise ValueError()
        fingerprint = source.get("account_fingerprint")
        if fingerprint is not None and (not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None):
            raise ValueError()
        if data["stories"] and fingerprint is None:
            raise ValueError()
        runs = {}
        for run in data["runs"]:
            if not isinstance(run, dict) or not isinstance(run.get("id"), str) or re.fullmatch(r"[0-9a-f]{32}", run["id"]) is None or run["id"] in runs:
                raise ValueError()
            if parse_stamp(run["finished_at"]) < parse_stamp(run["started_at"]):
                raise ValueError()
            if run["status"] not in ("complete", "partial", "failed") or run["listing_status"] not in ("complete", "partial", "failed", "not_started"):
                raise ValueError()
            if (run.get("api_version") != API_VERSION
                    or run.get("insights_status") not in ("complete", "partial", "not_needed")
                    or run.get("pagination_status") not in ({"not_started", "pending", "exhausted"} | CATEGORIES)):
                raise ValueError()
            for name in ("pages_received", "distinct_stories_observed", "duplicates", "invalid_records", "conflicting_timestamps", "requests_used"):
                if not count(run.get(name)):
                    raise ValueError()
            if (type(run.get("request_limit")) is not int or not 1 <= run["request_limit"] <= HARD_MAX_REQUESTS
                    or run["requests_used"] > run["request_limit"]
                    or type(run.get("page_limit")) is not int or not 1 <= run["page_limit"] <= HARD_MAX_PAGES
                    or run["pages_received"] > run["page_limit"]):
                raise ValueError()
            if run["listing_status"] == "complete" and (run["pagination_status"] != "exhausted" or run["invalid_records"] or run["conflicting_timestamps"] or not run["pages_received"]):
                raise ValueError()
            if run["status"] == "complete" and (run["listing_status"] != "complete" or run["insights_status"] == "partial"):
                raise ValueError()
            if run["status"] == "failed" and run["pages_received"]:
                raise ValueError()
            if not isinstance(run.get("issues"), list):
                raise ValueError()
            for issue in run["issues"]:
                if (not isinstance(issue, dict) or issue.get("category") not in CATEGORIES
                        or issue.get("stage") not in ("account", "listing", "insights")
                        or issue.get("metric") not in ("-", *METRICS)
                        or not count(issue.get("count")) or issue["count"] == 0):
                    raise ValueError()
                for name, http in (("http", True), ("meta_code", False), ("meta_subcode", False)):
                    if issue.get(name) is not None and safe_number(issue[name], http) != issue[name]:
                        raise ValueError()
            runs[run["id"]] = run
        for key, story in data["stories"].items():
            if not identifier(key) or not isinstance(story, dict) or story.get("id") != key:
                raise ValueError()
            published = parse_stamp(story["timestamp"])
            if parse_stamp(story["published_at"]) != published or story["publication_date_brt"] != published.astimezone(zone).date().isoformat():
                raise ValueError()
            if published > parse_stamp(story["first_seen_at"]) or parse_stamp(story["last_seen_at"]) < parse_stamp(story["first_seen_at"]):
                raise ValueError()
            if not isinstance(story["snapshots"], list):
                raise ValueError()
            seen_runs = set()
            for snapshot in story["snapshots"]:
                run_id = snapshot["run_id"]
                if run_id not in runs or run_id in seen_runs or snapshot["inventory_observation"] not in ("complete", "partial"):
                    raise ValueError()
                seen_runs.add(run_id)
                collected_at = parse_stamp(snapshot["collected_at"])
                if not parse_stamp(runs[run_id]["started_at"]) <= collected_at <= parse_stamp(runs[run_id]["finished_at"]):
                    raise ValueError()
                metrics = snapshot["metrics"]
                if not isinstance(metrics, dict) or set(metrics) != set(METRICS):
                    raise ValueError()
                for metric, item in metrics.items():
                    if not isinstance(item, dict) or item.get("status") not in STATUSES:
                        raise ValueError()
                    checked_at = parse_stamp(item["checked_at"])
                    if not parse_stamp(runs[run_id]["started_at"]) <= checked_at <= collected_at:
                        raise ValueError()
                    if item["status"] == "confirmed":
                        if not count(item.get("value")) or item.get("api_metric") != metric or item.get("api_version") != API_VERSION or item.get("response_structure") not in ("values", "value", "total_value") or item.get("period") not in (None, "lifetime"):
                            raise ValueError()
                    elif "value" in item or item.get("category") not in CATEGORIES:
                        raise ValueError()
        # Reject non-JSON numbers even in extension fields; preserve safe unknown fields.
        json.dumps(data, allow_nan=False)
    except (KeyError, TypeError, ValueError, OverflowError, Problem):
        raise Problem("invalid_document") from None


def strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError()


def load_document(path, zone):
    try:
        original = Path(path).read_bytes()
    except FileNotFoundError:
        return empty_document(), None
    try:
        data = json.loads(original.decode("utf-8"), object_pairs_hook=strict_pairs,
                          parse_constant=reject_constant)
    except (UnicodeError, ValueError):
        raise Problem("invalid_document") from None
    validate_document(data, zone)
    return data, original


@contextmanager
def exclusive_lock(path):
    lock = str(path) + ".lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Problem("locked") from None
    try:
        os.close(fd)
        yield
    finally:
        os.unlink(lock)


def save_document(path, data, original, zone):
    validate_document(data, zone)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=Path(path).parent, prefix=".stories-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            current = Path(path).read_bytes()
        except FileNotFoundError:
            current = None
        if current != original:
            raise Problem("concurrent_change")
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def collect(data, client, page_id, zone, max_pages):
    if type(max_pages) is not int or not 1 <= max_pages <= HARD_MAX_PAGES:
        raise Problem("invalid_environment")
    result = copy.deepcopy(data)
    issues = Issues()
    run = {"id": uuid4().hex, "api_version": API_VERSION, "started_at": utc_now(),
           "listing_status": "not_started", "pagination_status": "not_started",
           "pages_received": 0, "distinct_stories_observed": 0, "duplicates": 0,
           "invalid_records": 0, "conflicting_timestamps": 0,
           "request_limit": client.max_requests, "page_limit": max_pages}
    observations, conflicts = {}, set()
    try:
        account = client.get(page_id, {"fields": "instagram_business_account"})
        linked = account.get("instagram_business_account")
        user_id = linked.get("id") if isinstance(linked, dict) else None
        if not identifier(user_id):
            raise Problem("invalid_account")
        fingerprint = hashlib.sha256(("instagram:" + user_id).encode()).hexdigest()
        bound = result["source"]["account_fingerprint"]
        if bound is not None and bound != fingerprint:
            raise Problem("account_mismatch")
        result["source"]["account_fingerprint"] = fingerprint
    except Problem as problem:
        issues.add("account", "-", problem)
        user_id = None

    if user_id is not None:
        path, cursor, visited = user_id + "/stories", None, set()
        run["listing_status"] = "partial"
        for _ in range(max_pages):
            try:
                params = {"fields": "id,timestamp", "limit": PAGE_SIZE}
                if cursor is not None:
                    params["after"] = cursor
                payload = client.get(path, params)
                observed_at = utc_now()
                items = payload.get("data")
                if not isinstance(items, list) or len(items) > PAGE_SIZE:
                    raise Problem("invalid_listing")
                run["pages_received"] += 1
                for item in items:
                    if not isinstance(item, dict) or not identifier(item.get("id")):
                        run["invalid_records"] += 1
                        issues.add("listing", "-", Problem("invalid_id"))
                        continue
                    key = item["id"]
                    try:
                        published = parse_stamp(item.get("timestamp"))
                        if published > parse_stamp(observed_at):
                            raise Problem("invalid_timestamp")
                    except Problem as problem:
                        run["invalid_records"] += 1
                        issues.add("listing", "-", problem)
                        continue
                    existing = result["stories"].get(key)
                    previous = observations.get(key)
                    if previous:
                        run["duplicates"] += 1
                    if ((previous and previous["published_at"] != published.isoformat())
                            or (existing and parse_stamp(existing["published_at"]) != published)):
                        conflicts.add(key)
                        issues.add("listing", "-", Problem("conflicting_timestamp"))
                        continue
                    observations[key] = {"id": key, "timestamp": item["timestamp"],
                                         "published_at": published.isoformat(),
                                         "publication_date_brt": published.astimezone(zone).date().isoformat(),
                                         "first_observed_at": previous["first_observed_at"] if previous else observed_at,
                                         "observed_at": observed_at}
                following = next_cursor(payload, path)
                if following is None:
                    run["pagination_status"] = "exhausted"
                    break
                if following in visited:
                    raise Problem("pagination_cycle")
                visited.add(following)
                cursor = following
                run["pagination_status"] = "pending"
            except Problem as problem:
                run["pagination_status"] = problem.category
                issues.add("listing", "-", problem)
                break
        if run["pagination_status"] == "pending":
            issues.add("listing", "-", Problem("page_limit"))
        if run["pagination_status"] == "exhausted" and not run["invalid_records"] and not conflicts:
            run["listing_status"] = "complete"
        elif not run["pages_received"]:
            run["listing_status"] = "failed"

    run["conflicting_timestamps"] = len(conflicts)
    for key in conflicts:
        observations.pop(key, None)
    run["distinct_stories_observed"] = len(observations)
    # Discover all pages before enrichment; prioritize missing evidence, then older stories.
    ordered = sorted(observations, key=lambda key: (
        all(latest_confirmed(result["stories"].get(key, {"snapshots": []}), m) is not None for m in METRICS),
        observations[key]["published_at"], key))
    unavailable = 0
    for key in ordered:
        observation = observations[key]
        story = result["stories"].get(key)
        if story is None:
            story = {k: observation[k] for k in ("id", "timestamp", "published_at", "publication_date_brt")}
            story.update(first_seen_at=observation["first_observed_at"],
                         last_seen_at=observation["observed_at"], snapshots=[])
            result["stories"][key] = story
        else:
            story["last_seen_at"] = max(story["last_seen_at"], observation["observed_at"], key=parse_stamp)
        snapshot = {"run_id": run["id"],
                    "inventory_observation": "complete" if run["listing_status"] == "complete" else "partial",
                    "metrics": {}}
        for metric in METRICS:
            try:
                payload = client.get(key + "/insights", {"metric": metric})
                evidence = parse_metric(payload, metric)
            except Problem as problem:
                issues.add("insights", metric, problem)
                evidence = {"status": "invalid" if problem.category in ("invalid_value", "invalid_structure", "unexpected_period", "invalid_pagination") else "unavailable",
                            "category": problem.category}
                unavailable += 1
            evidence["checked_at"] = utc_now()
            snapshot["metrics"][metric] = evidence
        snapshot["collected_at"] = utc_now()
        append_snapshot(story, snapshot)

    run["insights_status"] = "partial" if unavailable else "complete" if observations else "not_needed"
    run["status"] = ("complete" if run["listing_status"] == "complete" and not unavailable else
                     "partial" if run["pages_received"] else "failed")
    run["finished_at"] = utc_now()
    run["requests_used"] = client.used
    run["issues"] = list(issues.records.values())
    result["runs"].append(run)
    validate_document(result, zone)
    return result, run


def positive_limit(value, maximum):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use um inteiro positivo.") from None
    if not 1 <= number <= maximum:
        raise argparse.ArgumentTypeError(f"Limite permitido: 1 a {maximum}.")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Observa stories do Instagram; nao certifica producao diaria total.")
    parser.add_argument("--dry-run", action="store_true", help="Consulta a API e processa em memoria, sem criar ou alterar arquivos.")
    parser.add_argument("--max-requests", type=lambda v: positive_limit(v, HARD_MAX_REQUESTS), default=DEFAULT_REQUESTS)
    parser.add_argument("--max-pages", type=lambda v: positive_limit(v, HARD_MAX_PAGES), default=DEFAULT_PAGES)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        token = os.environ.get("FB_PAGE_ACCESS_TOKEN")
        page_id = os.environ.get("FB_PAGE_ID", DEFAULT_PAGE_ID)
        if not token or not identifier(page_id):
            raise Problem("invalid_environment")
        try:
            zone = ZoneInfo(LOCAL_ZONE)
        except ZoneInfoNotFoundError:
            raise Problem("timezone_unavailable") from None
        # A cooperative writer holds the lock for the whole read/collect/save cycle.
        # Dry-run never creates a lock, temporary file, directory or document.
        with nullcontext() if args.dry_run else exclusive_lock(STORIES_PATH):
            data, original = load_document(STORIES_PATH, zone)
            with requests.Session() as session:
                session.mount("https://", HTTPAdapter(max_retries=0))
                result, run = collect(data, Client(session, token, args.max_requests), page_id, zone, args.max_pages)
            if not args.dry_run:
                try:
                    save_document(STORIES_PATH, result, original, zone)
                except Problem:
                    raise
                except (OSError, ValueError, TypeError):
                    raise Problem("storage") from None
            print(f"rede=instagram etapa=result status={run['status']} listagem={run['listing_status']} "
                  f"paginacao={run['pagination_status']} observados={run['distinct_stories_observed']} "
                  f"requisicoes={run['requests_used']} restantes={args.max_requests - run['requests_used']} "
                  f"dry_run={'sim' if args.dry_run else 'nao'}")
            print("Contagem observada nao e producao total; vazio nao confirma zero diario. Views e reach sao snapshots distintos, nao somas entre execucoes.")
            print("Dry-run: nenhum arquivo gravado." if args.dry_run else "stories.json gravado; historico de snapshots preservado.")
            return 0 if run["status"] == "complete" else 2
    except Problem as problem:
        Issues().add("execution", "-", problem)
        return 1
    except OSError:
        Issues().add("execution", "-", Problem("storage"))
        return 1
    except Exception:
        Issues().add("execution", "-", Problem("internal"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
