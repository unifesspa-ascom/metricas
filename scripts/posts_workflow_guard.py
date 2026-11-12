#!/usr/bin/env python3
"""Manual/Actions guard. No API calls except the explicitly selected collector.

All candidates and manifests stay in a runner-local directory outside the repo.
Validation does not import or execute a collector. Publishing uses a temporary
Git index, keeps unrelated remote changes, and attempts exactly one normal push.
"""
import argparse
import base64
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

TARGET = "dados/posts.json"
NUMERIC = {"vis", "views", "alc", "eng", "reac", "curt", "com", "comp", "salv"}
TEXT = {"tipo", "txt", "url"}
PROTECTED = (TARGET, "scripts/update_posts.py", "scripts/backfill_posts.py",
             "scripts/posts_workflow_guard.py", ".github/workflows/update-metrics.yml",
             ".github/workflows/backfill-posts.yml")
GENERAL = {"authentication", "permission", "service", "connection", "request", "rate_limit"}
OPTIONAL = {"metric_incompatible", "api_error", "insights_invalid_structure",
            "insights_pagination_unresolved", "missing_or_invalid", "invalid_entry",
            "duplicate", "unavailable", "invalid_date", "historical_date_conflict"}
COLLECTION_FAILURES = GENERAL | {"page_limit", "request_limit", "invalid_document",
    "invalid_listing", "invalid_pagination", "invalid_account", "invalid_publication_id",
    "new_publication_without_date", "locked", "lock_unavailable", "concurrent_change",
    "storage", "timezone_unavailable", "redirect_rejected", "invalid_response", "internal"}
REQUEST_LIMIT, PAGE_LIMIT = 2000, 50


class GuardError(RuntimeError):
    pass


def require(condition, category):
    if not condition:
        raise GuardError(category)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def decode(raw):
    try:
        value = json.loads(raw, object_pairs_hook=object_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(GuardError("invalid_number")))
        canonical(value)
        return value
    except (ValueError, TypeError, UnicodeError):
        raise GuardError("invalid_json") from None


def valid_date(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def count(value):
    return type(value) is int and value >= 0


def validate_document(doc):
    """Independent contract, including safe links; never certifies old values."""
    require(isinstance(doc, dict) and {"atualizado", "facebook", "instagram"} <= doc.keys(), "document_contract")
    require(doc["atualizado"] == "" or valid_date(doc["atualizado"]), "document_date")
    for network in ("facebook", "instagram"):
        require(isinstance(doc[network], list), "network_contract")
        seen = set()
        for post in doc[network]:
            require(isinstance(post, dict), "post_contract")
            pid = post.get("id")
            require(isinstance(pid, str) and re.fullmatch(r"[A-Za-z0-9_-]+", pid), "post_identity")
            require(pid not in seen, "duplicate_id")
            seen.add(pid)
            require(valid_date(post.get("data")), "post_date")
            require(all(key not in post or count(post[key]) for key in NUMERIC), "metric_contract")
            require(all(key not in post or isinstance(post[key], str) for key in TEXT), "text_contract")
            if post.get("url"):
                try:
                    url = urlsplit(post["url"])
                    require(url.scheme == "https" and bool(url.hostname) and not url.username and not url.password,
                            "unsafe_link")
                    require(not ({k.lower() for k in parse_qs(url.query)} &
                                 {"access_token", "token", "password", "secret"}), "unsafe_link")
                except ValueError:
                    raise GuardError("unsafe_link") from None
    require(not re.search(rb"\bEAA[A-Za-z0-9]{30,}|\bgh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----",
                          canonical(doc)), "sensitive_content")
    return doc


def arguments(mode, env):
    require(mode in ("update", "backfill"), "collector_mode")
    args = []
    if mode == "backfill":
        start = env.get("BACKFILL_START", "")
        end = env.get("BACKFILL_END", "") or datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()
        require(valid_date(start) and valid_date(end) and start <= end, "date_interval")
        args = ["--start", start, "--end", end]
        for variable, flag in (("SKIP_INSTAGRAM", "--skip-instagram"), ("SKIP_INSIGHTS", "--skip-insights")):
            value = env.get(variable, "false")
            require(value in ("true", "false"), "boolean_input")
            if value == "true":
                args.append(flag)
    return args + ["--max-requests", str(REQUEST_LIMIT), "--max-pages", str(PAGE_LIMIT)]


def git(repo, *args, env=None, input=None, allowed=(0,)):
    result = subprocess.run(["git", "-C", str(repo), *args], input=input,
                            capture_output=True, env=env, timeout=90)
    require(result.returncode in allowed, "git_operation_refused")
    return result


def text_git(repo, *args, **kwargs):
    return git(repo, *args, **kwargs).stdout.decode("utf-8").strip()


def put(path, value):
    path.write_bytes(canonical(value))


def read_state(work):
    meta = decode((work / "prepared.json").read_bytes())
    require(meta.get("schema") == 1 and meta.get("mode") in ("update", "backfill"), "preparation_contract")
    require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", meta.get("base_head", "")), "baseline_commit")
    args = meta.get("args")
    require(isinstance(args, list) and args[-4:] == ["--max-requests", "2000", "--max-pages", "50"], "prepared_arguments")
    prefix = args[:-4]
    if meta["mode"] == "update":
        require(not prefix, "prepared_arguments")
    else:
        require(len(prefix) >= 4 and prefix[0] == "--start" and prefix[2] == "--end"
                and valid_date(prefix[1]) and valid_date(prefix[3]) and prefix[1] <= prefix[3], "prepared_arguments")
        require(prefix[4:] in ([], ["--skip-instagram"], ["--skip-insights"],
                                ["--skip-instagram", "--skip-insights"]), "prepared_arguments")
    baseline = (work / "before.json").read_bytes()
    require(digest(baseline) == meta["before_sha256"], "baseline_changed")
    validate_document(decode(baseline))
    return meta


def prepare(repo, work, branch, mode, env):
    repo, work = repo.resolve(), work.resolve()
    require(not work.is_relative_to(repo), "work_directory_inside_repository")
    git(repo, "check-ref-format", "refs/heads/" + branch)
    require(not text_git(repo, "status", "--porcelain"), "dirty_repository")
    require(not work.exists(), "work_directory_exists")
    raw = (repo / TARGET).read_bytes()
    validate_document(decode(raw))
    args = arguments(mode, env)
    head = text_git(repo, "rev-parse", "HEAD")
    # Never publish pre-existing local data or uncommitted collector changes.
    require(git(repo, "show", head + ":" + TARGET).stdout == raw, "uncommitted_baseline")
    work.mkdir(parents=True)
    (work / "before.json").write_bytes(raw)
    (work / "candidate.json").write_bytes(raw)
    put(work / "prepared.json", {"schema": 1, "branch": branch, "mode": mode, "args": args,
                               "base_head": head, "before_sha256": digest(raw)})
    print("posts: baseline validated; candidate outside repository")


def load_collector(repo, mode):
    filename = "update_posts.py" if mode == "update" else "backfill_posts.py"
    spec = importlib.util.spec_from_file_location("guarded_posts", repo / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def projected(post):
    """Only normalized values, hashed IDs and hashed text; no raw API payloads."""
    fields = {}
    for key, value in post.items():
        if key in NUMERIC and count(value):
            fields[key] = value
        elif key == "data" and valid_date(value):
            fields[key] = value
        elif key in TEXT and isinstance(value, str) and value.strip():
            fields[key] = {"sha256": digest(canonical(value))}
    require(isinstance(post.get("id"), str) and re.fullmatch(r"[A-Za-z0-9_-]+", post["id"]), "collected_identity")
    return {"id_sha256": digest(post["id"].encode()), "fields": fields}


def collect(repo, work, loader=load_collector):
    meta = read_state(work)
    require(not text_git(repo, "status", "--porcelain"), "dirty_repository")
    require(text_git(repo, "rev-parse", "HEAD") == meta["base_head"], "local_head_changed")
    require(digest((repo / TARGET).read_bytes()) == meta["before_sha256"], "repository_data_changed")
    require(digest((work / "candidate.json").read_bytes()) == meta["before_sha256"], "candidate_not_initial")
    require(not (work / "collected.json").exists(), "collection_already_recorded")
    observations = {}
    started = datetime.now(timezone.utc).isoformat()
    # Legacy exceptions may carry authenticated URLs. Never echo arbitrary output.
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        module = loader(repo, meta["mode"])
        module.POSTS_PATH = str(work / "candidate.json")
        for network, name in (("facebook", "fetch_fb_posts"), ("instagram", "fetch_ig_posts")):
            original = getattr(module, name)

            def capture(*args, _fn=original, _network=network, **kwargs):
                result = _fn(*args, **kwargs)
                require(_network not in observations and isinstance(result, list), "listing_receipt")
                # This receipt exists only AFTER the listing function has completed.
                observations[_network] = [projected(post) for post in result]
                return result

            setattr(module, name, capture)
        try:
            result = module.main(meta["args"])
        except module.CollectionError as error:
            category = error.category if error.category in COLLECTION_FAILURES else "collection_failed"
            raise GuardError(category) from None
    expected = {"facebook"} if "--skip-instagram" in meta["args"] else {"facebook", "instagram"}
    require(set(observations) == expected, "listing_incomplete")
    require(not module.INSIGHT_BLOCKED, "general_insight_failure")
    require(not any(record["category"] in GENERAL for record in module.WARNING_COUNTS.values()), "general_insight_failure")
    warnings = {}
    for record in module.WARNING_COUNTS.values():
        category, occurrences = record["category"], record["count"]
        require(category in OPTIONAL and type(occurrences) is int and occurrences > 0, "unexpected_warning")
        warnings[category] = warnings.get(category, 0) + occurrences
    require(result.get("status") in ("complete", "partial") and not result.get("dry_run"), "collection_failed")
    used = result["operations"]["requests"]
    require(type(used) is int and 0 <= used <= REQUEST_LIMIT, "request_budget")
    raw = (work / "candidate.json").read_bytes()
    receipt = {"schema": 1, "before_sha256": meta["before_sha256"], "candidate_sha256": digest(raw),
               "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
               "status": result["status"], "requests": used, "observations": observations,
               "warnings": warnings}
    put(work / "collected.json", receipt)
    print("posts: inventory completed; optional_metrics=" + ("partial" if result["status"] == "partial" else "complete"))
    for category, occurrences in sorted(warnings.items()):
        print("posts: warning=" + category + " occurrences=" + str(occurrences))


def projection_document(doc):
    result = {}
    for network in ("facebook", "instagram"):
        result[network] = {}
        for post in doc[network]:
            fields = {}
            for key, value in post.items():
                if key == "id":
                    continue
                fields[key] = value if key == "data" or key in NUMERIC else {"sha256": digest(canonical(value))}
            result[network][digest(post["id"].encode())] = fields
    return result


def validate_candidate(repo, work):
    meta = read_state(work)
    before = decode((work / "before.json").read_bytes())
    raw = (work / "candidate.json").read_bytes()
    after = validate_document(decode(raw))
    receipt_raw = (work / "collected.json").read_bytes()
    receipt = decode(receipt_raw)
    require(receipt.get("schema") == 1 and receipt.get("status") in ("complete", "partial"), "receipt_contract")
    require(isinstance(receipt.get("warnings"), dict) and all(
        k in OPTIONAL and type(v) is int and v > 0 for k, v in receipt["warnings"].items()), "receipt_warnings")
    require(receipt["before_sha256"] == meta["before_sha256"] and receipt["candidate_sha256"] == digest(raw), "candidate_integrity")
    require(type(receipt["requests"]) is int and 0 <= receipt["requests"] <= REQUEST_LIMIT, "request_budget")
    expected_networks = {"facebook"} if "--skip-instagram" in meta["args"] else {"facebook", "instagram"}
    require(set(receipt["observations"]) == expected_networks, "listing_incomplete")
    expected = projection_document(before)
    for network, entries in receipt["observations"].items():
        require(isinstance(entries, list), "receipt_contract")
        for entry in entries:
            pid, fields = entry["id_sha256"], entry["fields"]
            require(isinstance(pid, str) and re.fullmatch(r"[0-9a-f]{64}", pid), "receipt_identity")
            require(isinstance(fields, dict) and fields.keys() <= NUMERIC | TEXT | {"data"}, "receipt_fields")
            for key, value in fields.items():
                require((count(value) if key in NUMERIC else valid_date(value) if key == "data" else
                         isinstance(value, dict) and set(value) == {"sha256"} and
                         isinstance(value["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"])), "receipt_value")
            if pid in expected[network]:
                fields = {k: v for k, v in fields.items() if k != "data"}
                expected[network][pid].update(fields)
            else:
                require("data" in fields, "new_post_without_date")
                expected[network][pid] = copy.deepcopy(fields)
    require(projection_document(after) == expected, "history_or_field_integrity")
    excluded = {"atualizado", "facebook", "instagram"}
    require({k: v for k, v in before.items() if k not in excluded} ==
            {k: v for k, v in after.items() if k not in excluded}, "unknown_root_fields_changed")
    try:
        zone = ZoneInfo("America/Sao_Paulo")
        start = datetime.fromisoformat(receipt["started_at"])
        finish = datetime.fromisoformat(receipt["finished_at"])
        require(start.tzinfo is not None and finish.tzinfo is not None and finish >= start, "receipt_time")
        require(start.astimezone(zone).date().isoformat() <= after["atualizado"] <= finish.astimezone(zone).date().isoformat(), "execution_date")
    except (ValueError, TypeError):
        raise GuardError("receipt_time") from None
    (work / "validated-posts.json").write_bytes(raw)
    put(work / "validated.json", {"candidate_sha256": digest(raw), "receipt_sha256": digest(receipt_raw),
                                 "prepared_sha256": digest((work / "prepared.json").read_bytes())})
    print("posts: independent contract, history and field validation passed")


def auth_environment(env):
    result = dict(env)
    result["GIT_TERMINAL_PROMPT"] = "0"
    token = result.pop("GITHUB_TOKEN", "")
    if token:
        require(not result.get("GIT_CONFIG_COUNT"), "unexpected_git_configuration")
        result.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                      GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode())
    return result


def publish(repo, work, env=None):
    meta = read_state(work)
    seal = decode((work / "validated.json").read_bytes())
    raw = (work / "validated-posts.json").read_bytes()
    require(digest(raw) == seal["candidate_sha256"] == digest((work / "candidate.json").read_bytes()), "validated_candidate_changed")
    require(digest((work / "collected.json").read_bytes()) == seal["receipt_sha256"] and
            digest((work / "prepared.json").read_bytes()) == seal["prepared_sha256"], "validation_inputs_changed")
    validate_document(decode(raw))
    require(text_git(repo, "rev-parse", "HEAD") == meta["base_head"], "local_head_changed")
    require(not text_git(repo, "status", "--porcelain"), "dirty_repository")
    branch = meta["branch"]
    git(repo, "check-ref-format", "refs/heads/" + branch)
    auth = auth_environment(os.environ if env is None else env)
    git(repo, "fetch", "--no-tags", "origin", "refs/heads/" + branch, env=auth)
    remote = text_git(repo, "rev-parse", "FETCH_HEAD")
    git(repo, "merge-base", "--is-ancestor", meta["base_head"], remote)
    require(not text_git(repo, "log", "--format=%H", meta["base_head"] + ".." + remote,
                         "--", *PROTECTED), "remote_components_changed")
    require(git(repo, "diff", "--quiet", meta["base_head"], remote, "--", *PROTECTED,
                allowed=(0, 1)).returncode == 0, "remote_components_changed")
    if git(repo, "show", remote + ":" + TARGET).stdout == raw:
        print("posts: unchanged; no commit or push")
        return None
    with tempfile.TemporaryDirectory(prefix="posts-index-", dir=work) as index_dir:
        index_env = {**auth, "GIT_INDEX_FILE": str(Path(index_dir) / "index"),
                     "GIT_AUTHOR_NAME": "github-actions[bot]", "GIT_COMMITTER_NAME": "github-actions[bot]",
                     "GIT_AUTHOR_EMAIL": "github-actions[bot]@users.noreply.github.com",
                     "GIT_COMMITTER_EMAIL": "github-actions[bot]@users.noreply.github.com"}
        git(repo, "read-tree", remote, env=index_env)
        blob = text_git(repo, "hash-object", "-w", "--stdin", input=raw, env=index_env)
        git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, TARGET, env=index_env)
        require(text_git(repo, "diff", "--cached", "--name-only", remote, env=index_env).splitlines() == [TARGET], "commit_scope")
        tree = text_git(repo, "write-tree", env=index_env)
        commit = text_git(repo, "commit-tree", tree, "-p", remote, "-m", "chore: update publication inventory", env=index_env)
        require(text_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines() == [TARGET], "commit_scope")
        git(repo, "push", "origin", commit + ":refs/heads/" + branch, env=auth)
    print("posts: published only dados/posts.json; ordinary push")
    return commit


def account(repo, work):
    """Keep legacy collection isolated; never publish its file or raw logs."""
    require(not work.resolve().is_relative_to(repo.resolve()), "work_directory_inside_repository")
    require(not work.exists(), "work_directory_exists")
    (work / "dados").mkdir(parents=True)
    (work / "dados" / "metricas.json").write_bytes((repo / "dados" / "metricas.json").read_bytes())
    try:
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(repo / "scripts" / "update_metrics.py")],
                                cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
        require(result.returncode == 0, "account_collection_failed")
    except subprocess.TimeoutExpired:
        raise GuardError("account_collection_timeout") from None
    print("account: legacy collection finished; no account data will be published")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "collect", "validate", "publish", "account"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--branch", default=os.environ.get("TARGET_BRANCH", "main"))
    parser.add_argument("--mode", choices=("update", "backfill"), default="update")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.repo, args.work, args.branch, args.mode, os.environ)
        elif args.command == "collect":
            collect(args.repo, args.work)
        elif args.command == "validate":
            validate_candidate(args.repo, args.work)
        elif args.command == "publish":
            publish(args.repo, args.work)
        else:
            account(args.repo.resolve(), args.work)
        return 0
    except Exception as error:
        category = str(error) if isinstance(error, GuardError) else "operation_failed"
        # Only local, fixed guard categories are eligible for output.
        if not re.fullmatch(r"[a-z_]{1,60}", category):
            category = "operation_failed"
        print("posts_guard: category=" + category, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
