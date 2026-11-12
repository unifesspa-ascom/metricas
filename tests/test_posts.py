"""Offline tests: no real credentials, network, or project JSON writes."""
import contextlib
import builtins
import copy
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import subprocess
from unittest.mock import patch, Mock


class PostsTestSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = []
        root = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {"FB_PAGE_ACCESS_TOKEN": "FAKE_OFFLINE_TOKEN"}):
            for name in ("update_posts", "backfill_posts"):
                spec = importlib.util.spec_from_file_location(name, root / "scripts" / (name + ".py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                cls.modules.append(module)

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch("time.sleep"))
        self.stack.enter_context(patch.object(sys, "argv", ["collector"]))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        for module in self.modules:
            self.stack.enter_context(patch.object(module, "POSTS_PATH", str(Path(directory) / (module.__name__ + ".json"))))
            module.reset_execution_state()

class PostsTests(PostsTestSetup):
    def test_confirmed_zero_and_invalid_values(self):
        for m in self.modules:
            self.assertTrue(m.confirmed_count(0))
            for value in (None, False, True, "0", -1, 1.5, float("nan"), {}):
                self.assertFalse(m.confirmed_count(value))

    def test_insights_zero_and_reactions(self):
        for m in self.modules:
            entries = [{"name": n, "values": [{"value": 0}]} for n in ("impressions", "reach", "saved")]
            with patch.object(m, "api_get", return_value={"data": entries}):
                self.assertEqual(m.fetch_ig_media_insights("id"), {"impressions": 0, "reach": 0, "saved": 0})
            names = ("post_impressions", "post_impressions_unique", "post_engaged_users", "post_reactions_by_type_total")
            entries = [{"name": n, "value": {"like": 0} if n == names[-1] else 0} for n in names]
            with patch.object(m, "api_get", return_value={"data": entries}):
                self.assertEqual(m.fetch_fb_post_insights("id")[names[-1]], 0)

    def test_partial_missing_null_invalid_insights_are_omitted(self):
        for m in self.modules:
            for payload in ({}, {"data": []}, {"data": [{"name": "reach", "value": 10}]},
                            {"data": [{"name": "reach", "values": []}]},
                            {"data": [{"name": "reach", "value": None}]},
                            {"data": [{"name": "reach", "value": "0"}]}):
                with self.subTest(module=m.__name__, payload=payload), patch.object(m, "api_get", return_value=payload):
                    result = m.fetch_ig_media_insights("id")
                    self.assertEqual(result, {"reach": 10} if payload == {"data": [{"name": "reach", "value": 10}]} else {})

    def test_merge_preserves_history_and_accepts_zero(self):
        for m in self.modules:
            original = [{"id": "id", "data": "2026-01-01", "vis": 30, "alc": 20, "txt": "old", "custom": 7}]
            snapshot = copy.deepcopy(original)
            result = m.merge_posts(original, [{"id": "id", "vis": None, "alc": 0, "txt": "", "com": "0"}])
            self.assertEqual(original, snapshot)
            self.assertEqual(result[0], {**original[0], "alc": 0})
            result = m.merge_posts([], [{"id": "id", "data": "2026-01-01"}, {"id": "id", "vis": 0}])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["vis"], 0)

    def test_optional_missing_fields_and_skip_insights(self):
        for m in self.modules:
            payload = {"data": [{"id": "id", "timestamp": "2026-01-01T12:00:00+0000", "media_type": "IMAGE", "like_count": 0}]}
            with patch.object(m, "api_get", return_value=payload), patch.object(m, "fetch_ig_media_insights", return_value={"impressions": 0, "reach": 0, "saved": 0}):
                post = m.fetch_ig_posts("ig", "a", "b")[0]
                self.assertEqual(post["curt"], 0)
                self.assertNotIn("com", post)
                self.assertNotIn("txt", post)
            if m.__name__ == "backfill_posts":
                for platform in ("ig", "fb"):
                    item = payload["data"][0] if platform == "ig" else {"id": "id", "created_time": "2026-01-01T12:00:00+0000"}
                    with patch.object(m, "api_get", return_value={"data": [item]}), patch.object(m, "fetch_ig_media_insights") as ig, patch.object(m, "fetch_fb_post_insights") as fb:
                        posts = m.fetch_ig_posts("ig", "a", "b", True) if platform == "ig" else m.fetch_fb_posts("a", "b", True)
                        ig.assert_not_called()
                        fb.assert_not_called()
                        merged = m.merge_posts([{"id": "id", "data": "2026-01-01", "vis": 99}], posts)
                        self.assertEqual(merged[0]["vis"], 99)

    def test_pagination_and_failure(self):
        for m in self.modules:
            with patch.object(m, "api_get", side_effect=[{"data": [], "paging": {"next": "page2"}}, {"data": []}]) as api:
                self.assertEqual(m.fetch_fb_posts("a", "b"), [])
                self.assertEqual(api.call_args_list[1].args, ("page2", {}))
            with patch.object(m, "api_get", side_effect=[{"data": [], "paging": {"next": "page2"}}, RuntimeError("failure")]):
                with self.assertRaises(RuntimeError):
                    m.fetch_fb_posts("a", "b")

    def test_main_failure_preserves_file_and_date(self):
        for m in self.modules:
            for fail_fb in (True, False):
                with self.subTest(module=m.__name__, fail_fb=fail_fb), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "posts.json"
                    original = b'{"atualizado":"2020-01-01","facebook":[],"instagram":[]}'
                    path.write_bytes(original)
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(patch.object(m, "POSTS_PATH", str(path)))
                        stack.enter_context(patch.object(m, "get_ig_user_id", return_value="ig"))
                        if m.__name__ == "backfill_posts":
                            stack.enter_context(patch.object(m, "parse_args", return_value=SimpleNamespace(start="2026-01-01", end="2026-01-31", skip_instagram=False, skip_insights=False, dry_run=False)))
                        stack.enter_context(patch.object(m, "fetch_fb_posts", side_effect=RuntimeError("failure") if fail_fb else None, return_value=[]))
                        stack.enter_context(patch.object(m, "fetch_ig_posts", side_effect=RuntimeError("failure")))
                        with self.assertRaises(RuntimeError):
                            m.main()
                    self.assertEqual(path.read_bytes(), original)

    def test_success_and_skipped_instagram(self):
        for m in self.modules:
            original = {"atualizado": "2020-01-01", "facebook": [{"id": "id", "data": "2020-01-01", "vis": 7}], "instagram": [{"id": "ig", "data": "2020-01-01", "vis": 8}]}
            with patch.object(m, "load_posts", return_value=copy.deepcopy(original)), patch.object(m, "save_posts") as save, patch.object(m, "fetch_fb_posts", return_value=[{"id": "id", "vis": 0}]), patch.object(m, "get_ig_user_id", return_value="ig"), patch.object(m, "fetch_ig_posts", return_value=[]) as ig, contextlib.ExitStack() as stack:
                if m.__name__ == "backfill_posts":
                    stack.enter_context(patch.object(m, "parse_args", return_value=SimpleNamespace(start="2026-01-01", end="2026-01-31", skip_instagram=True, skip_insights=True, dry_run=False)))
                m.main()
                result = save.call_args.args[0]
                self.assertEqual(result["facebook"][0]["vis"], 0)
                self.assertEqual(result["instagram"], original["instagram"])
                self.assertNotEqual(result["atualizado"], original["atualizado"])
                if m.__name__ == "backfill_posts":
                    ig.assert_not_called()

    def test_atomic_save_success_and_failures(self):
        for m in self.modules:
            with tempfile.TemporaryDirectory() as directory, patch.object(m, "POSTS_PATH", str(Path(directory) / "posts.json")):
                path = Path(m.POSTS_PATH)
                path.write_text('{"atualizado":"","facebook":[],"instagram":[]}', encoding="utf-8")
                with patch.object(m.os, "replace", side_effect=OSError("failure")):
                    with self.assertRaises(OSError):
                        m.save_posts({"atualizado": "", "facebook": [], "instagram": []})
                self.assertEqual(path.read_text(), '{"atualizado":"","facebook":[],"instagram":[]}')
                self.assertEqual(list(Path(directory).iterdir()), [path])
                with self.assertRaises(TypeError):
                    m.save_posts({"invalid": object()})
                self.assertEqual(path.read_text(), '{"atualizado":"","facebook":[],"instagram":[]}')
                with patch.object(m.os, "fsync", side_effect=OSError("failure")):
                    with self.assertRaises(OSError):
                        m.save_posts({"atualizado": "", "facebook": [], "instagram": []})
                self.assertEqual(path.read_text(), '{"atualizado":"","facebook":[],"instagram":[]}')
                self.assertEqual(list(Path(directory).iterdir()), [path])
                m.save_posts({"atualizado": "", "facebook": [], "instagram": []})
                self.assertEqual(json.loads(path.read_text()), {"atualizado": "", "facebook": [], "instagram": []})

    def test_retries_and_safe_errors(self):
        for m in self.modules:
            response = Mock(status_code=200, ok=True)
            response.json.return_value = {"data": []}
            with patch.object(m.requests, "get", side_effect=[m.requests.Timeout("FAKE_OFFLINE_TOKEN"), Mock(status_code=429), Mock(status_code=503), response]):
                self.assertEqual(m.api_get("url", {}, retries=4), {"data": []})
            stream = io.StringIO()
            with patch.object(m.requests, "get", side_effect=m.requests.ConnectionError("FAKE_OFFLINE_TOKEN")), contextlib.redirect_stderr(stream):
                with self.assertRaises(RuntimeError) as error:
                    m.api_get("url?access_token=FAKE_OFFLINE_TOKEN", {}, retries=1)
            self.assertNotIn("FAKE_OFFLINE_TOKEN", str(error.exception) + stream.getvalue())

    @staticmethod
    def response(data, status=200):
        response = Mock(status_code=status, ok=status < 400)
        response.json.return_value = data
        return response

    def test_metric_incompatibility_recovers_individually(self):
        for m in self.modules:
            responses = [
                self.response({"error": {"code": 100, "message": "Invalid metric FAKE_OFFLINE_TOKEN"}}, 400),
                self.response({"data": [{"name": "impressions", "value": 0}]}),
                self.response({"error": {"code": 100, "message": "metric not supported"}}, 400),
                self.response({"data": [{"name": "saved", "value": 5}]}),
                self.response({"data": [{"name": "views", "value": 9}]}),
            ]
            stream = io.StringIO()
            with patch.object(m.requests, "get", side_effect=responses) as request, contextlib.redirect_stderr(stream):
                self.assertEqual(m.fetch_ig_media_insights("id"), {"impressions": 0, "saved": 5, "views": 9})
            self.assertEqual(request.call_count, 5)
            self.assertEqual([call.kwargs["params"]["metric"] for call in request.call_args_list],
                             ["impressions,reach,saved", "impressions", "reach", "saved", "views"])
            self.assertNotIn("FAKE_OFFLINE_TOKEN", stream.getvalue())
            self.assertIn("rede=instagram", stream.getvalue())
            self.assertIn("metrica=reach", stream.getvalue())
            self.assertIn("categoria=metric_incompatible", stream.getvalue())

    def test_general_errors_never_fan_out_or_repeat_for_other_posts(self):
        for m in self.modules:
            for code, status, category, calls in ((190, 400, "authentication", 1), (200, 403, "permission", 1),
                                                   (2, 500, "service", 3), (4, 429, "rate_limit", 3)):
                with self.subTest(module=m.__name__, category=category):
                    m.INSIGHT_BLOCKED.clear()
                    response = self.response({"error": {"code": code, "message": "FAKE_OFFLINE_TOKEN"}}, status)
                    with patch.object(m.requests, "get", return_value=response) as request:
                        self.assertEqual(m.fetch_ig_media_insights("first"), {})
                        self.assertEqual(m.fetch_ig_media_insights("second"), {})
                    self.assertEqual(request.call_count, calls)
                    self.assertEqual(m.INSIGHT_BLOCKED["instagram"], category)

    def test_individual_recovery_stops_on_general_failure(self):
        for m in self.modules:
            with patch.object(m, "api_get", side_effect=[m.CollectionError("metric_incompatible"),
                    {"data": [{"name": "impressions", "value": 5}]}, m.CollectionError("authentication")]) as api:
                self.assertEqual(m.fetch_ig_media_insights("id"), {"impressions": 5})
                self.assertEqual(api.call_count, 3)

    def test_partial_query_preserves_only_unavailable_fields(self):
        for m in self.modules:
            payload = {"data": [{"name": "reach", "value": 0}, {"name": "saved", "value": None}]}
            with patch.object(m, "api_get", return_value=payload) as api:
                insights = m.fetch_ig_media_insights("id")
            self.assertEqual(api.call_count, 2)
            new = {"id": "id"}
            for key, name in (("vis", "impressions"), ("alc", "reach"), ("salv", "saved")):
                m.put_count(new, key, insights.get(name))
            old = [{"id": "id", "data": "2026-01-01", "vis": 10, "alc": 20, "salv": 30}]
            summary = {}
            result = m.merge_posts(old, [new], "instagram", summary=summary)
            self.assertEqual(result[0], {**old[0], "alc": 0})
            self.assertEqual(summary["instagram"], {"updated": 1, "preserved": 3, "unavailable": 6})

    def test_malformed_entry_does_not_discard_valid_metrics(self):
        for m in self.modules:
            with patch.object(m, "api_get", return_value={"data": [None, {"name": "reach", "value": 0}, {"name": "saved", "value": False}]}):
                self.assertEqual(m.fetch_ig_media_insights("id"), {"reach": 0})

    def test_duplicate_insights_do_not_overwrite_history(self):
        for m in self.modules:
            for values in ((1, 2, 3), (None, 2), (0, None)):
                with patch.object(m, "api_get", return_value={"data": [{"name": "reach", "value": v} for v in values]}):
                    self.assertNotIn("reach", m.fetch_ig_media_insights("id"))

    def test_dates_and_timezone_policy(self):
        for m in self.modules:
            for invalid in (None, "", "invalid", "2026-02-30", "2026-01-01garbage", 123):
                self.assertIsNone(m.publication_date(invalid, "instagram"))
                old = [{"id": "id", "data": "2026-01-01", "vis": 3}]
                result = m.merge_posts(old, [{"id": "id", "data": invalid, "vis": 0}])
                self.assertEqual(result[0]["data"], "2026-01-01")
                self.assertEqual(result[0]["vis"], 0)
            timestamp = "2026-01-02T01:00:00+0000"
            expected = "2026-01-01"
            self.assertEqual(m.publication_date(timestamp, "instagram"), expected)
            with self.assertRaises(m.CollectionError):
                m.merge_posts([], [{"id": "new", "data": "bad"}])

    def test_pagination_failure_after_real_posts_is_fatal(self):
        for m in self.modules:
            for network in ("facebook", "instagram"):
                item = {"id": "id", "created_time": "2026-01-01T12:00:00+0000", "timestamp": "2026-01-01T12:00:00+0000"}
                with patch.object(m, "api_get", side_effect=[{"data": [item], "paging": {"next": "page2"}}, m.CollectionError("service")]), patch.object(m, "fetch_fb_post_insights", return_value={}), patch.object(m, "fetch_ig_media_insights", return_value={}):
                    with self.assertRaises(m.CollectionError):
                        if network == "facebook":
                            m.fetch_fb_posts("a", "b")
                        else:
                            m.fetch_ig_posts("ig", "a", "b")

    def test_invalid_listings_accounts_and_pagination_are_fatal(self):
        for m in self.modules:
            for payload in ({}, {"data": None}, {"data": [None]}, {"data": [], "paging": None},
                            {"data": [], "paging": {"next": 3}}):
                with patch.object(m, "api_get", return_value=payload):
                    with self.assertRaises(m.CollectionError):
                        m.fetch_fb_posts("a", "b")
            for payload in ({}, {"instagram_business_account": {"id": None}}):
                with patch.object(m, "api_get", return_value=payload):
                    with self.assertRaises(m.CollectionError):
                        m.get_ig_user_id()
            with patch.object(m, "api_get", return_value={"data": [], "paging": {"next": "loop"}}):
                with self.assertRaises(m.CollectionError):
                    m.fetch_ig_posts("ig", "a", "b")
            with patch.object(m, "api_get", return_value={"data": [], "paging": {"next": "page"}}):
                self.assertEqual(m.fetch_ig_media_insights("id"), {})

    def test_write_failure_preserves_original_and_removes_temporary(self):
        for m in self.modules:
            with tempfile.TemporaryDirectory() as directory, patch.object(m, "POSTS_PATH", str(Path(directory) / "posts.json")):
                path = Path(m.POSTS_PATH)
                path.write_bytes(b'{"atualizado":"","facebook":[],"instagram":[]}')
                real_factory = m.tempfile.NamedTemporaryFile

                @contextlib.contextmanager
                def broken_writer(*args, **kwargs):
                    with real_factory(*args, **kwargs) as file:
                        wrapper = Mock(wraps=file)
                        wrapper.name = file.name
                        def fail_write(payload):
                            file.write(payload[:3])
                            raise OSError("simulated disk full")
                        wrapper.write.side_effect = fail_write
                        yield wrapper

                with patch.object(m.tempfile, "NamedTemporaryFile", side_effect=broken_writer):
                    with self.assertRaises(OSError):
                        m.save_posts({"atualizado": "", "facebook": [], "instagram": []})
                self.assertEqual(path.read_bytes(), b'{"atualizado":"","facebook":[],"instagram":[]}')
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_complete_execution_simulated_with_partial_insights(self):
        for m in self.modules:
            for failed_insights in (False, True):
                with self.subTest(module=m.__name__, failed=failed_insights), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "posts.json"
                    original = {"atualizado": "2020-01-01", "facebook": [], "instagram": [{"id": "old", "data": "2026-01-01", "vis": 9, "alc": 8, "salv": 7}]}
                    path.write_text(json.dumps(original), encoding="utf-8")
                    media = [{"id": "old", "timestamp": "2026-01-01T12:00:00+0000", "like_count": 0},
                             {"id": "new", "timestamp": "2026-01-02T12:00:00+0000", "comments_count": 1}]
                    insight = m.CollectionError("authentication") if failed_insights else {"data": [{"name": "reach", "value": 0}]}
                    responses = [{"data": []}, {"instagram_business_account": {"id": "ig"}}, {"data": media}, insight]
                    if not failed_insights:
                        responses.extend([{"data": []}, {"data": []}, {"data": []}])
                    with patch.object(m, "POSTS_PATH", str(path)), patch.object(m, "api_get", side_effect=responses), contextlib.ExitStack() as stack:
                        if m.__name__ == "backfill_posts":
                            stack.enter_context(patch.object(m, "parse_args", return_value=SimpleNamespace(start="2026-01-01", end="2026-01-31", skip_instagram=False, skip_insights=False, dry_run=False)))
                        result = m.main()
                    stored = json.loads(path.read_text(encoding="utf-8"))
                    by_id = {p["id"]: p for p in stored["instagram"]}
                    self.assertEqual(by_id["old"]["vis"], 9)
                    self.assertEqual(by_id["old"]["salv"], 7)
                    self.assertEqual(by_id["old"]["alc"], 8 if failed_insights else 0)
                    self.assertEqual(by_id["old"]["curt"], 0)
                    self.assertNotIn("vis", by_id["new"])
                    self.assertNotIn("salv", by_id["new"])
                    self.assertEqual(by_id["new"]["com"], 1)
                    self.assertEqual(result["status"], "partial")
                    self.assertIn("not_all_insights", result["updated_means"])
                    self.assertNotEqual(stored["atualizado"], "2020-01-01")

    def test_entry_point_exit_codes_and_safe_context(self):
        for m in self.modules:
            for error, expected in ((m.CollectionError("invalid_listing"), "invalid_listing"),
                                    (RuntimeError("FAKE_OFFLINE_TOKEN url?access_token=secret"), "internal")):
                stream = io.StringIO()
                with patch.object(m, "main", side_effect=error), contextlib.redirect_stderr(stream):
                    self.assertEqual(m.run(), 1)
                self.assertIn("etapa=execution", stream.getvalue())
                self.assertIn("categoria=" + expected, stream.getvalue())
                self.assertNotIn("FAKE_OFFLINE_TOKEN", stream.getvalue())
                self.assertNotIn("access_token", stream.getvalue())
            with patch.object(m, "main", return_value={"status": "partial"}):
                self.assertEqual(m.run(), 0)

    def test_fatal_execution_preserves_file_for_listing_account_and_write(self):
        for m in self.modules:
            for failure in ("listing", "account", "write"):
                with self.subTest(module=m.__name__, failure=failure), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "posts.json"
                    original = b'{"atualizado":"2020-01-01","facebook":[],"instagram":[]}'
                    path.write_bytes(original)
                    item = {"id": "id", "created_time": "2026-01-01T12:00:00+0000", "shares": {"count": 0}, "comments": {"summary": {"total_count": 0}}}
                    responses = ([{"data": [item], "paging": {"next": "page2"}}, m.CollectionError("service")]
                                 if failure == "listing" else
                                 [{"data": []}, {}] if failure == "account" else
                                 [{"data": []}, {"instagram_business_account": {"id": "ig"}}, {"data": []}])
                    stream = io.StringIO()
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(patch.object(m, "POSTS_PATH", str(path)))
                        stack.enter_context(patch.object(m, "api_get", side_effect=responses))
                        stack.enter_context(patch.object(m, "fetch_fb_post_insights", return_value={}))
                        stack.enter_context(contextlib.redirect_stderr(stream))
                        if m.__name__ == "backfill_posts":
                            stack.enter_context(patch.object(m, "parse_args", return_value=SimpleNamespace(start="2026-01-01", end="2026-01-31", skip_instagram=False, skip_insights=False, dry_run=False)))
                        if failure == "write":
                            stack.enter_context(patch.object(m.os, "replace", side_effect=OSError("FAKE_OFFLINE_TOKEN")))
                        self.assertEqual(m.run(), 1)
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(list(Path(directory).iterdir()), [path])
                    self.assertNotIn("FAKE_OFFLINE_TOKEN", stream.getvalue())
                    self.assertIn("categoria=", stream.getvalue())

    def test_unknown_api_error_does_not_trigger_individual_queries(self):
        for m in self.modules:
            with patch.object(m.requests, "get", return_value=self.response({"error": {"code": 100, "message": "Invalid unrelated parameter"}}, 400)) as request:
                self.assertEqual(m.fetch_ig_media_insights("id"), {})
                self.assertEqual(request.call_count, 2)
                self.assertEqual([call.kwargs["params"]["metric"] for call in request.call_args_list],
                                 ["impressions,reach,saved", "views"])

    def test_complete_result_and_summary_semantics(self):
        for m in self.modules:
            old = [{"id": "id", "data": "2026-01-01", "vis": 1, "alc": 2, "salv": 3, "curt": 4, "com": 5}]
            summary = {}
            result = m.merge_posts(old, [{"id": "id", "data": "2026-01-02", "curt": 0, "com": 0}],
                                   "instagram", skip_insights=True, summary=summary)
            self.assertEqual(summary["instagram"], {"updated": 2, "preserved": 4, "unavailable": 0})
            self.assertEqual(result[0]["vis"], 1)
            self.assertEqual(result[0]["data"], "2026-01-01")

    def test_dry_run_cli_requires_explicit_historical_interval(self):
        for m in self.modules:
            if m.__name__ == "update_posts":
                self.assertTrue(m.parse_args(["--dry-run"]).dry_run)
                self.assertFalse(m.parse_args([]).dry_run)
            else:
                with self.assertRaises(SystemExit):
                    m.parse_args(["--dry-run"])
                self.assertTrue(m.parse_args(["--start", "2026-11-01", "--end", "2026-11-02", "--dry-run"]).dry_run)
                self.assertFalse(m.parse_args(["--start", "2026-11-01"]).dry_run)

    def test_dry_run_real_flow_is_read_only_even_on_failure(self):
        for m in self.modules:
            for failure in (None, "listing", "account", "pagination", "insights"):
                with self.subTest(module=m.__name__, failure=failure), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = root / "posts.json"
                    original = {"atualizado": "2020-01-01", "facebook": [],
                                "instagram": [{"id": "id", "data": "2026-11-01", "vis": 99, "alc": 88, "salv": 77}]}
                    path.write_text(json.dumps(original), encoding="utf-8")
                    (root / "metricas.json").write_bytes(b"account-data-untouched")
                    before = {p.name: p.read_bytes() for p in root.iterdir()}
                    item = {"id": "id", "timestamp": "2026-11-01T12:00:00+0000", "like_count": 0, "comments_count": 1}
                    responses = [{"data": []}, {"instagram_business_account": {"id": "ig"}}, {"data": [item]},
                                 {"data": [{"name": "reach", "value": 0}, {"name": "impressions", "value": 12}]}]
                    if failure == "listing":
                        responses = [m.CollectionError("service")]
                    elif failure == "account":
                        responses[1] = {}
                    elif failure == "pagination":
                        responses[2] = {"data": [item], "paging": {"next": "page2"}}
                        responses.extend([{"data": []}, m.CollectionError("service")])
                    elif failure == "insights":
                        responses[3] = m.CollectionError("authentication")
                    elif failure is None:
                        responses.append({"data": []})
                    argv = ["--dry-run"] if m.__name__ == "update_posts" else ["--start", "2026-11-01", "--end", "2026-11-02", "--dry-run"]
                    output = io.StringIO()
                    real_open = builtins.open

                    def readonly_open(file, mode="r", *args, **kwargs):
                        if any(flag in mode for flag in "wax+"):
                            raise AssertionError("dry-run tried to open a file for writing")
                        return real_open(file, mode, *args, **kwargs)

                    with patch.object(m, "POSTS_PATH", str(path)), patch.object(m, "api_get", side_effect=responses), patch.object(m, "save_posts") as save, patch.object(m.tempfile, "NamedTemporaryFile") as temporary, patch.object(m.os, "replace") as replace, patch("builtins.open", side_effect=readonly_open), contextlib.redirect_stdout(output):
                        if failure in ("listing", "account", "pagination"):
                            with self.assertRaises(m.CollectionError):
                                m.main(argv)
                        else:
                            result = m.main(argv)
                            self.assertTrue(result["dry_run"])
                            self.assertEqual(result["diagnostics"]["instagram"]["publications"], 1)
                            metrics = result["diagnostics"]["instagram"]["metrics"]
                            self.assertEqual(metrics["vis"]["source"], "impressions")
                            self.assertEqual(metrics["alc"]["source"], "reach")
                            if failure == "insights":
                                self.assertEqual(metrics["vis"]["confirmed"], 0)
                                self.assertEqual(metrics["vis"]["preserved"], 1)
                            else:
                                self.assertEqual(metrics["vis"]["confirmed"], 1)
                                self.assertEqual(metrics["vis"]["positive"], 1)
                                self.assertEqual(metrics["alc"]["zero"], 1)
                            self.assertEqual(metrics["salv"]["unavailable"], 1)
                            self.assertEqual(metrics["salv"]["preserved"], 1)
                            self.assertIn("[dry-run] rede=facebook publicacoes=0", output.getvalue())
                            self.assertIn("origem=impressions", output.getvalue())
                            self.assertIn("origem=reach", output.getvalue())
                        save.assert_not_called()
                        temporary.assert_not_called()
                        replace.assert_not_called()
                    self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)
                    self.assertNotIn("FAKE_OFFLINE_TOKEN", output.getvalue())

    def test_dry_run_does_not_create_missing_data_file(self):
        for m in self.modules:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "posts.json"
                argv = ["--dry-run"] if m.__name__ == "update_posts" else ["--start", "2026-11-01", "--dry-run"]
                with patch.object(m, "POSTS_PATH", str(path)), patch.object(m, "api_get", side_effect=[{"data": []}, {"instagram_business_account": {"id": "ig"}}, {"data": []}]):
                    result = m.main(argv)
                self.assertTrue(result["dry_run"])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_safe_numeric_api_details_survive_fatal_execution(self):
        for m in self.modules:
            for code, subcode, category in ((100, 33, "api_error"), (190, 463, "authentication"), (200, 0, "permission")):
                stream = io.StringIO()
                response = self.response({"error": {"code": code, "error_subcode": subcode,
                    "type": "OAuthException", "message": "FAKE_OFFLINE_TOKEN https://secret/?access_token=hidden",
                    "fbtrace_id": "PRIVATE_TRACE", "error_user_msg": "PRIVATE_BODY"}}, 400)
                with tempfile.TemporaryDirectory() as directory, patch.object(m, "POSTS_PATH", str(Path(directory) / "posts.json")), patch.object(m.requests, "get", return_value=response), patch.object(m, "save_posts") as save, patch.object(sys, "argv", ["collector", "--dry-run"] if m.__name__ == "update_posts" else ["collector", "--start", "2026-11-01", "--dry-run"]), contextlib.redirect_stderr(stream):
                    self.assertEqual(m.run(), 1)
                    save.assert_not_called()
                    self.assertEqual(list(Path(directory).iterdir()), [])
                lines = stream.getvalue().splitlines()
                self.assertEqual(len(lines), 3)
                self.assertIn("etapa=listing", lines[0])
                self.assertIn("etapa=execution", lines[-1])
                for line in lines:
                    self.assertIn("http=400", line)
                    self.assertIn(f"meta_code={code}", line)
                    self.assertIn(f"meta_subcode={subcode}", line)
                    self.assertIn("categoria=" + category, line)
                for secret in ("FAKE_OFFLINE_TOKEN", "https://", "access_token", "PRIVATE_TRACE", "PRIVATE_BODY", "OAuthException"):
                    self.assertNotIn(secret, stream.getvalue())

    def test_error_metadata_rejects_untrusted_types(self):
        for m in self.modules:
            for value in ("FAKE_OFFLINE_TOKEN", "100\naccess_token=secret", True, {}, [190], -1, 10**40):
                m.reset_execution_state()
                stream = io.StringIO()
                error = m.CollectionError("api_error", http_status=value, meta_code=value, meta_subcode=value)
                with contextlib.redirect_stderr(stream):
                    m.log_issue("facebook", "listing", "-", error.category, error=error)
                self.assertEqual(stream.getvalue().strip(), "rede=facebook etapa=listing metrica=- categoria=api_error http=- meta_code=- meta_subcode=-")

    def test_final_server_and_invalid_json_errors_keep_safe_http_details(self):
        for m in self.modules:
            for status, invalid_json in ((503, False), (400, True)):
                response = self.response({"error": {"code": 2, "error_subcode": 99,
                                                       "message": "FAKE_OFFLINE_TOKEN"}}, status)
                if invalid_json:
                    response.json.side_effect = ValueError("FAKE_OFFLINE_TOKEN")
                stream = io.StringIO()
                with patch.object(m.requests, "get", return_value=response) as request, contextlib.redirect_stderr(stream):
                    with self.assertRaises(m.CollectionError):
                        m.fetch_listing_page("https://secret/?access_token=hidden", {"access_token": "FAKE_OFFLINE_TOKEN"}, "facebook")
                self.assertEqual(request.call_count, 1 if invalid_json else 3)
                self.assertIn(f"http={status}", stream.getvalue())
                self.assertIn("meta_code=-" if invalid_json else "meta_code=2", stream.getvalue())
                self.assertNotIn("FAKE_OFFLINE_TOKEN", stream.getvalue())
                self.assertNotIn("https://", stream.getvalue())

    def test_facebook_endpoint_parameters_and_safe_pagination_context(self):
        for m in self.modules:
            initial = {"data": [{"id": "id", "created_time": "2026-11-01T12:00:00+0000"}],
                       "paging": {"next": "https://secret/page2?access_token=PRIVATE_TOKEN"}}
            error = self.response({"error": {"code": 100, "error_subcode": 33, "message": "PRIVATE_BODY"}}, 400)
            stream = io.StringIO()
            with patch.object(m.requests, "get", side_effect=[self.response(initial), error]) as request, patch.object(m, "fetch_fb_post_insights", return_value={}), contextlib.redirect_stderr(stream):
                with self.assertRaises(m.CollectionError):
                    m.fetch_fb_posts("2026-11-01", "2026-11-02")
            first, second = request.call_args_list
            self.assertEqual(first.args[0], f"{m.BASE_URL}/{m.PAGE_ID}/published_posts")
            self.assertEqual(first.kwargs["params"], {"fields": "id,created_time,message,permalink_url,shares",
                             "since": "2026-11-01", "until": "2026-11-02", "access_token": m.TOKEN, "limit": 100})
            self.assertEqual(second.args[0], initial["paging"]["next"])
            self.assertEqual(second.kwargs["params"], {})
            self.assertIn("etapa=pagination", stream.getvalue())
            self.assertIn("http=400 meta_code=100 meta_subcode=33", stream.getvalue())
            for secret in ("PRIVATE_TOKEN", "PRIVATE_BODY", "access_token", "https://"):
                self.assertNotIn(secret, stream.getvalue())

    def test_corrected_facebook_listing_preserves_historical_type_and_comments(self):
        for m in self.modules:
            for skip in ((False, True) if m.__name__ == "backfill_posts" else (False,)):
                with self.subTest(module=m.__name__, skip_insights=skip):
                    item = {"id": "old", "created_time": "2026-11-01T12:00:00+0000",
                            "message": "Updated text", "permalink_url": "https://example.org/post",
                            "shares": {"count": 0}}
                    new_item = {**item, "id": "new"}
                    original = [{"id": "old", "data": "2026-10-31", "tipo": "photo", "com": 42,
                                 "comp": 9, "vis": 88, "custom": "historical"}]
                    snapshot = copy.deepcopy(original)
                    with patch.object(m, "api_get", return_value={"data": [item, new_item]}) as api, patch.object(m, "fetch_fb_post_insights", return_value={"post_impressions": 0}) as insights:
                        fetched = m.fetch_fb_posts("2026-11-01", "2026-11-02", skip) if m.__name__ == "backfill_posts" else m.fetch_fb_posts("2026-11-01", "2026-11-02")
                    self.assertEqual(api.call_count, 1)
                    self.assertEqual(api.call_args.args[1]["fields"], "id,created_time,message,permalink_url,shares")
                    for post in fetched:
                        self.assertNotIn("tipo", post)
                        self.assertNotIn("com", post)
                        self.assertEqual(post["comp"], 0)
                    if skip:
                        insights.assert_not_called()
                    else:
                        self.assertEqual(insights.call_count, 2)
                    merged = {p["id"]: p for p in m.merge_posts(original, fetched, "facebook", skip)}
                    self.assertEqual(original, snapshot)
                    self.assertEqual(merged["old"]["tipo"], "photo")
                    self.assertEqual(merged["old"]["com"], 42)
                    self.assertEqual(merged["old"]["comp"], 0)
                    self.assertEqual(merged["old"]["vis"], 88 if skip else 0)
                    self.assertEqual(merged["old"]["custom"], "historical")
                    self.assertNotIn("tipo", merged["new"])
                    self.assertNotIn("com", merged["new"])

    def test_facebook_dry_run_uses_corrected_fields_and_reports_preservation(self):
        for m in self.modules:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "posts.json"
                original = {"atualizado": "2020-01-01", "facebook": [{"id": "id", "data": "2026-11-01", "tipo": "video", "com": 15}], "instagram": []}
                path.write_text(json.dumps(original), encoding="utf-8")
                before = path.read_bytes()
                responses = [{"data": [{"id": "id", "created_time": "2026-11-01T12:00:00+0000", "shares": {"count": 0}}]},
                             {"instagram_business_account": {"id": "ig"}}, {"data": []}]
                argv = ["--dry-run"] if m.__name__ == "update_posts" else ["--start", "2026-11-01", "--end", "2026-11-02", "--dry-run"]
                with patch.object(m, "POSTS_PATH", str(path)), patch.object(m, "api_get", side_effect=responses) as api, patch.object(m, "fetch_fb_post_insights", return_value={}), patch.object(m, "save_posts") as save:
                    result = m.main(argv)
                save.assert_not_called()
                self.assertEqual(api.call_args_list[0].args[1]["fields"], "id,created_time,message,permalink_url,shares")
                comments = result["diagnostics"]["facebook"]["metrics"]["com"]
                self.assertEqual(comments["confirmed"], 0)
                self.assertEqual(comments["unavailable"], 1)
                self.assertEqual(comments["preserved"], 1)
                self.assertEqual(comments["zero"], 0)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_optional_insights_pagination_and_structure_categories(self):
        for m in self.modules:
            for facebook in (False, True):
                name = "post_reactions_by_type_total" if facebook else "reach"
                value = {"like": 5} if facebook else 5
                for paging, category in (({"next": "https://secret/?access_token=PRIVATE"}, "insights_pagination_unresolved"),
                                         (None, "insights_invalid_structure"),
                                         ([], "insights_invalid_structure"),
                                         ({"next": 5}, "insights_invalid_structure"),
                                         ({"next": ""}, "insights_invalid_structure"),
                                         ({"next": " "}, "insights_invalid_structure")):
                    m.reset_execution_state()
                    stream = io.StringIO()
                    payload = {"data": [{"name": name, "value": value}], "paging": paging}
                    with patch.object(m, "api_get", return_value=payload) as api, contextlib.redirect_stderr(stream):
                        result = m.fetch_insights("id", (name,), reactions=facebook)
                    self.assertEqual(result, {})
                    self.assertEqual(api.call_count, 1)
                    self.assertIn("categoria=" + category, stream.getvalue())
                    self.assertIn("metrica=" + name, stream.getvalue())
                    self.assertNotIn("PRIVATE", stream.getvalue())
                    self.assertNotIn("https://", stream.getvalue())
                    self.assertNotIn("invalid_pagination", stream.getvalue())
                    self.assertNotIn("facebook" if facebook else "instagram", m.INSIGHT_BLOCKED)
                with patch.object(m, "api_get", return_value={"data": [{"name": name, "value": value}], "paging": {"next": None}}):
                    self.assertEqual(m.fetch_insights("id", (name,), reactions=facebook), {name: 5})
                m.reset_execution_state()
                with patch.object(m, "api_get", side_effect=m.CollectionError("invalid_response", http_status=200)), contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(m.fetch_insights("id", (name,), reactions=facebook), {})
                self.assertIn("insights_invalid_structure", output.getvalue())
                self.assertIn("http=200", output.getvalue())

    def test_individual_recovery_keeps_valid_metrics_before_and_after_bad_page(self):
        for m in self.modules:
            names = ("post_impressions", "post_impressions_unique", "post_engaged_users", "post_reactions_by_type_total")
            responses = [m.CollectionError("metric_incompatible"),
                         {"data": [{"name": names[0], "value": 0}]},
                         {"data": [{"name": names[1], "value": 500}], "paging": {"next": "unresolved"}},
                         {"data": [{"name": names[2], "value": 3}]},
                         {"data": [{"name": names[3], "value": {"like": 2}}]}]
            with patch.object(m, "api_get", side_effect=responses) as api:
                self.assertEqual(m.fetch_fb_post_insights("id"), {names[0]: 0, names[2]: 3, names[3]: 2})
            self.assertEqual(api.call_count, 5)

    def test_observed_reactions_pagination_continues_inventory_and_instagram(self):
        for m in self.modules:
            for dry_run in (False, True):
                with self.subTest(module=m.__name__, dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "posts.json"
                    original = {"atualizado": "2020-01-01", "facebook": [{"id": "old", "data": "2026-11-01", "reac": 42, "vis": 88}], "instagram": []}
                    path.write_text(json.dumps(original), encoding="utf-8")
                    before = path.read_bytes()
                    item = {"id": "old", "created_time": "2026-11-01T12:00:00+0000", "shares": {"count": 0}}
                    reactions = {"data": [{"name": "post_reactions_by_type_total", "value": {"like": 999}}], "paging": {"next": "secret-insights-page"}}
                    responses = [{"data": [item], "paging": {"next": "publication-page2"}},
                                 m.CollectionError("metric_incompatible"),
                                 m.CollectionError("metric_incompatible"), m.CollectionError("metric_incompatible"),
                                 m.CollectionError("metric_incompatible"), reactions,
                                 {"data": [{**item, "id": "new"}]},
                                 {"data": [], "paging": None},
                                 {"instagram_business_account": {"id": "ig"}},
                                 {"data": [{"id": "ig-new", "timestamp": "2026-11-01T12:00:00+0000", "like_count": 0}]},
                                 {"data": [{"name": "reach", "value": 0}]}, {"data": []}]
                    argv = [] if m.__name__ == "update_posts" else ["--start", "2026-11-01", "--end", "2026-11-02"]
                    if dry_run:
                        argv += ["--dry-run"]
                    actual_save = m.save_posts
                    with patch.object(m, "POSTS_PATH", str(path)), patch.object(m, "api_get", side_effect=responses) as api, patch.object(m, "save_posts", wraps=actual_save) as save:
                        result = m.main(argv)
                    self.assertEqual(api.call_count, len(responses))
                    self.assertEqual(api.call_args_list[6].args, ("publication-page2", {}))
                    self.assertEqual(result["status"], "partial")
                    self.assertIn("instagram", result["fields"])
                    if dry_run:
                        save.assert_not_called()
                        self.assertEqual(path.read_bytes(), before)
                        metric = result["diagnostics"]["facebook"]["metrics"]["reac"]
                        self.assertEqual(metric["confirmed"], 0)
                        self.assertEqual(metric["preserved"], 1)
                    else:
                        save.assert_called_once()
                        stored = json.loads(path.read_text(encoding="utf-8"))
                        posts = {p["id"]: p for p in stored["facebook"]}
                        self.assertEqual(posts["old"]["reac"], 42)
                        self.assertEqual(posts["old"]["vis"], 88)
                        self.assertEqual(posts["old"]["comp"], 0)
                        self.assertNotIn("reac", posts["new"])
                        self.assertEqual(stored["instagram"][0]["alc"], 0)
                    self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_fatal_listing_second_page_after_optional_insight_problem(self):
        for m in self.modules:
            for failure in (m.CollectionError("service"), {}, {"data": [] , "paging": None},
                            {"data": [], "paging": {"next": "publication-page2"}}):
                for dry_run in (False, True):
                    with self.subTest(module=m.__name__, dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "posts.json"
                        before = b'{"atualizado":"2020-01-01","facebook":[],"instagram":[]}'
                        path.write_bytes(before)
                        item = {"id": "id", "created_time": "2026-11-01T12:00:00+0000"}
                        responses = [{"data": [item], "paging": {"next": "publication-page2"}},
                                     {"data": [], "paging": {"next": "insight-page"}}, failure]
                        argv = [] if m.__name__ == "update_posts" else ["--start", "2026-11-01"]
                        if dry_run:
                            argv += ["--dry-run"]
                        with patch.object(m, "POSTS_PATH", str(path)), patch.object(m, "api_get", side_effect=responses), patch.object(m, "save_posts") as save, patch.object(m, "get_ig_user_id") as account:
                            with self.assertRaises(m.CollectionError):
                                m.main(argv)
                            save.assert_not_called()
                            account.assert_not_called()
                        self.assertEqual(path.read_bytes(), before)

    def test_unexpected_internal_insight_errors_are_not_optional(self):
        for m in self.modules:
            for error in (RuntimeError("unexpected"), TypeError("unexpected"), m.CollectionError("unknown_internal")):
                with patch.object(m, "api_get", side_effect=error):
                    with self.assertRaises(type(error)):
                        m.fetch_fb_post_insights("id")
                with patch.object(m, "api_get", side_effect=[m.CollectionError("metric_incompatible"), error]):
                    with self.assertRaises(type(error)):
                        m.fetch_fb_post_insights("id")

    def test_joint_cache_reduces_requests_without_caching_individual_values(self):
        for m in self.modules:
            values = {"one": (0, 3), "two": (8, 0), "three": (12, 5)}
            requested = []

            def respond(url, params, **kwargs):
                requested.append(params["metric"])
                if "," in params["metric"] or params["metric"] == "impressions":
                    return self.response({"error": {"code": 100, "message": "Invalid metric PRIVATE_MESSAGE"}}, 400)
                object_id = url.split("/")[-2]
                value = values[object_id][0 if params["metric"] == "reach" else 1]
                return self.response({"data": [{"name": params["metric"], "value": value}]})

            stream = io.StringIO()
            with patch.object(m.requests, "get", side_effect=respond), contextlib.redirect_stderr(stream):
                results = {pid: m.fetch_insights(pid, ("impressions", "reach", "saved"), account_id="PRIVATE_ACCOUNT", media_type="IMAGE") for pid in values}
            self.assertEqual(results, {pid: {"reach": reach, "saved": saved} for pid, (reach, saved) in values.items()})
            self.assertEqual(requested.count("impressions,reach,saved"), 1)
            for name in ("impressions", "reach", "saved"):
                self.assertEqual(requested.count(name), 3)
            self.assertEqual(len(m.INCOMPATIBLE_COMBINATIONS), 1)
            stats = m.execution_summary()
            self.assertEqual(stats["requests"], 10)
            self.assertEqual(stats["queries_avoided"], 2)
            self.assertEqual(stats["warnings_repeated"], 2)
            self.assertEqual(len(stream.getvalue().splitlines()), 4)
            self.assertNotIn("PRIVATE", stream.getvalue())
            old = [{"id": pid, "data": "2026-11-01", "vis": 99} for pid in values]
            new = [{"id": pid, "alc": result["reach"], "salv": result["saved"]} for pid, result in results.items()]
            summary = {}
            merged = m.merge_posts(old, new, "instagram", summary=summary)
            self.assertEqual({p["id"]: p["alc"] for p in merged}, {"one": 0, "two": 8, "three": 12})
            self.assertTrue(all(p["vis"] == 99 for p in merged))

    def test_joint_cache_isolated_by_every_context_dimension_and_combination(self):
        for m in self.modules:
            calls = []

            def respond(url, params):
                names = params["metric"].split(",")
                calls.append(tuple(names))
                if len(names) > 1:
                    raise m.CollectionError("metric_incompatible")
                return {"data": [{"name": names[0], "value": 1}]}

            with patch.object(m, "api_get", side_effect=respond):
                base = {"account_id": "PRIVATE_ACCOUNT", "media_type": "IMAGE", "endpoint_family": "media/insights"}
                m.fetch_insights("one", ("reach", "saved"), **base)
                m.fetch_insights("two", ("saved", "reach"), **base)
                self.assertEqual(len(calls), 5)
                for change in ({"account_id": "OTHER_ACCOUNT"}, {"media_type": "VIDEO"},
                               {"endpoint_family": "other/insights"}, {"reactions": True}):
                    m.fetch_insights("three", ("reach", "saved"), **{**base, **change})
                with patch.object(m, "API_VERSION", "v-test"):
                    m.fetch_insights("four", ("reach", "saved"), **base)
                m.fetch_insights("five", ("reach", "impressions"), **base)
                self.assertEqual(sum(len(call) > 1 for call in calls), 7)
                self.assertEqual(m.execution_summary()["queries_avoided"], 1)
                # Missing account or media type must not generalize across IDs.
                for pid in ("unknown-one", "unknown-two"):
                    m.fetch_insights(pid, ("reach", "saved"))
                    m.fetch_insights(pid, ("reach", "saved"), account_id="known")
                self.assertEqual(sum(len(call) > 1 for call in calls), 11)

    def test_joint_cache_excludes_other_failures_and_single_metric_errors(self):
        for m in self.modules:
            for outcome in (m.CollectionError("authentication"), m.CollectionError("service"),
                            m.CollectionError("connection"), m.CollectionError("permission"),
                            m.CollectionError("rate_limit"), m.CollectionError("api_error"),
                            {}, {"data": []}, {"data": [], "paging": None},
                            {"data": [], "paging": {"next": "page"}}):
                m.reset_execution_state()
                with patch.object(m, "api_get", side_effect=[outcome]) if isinstance(outcome, Exception) else patch.object(m, "api_get", return_value=outcome):
                    self.assertEqual(m.fetch_insights("id", ("reach", "saved"), account_id="ig", media_type="IMAGE"), {})
                self.assertEqual(m.INCOMPATIBLE_COMBINATIONS, set())
                self.assertEqual(m.execution_summary()["queries_avoided"], 0)
            m.reset_execution_state()
            with patch.object(m, "api_get", side_effect=m.CollectionError("metric_incompatible")):
                self.assertEqual(m.fetch_insights("id", ("reach",), account_id="ig", media_type="IMAGE"), {})
            self.assertEqual(m.INCOMPATIBLE_COMBINATIONS, set())

    def test_warning_aggregation_counts_contexts_and_never_suppresses_fatal_logs(self):
        for m in self.modules:
            output, warnings = io.StringIO(), io.StringIO()
            context = m.insight_context("instagram", "id", "PRIVATE_ACCOUNT", "IMAGE")
            with contextlib.redirect_stderr(warnings):
                for _ in range(4):
                    m.log_issue("instagram", "insights", "reach", "unavailable", context=context)
                other = m.insight_context("instagram", "id", "PRIVATE_ACCOUNT", "VIDEO")
                m.log_issue("instagram", "insights", "reach", "unavailable", context=other)
                for _ in range(2):
                    m.log_issue("facebook", "pagination", "-", "service", fatal=True)
            self.assertEqual(len(warnings.getvalue().splitlines()), 4)
            self.assertEqual(warnings.getvalue().count("etapa=pagination"), 2)
            self.assertEqual(m.execution_summary()["warnings_repeated"], 3)
            m.RUN_FIELDS["instagram"] = {"updated": 8, "preserved": 2, "unavailable": 2}
            with contextlib.redirect_stdout(output):
                m.print_execution_summary()
            self.assertIn("ocorrencias=4 avisos_repetidos=3", output.getvalue())
            self.assertIn("requisicoes_realizadas=0 consultas_evitadas=0 avisos_repetidos=3 campos_afetados=2", output.getvalue())
            self.assertNotIn("PRIVATE", output.getvalue() + warnings.getvalue())

    def test_optimized_dry_run_resets_cache_and_statistics_between_executions(self):
        for m in self.modules:
            joint_queries = []

            def respond(url, params, **kwargs):
                if url.endswith("/published_posts"):
                    return self.response({"data": []})
                if params.get("fields") == "instagram_business_account":
                    return self.response({"instagram_business_account": {"id": "PRIVATE_IG"}})
                if url.endswith("/media"):
                    return self.response({"data": [{"id": pid, "timestamp": "2026-11-01T12:00:00+0000", "media_type": "IMAGE", "like_count": 0, "comments_count": 1} for pid in ("a", "b")]})
                name = params["metric"]
                if "," in name:
                    joint_queries.append(name)
                if "," in name or name == "impressions":
                    return self.response({"error": {"code": 100, "message": "Invalid metric"}}, 400)
                return self.response({"data": [{"name": name, "value": 0 if name == "reach" else 2}]})

            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "posts.json"
                original = b'{"atualizado":"2020-01-01","facebook":[],"instagram":[]}'
                path.write_bytes(original)
                argv = ["--dry-run"] if m.__name__ == "update_posts" else ["--start", "2026-11-01", "--dry-run"]
                with patch.object(m, "POSTS_PATH", str(path)), patch.object(m.requests, "get", side_effect=respond) as request, patch.object(m, "save_posts") as save:
                    first = m.main(argv)
                    snapshot = copy.deepcopy(first)
                    second = m.main(argv)
                    self.assertEqual(first, snapshot)
                    self.assertEqual(first, second)
                    self.assertEqual(request.call_count, 24)
                    save.assert_not_called()
                self.assertEqual(len(joint_queries), 2)
                self.assertEqual(second["operations"]["requests"], 12)
                self.assertEqual(second["operations"]["queries_avoided"], 1)
                self.assertEqual(second["operations"]["fields_affected"], 2)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_cached_recovery_keeps_second_publication_page_mandatory(self):
        for m in self.modules:
            item = {"created_time": "2026-11-01T12:00:00+0000", "type": "photo"}
            def respond(url, params):
                if url.endswith("/published_posts"):
                    return {"data": [{**item, "id": "one"}, {**item, "id": "two"}], "paging": {"next": "page2"}}
                if url == "page2":
                    raise m.CollectionError("service")
                names = params["metric"].split(",")
                if len(names) > 1:
                    raise m.CollectionError("metric_incompatible")
                return {"data": [{"name": names[0], "value": 0}]}
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "posts.json"
                original = b'{"atualizado":"2020-01-01","facebook":[],"instagram":[]}'
                path.write_bytes(original)
                argv = ["--dry-run"] if m.__name__ == "update_posts" else ["--start", "2026-11-01", "--dry-run"]
                with patch.object(m, "POSTS_PATH", str(path)), patch.object(m, "api_get", side_effect=respond) as api, patch.object(m, "save_posts") as save, patch.object(m, "get_ig_user_id") as account:
                    with self.assertRaises(m.CollectionError):
                        m.main(argv)
                    save.assert_not_called()
                    account.assert_not_called()
                    self.assertEqual(api.call_args.args, ("page2", {}))
                self.assertEqual(m.execution_summary()["queries_avoided"], 1)
                self.assertEqual(path.read_bytes(), original)

    def test_cached_individual_recovery_still_stops_on_general_failures(self):
        for m in self.modules:
            for category in ("authentication", "service", "connection"):
                m.reset_execution_state()
                responses = [m.CollectionError("metric_incompatible"),
                             {"data": [{"name": "reach", "value": 0}]},
                             {"data": [{"name": "saved", "value": 2}]},
                             m.CollectionError(category)]
                with patch.object(m, "api_get", side_effect=responses) as api:
                    options = {"account_id": "ig", "media_type": "IMAGE"}
                    self.assertEqual(m.fetch_insights("first", ("reach", "saved"), **options), {"reach": 0, "saved": 2})
                    self.assertEqual(m.fetch_insights("second", ("reach", "saved"), **options), {})
                    self.assertEqual(m.fetch_insights("third", ("reach", "saved"), **options), {})
                    self.assertEqual(api.call_count, 4)
                self.assertEqual(m.INSIGHT_BLOCKED["instagram"], category)
                self.assertEqual(m.execution_summary()["queries_avoided"], 1)

    def test_instagram_views_is_requested_individually_and_never_aliases_vis(self):
        for m in self.modules:
            for value in (0, 57):
                m.reset_execution_state()
                legacy = {"data": [{"name": "impressions", "value": 11},
                                   {"name": "reach", "value": 9}, {"name": "saved", "value": 2}]}
                views = {"data": [{"name": "views", "values": [{"value": value}]}]}
                with patch.object(m, "api_get", side_effect=[legacy, views]) as api:
                    result = m.fetch_ig_media_insights("media", account_id="ig", media_type="IMAGE")
                self.assertEqual(result, {"impressions": 11, "reach": 9, "saved": 2, "views": value})
                self.assertEqual(api.call_count, 2)
                self.assertEqual(api.call_args_list[0].args,
                                 (f"{m.BASE_URL}/media/insights", {"metric": "impressions,reach,saved", "access_token": m.TOKEN}))
                self.assertEqual(api.call_args_list[1].args,
                                 (f"{m.BASE_URL}/media/insights", {"metric": "views", "access_token": m.TOKEN}))
                self.assertEqual(m.INCOMPATIBLE_COMBINATIONS, set())

    def test_invalid_or_unavailable_views_preserves_previous_value_and_legacy_results(self):
        for m in self.modules:
            unavailable = [{}, {"data": []}, {"data": [{"name": "views"}]},
                           {"data": [{"name": "views", "values": []}]},
                           {"data": [{"name": "views", "value": 900}], "paging": {"next": "private-page"}},
                           m.CollectionError("metric_incompatible"), m.CollectionError("api_error"),
                           m.CollectionError("service")]
            unavailable += [{"data": [{"name": "views", "value": value}]} for value in
                            (None, True, False, "0", -1, 1.5, float("nan"), {})]
            for payload in unavailable:
                with self.subTest(module=m.__name__, payload_type=type(payload).__name__):
                    m.reset_execution_state()
                    legacy = {"data": [{"name": "reach", "value": 3}, {"name": "saved", "value": 0}]}
                    item = {"id": "id", "timestamp": "2026-11-01T12:00:00+0000", "media_type": "IMAGE"}
                    with patch.object(m, "api_get", side_effect=[{"data": [item]}, legacy, payload]):
                        posts = m.fetch_ig_posts("ig", "a", "b")
                    self.assertNotIn("views", posts[0])
                    self.assertNotIn("vis", posts[0])
                    old = [{"id": "id", "data": "2026-11-01", "vis": 45, "views": 123, "alc": 9}]
                    snapshot = copy.deepcopy(old)
                    result = m.merge_posts(old, posts, "instagram")
                    self.assertEqual(result[0]["views"], 123)
                    self.assertEqual(result[0]["vis"], 45)
                    self.assertEqual(result[0]["alc"], 3)
                    self.assertEqual(result[0]["salv"], 0)
                    self.assertEqual(old, snapshot)

    def test_views_recovery_produces_distinct_posts_and_consistent_collectors(self):
        all_results = []
        for m in self.modules:
            media = [{"id": "one", "timestamp": "2026-11-01T12:00:00+0000", "media_type": "IMAGE"},
                     {"id": "two", "timestamp": "2026-11-02T12:00:00+0000", "media_type": "IMAGE"}]
            requests = []
            def respond(url, params):
                if url.endswith("/media"):
                    return {"data": media}
                requests.append(params["metric"])
                if "," in params["metric"] or params["metric"] == "impressions":
                    raise m.CollectionError("metric_incompatible")
                pid = url.split("/")[-2]
                values = {"one": {"reach": 4, "saved": 2, "views": 0},
                          "two": {"reach": 8, "saved": 0, "views": 73}}
                return {"data": [{"name": params["metric"], "value": values[pid][params["metric"]]}]}
            with patch.object(m, "api_get", side_effect=respond):
                posts = m.fetch_ig_posts("ig", "a", "b")
            self.assertEqual(requests, ["impressions,reach,saved", "impressions", "reach", "saved", "views",
                                        "impressions", "reach", "saved", "views"])
            self.assertEqual(m.execution_summary()["queries_avoided"], 1)
            self.assertEqual(len(m.INCOMPATIBLE_COMBINATIONS), 1)
            self.assertEqual({p["id"]: p["views"] for p in posts}, {"one": 0, "two": 73})
            self.assertEqual({p["id"]: p["alc"] for p in posts}, {"one": 4, "two": 8})
            self.assertTrue(all("vis" not in p for p in posts))
            all_results.append(posts)
        self.assertEqual(all_results[0], all_results[1])

    def test_skip_insights_does_not_query_or_require_views(self):
        m = next(m for m in self.modules if m.__name__ == "backfill_posts")
        media = {"data": [{"id": "id", "timestamp": "2026-11-01T12:00:00+0000", "like_count": 0, "comments_count": 0}]}
        with patch.object(m, "api_get", return_value=media) as api, patch.object(m, "fetch_ig_media_insights") as insights:
            posts = m.fetch_ig_posts("ig", "a", "b", skip_insights=True)
        insights.assert_not_called()
        self.assertEqual(api.call_count, 1)
        self.assertNotIn("views", posts[0])
        old = [{"id": "id", "data": "2026-11-01", "vis": 4, "views": 80}]
        summary = {}
        merged = m.merge_posts(old, posts, "instagram", skip_insights=True, summary=summary)
        self.assertEqual(merged[0]["views"], 80)
        self.assertEqual(merged[0]["vis"], 4)
        self.assertEqual(summary["instagram"]["unavailable"], 0)

    def test_views_obeys_general_insight_suspension_without_losing_confirmed_metrics(self):
        for m in self.modules:
            for category in ("authentication", "permission", "service", "connection", "rate_limit"):
                m.reset_execution_state()
                with patch.object(m, "api_get", side_effect=m.CollectionError(category)) as api:
                    self.assertEqual(m.fetch_ig_media_insights("first", account_id="ig", media_type="IMAGE"), {})
                    self.assertEqual(m.fetch_ig_media_insights("second", account_id="ig", media_type="IMAGE"), {})
                    self.assertEqual(api.call_count, 1)
                m.reset_execution_state()
                legacy = {"data": [{"name": "reach", "value": 0}, {"name": "saved", "value": 3}]}
                with patch.object(m, "api_get", side_effect=[legacy, m.CollectionError(category)]) as api:
                    self.assertEqual(m.fetch_ig_media_insights("first", account_id="ig", media_type="IMAGE"), {"reach": 0, "saved": 3})
                    self.assertEqual(m.fetch_ig_media_insights("second", account_id="ig", media_type="IMAGE"), {})
                    self.assertEqual(api.call_count, 2)

    def test_views_merge_validates_values_and_reports_them_separately(self):
        for m in self.modules:
            old = [{"id": "id", "data": "2026-11-01", "vis": 10, "views": 70}]
            for value in (None, False, "0", -1, 1.5, float("nan")):
                merged = m.merge_posts(old, [{"id": "id", "views": value}], "instagram")
                self.assertEqual(merged[0]["views"], 70)
                self.assertEqual(merged[0]["vis"], 10)
            summary = {}
            m.merge_posts(old, [{"id": "id", "views": 0}], "instagram", summary=summary)
            self.assertEqual(summary["instagram"]["updated"], 1)
            self.assertEqual(summary["instagram"]["unavailable"], 6)
            report = m.diagnostic_summary("instagram", [{"id": "id", "views": 0}], old, summary["instagram"])
            self.assertEqual(report["metrics"]["views"], {"source": "views", "confirmed": 1, "unavailable": 0, "preserved": 0, "zero": 1, "positive": 0})
            self.assertEqual(report["metrics"]["vis"]["preserved"], 1)
            report = m.diagnostic_summary("instagram", [{"id": "id"}], old, summary["instagram"])
            self.assertEqual(report["metrics"]["views"]["preserved"], 1)
            self.assertEqual(report["metrics"]["views"]["unavailable"], 1)

    def test_views_full_execution_dry_run_never_writes_or_changes_legacy_vis(self):
        for m in self.modules:
            for dry_run in (False, True):
                with self.subTest(module=m.__name__, dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "posts.json"
                    original = {"atualizado": "2020-01-01", "facebook": [], "instagram": [{"id": "id", "data": "2026-11-01", "vis": 99, "views": 77}]}
                    path.write_text(json.dumps(original), encoding="utf-8")
                    before = path.read_bytes()
                    responses = [{"data": []}, {"instagram_business_account": {"id": "ig"}},
                                 {"data": [{"id": "id", "timestamp": "2026-11-01T12:00:00+0000", "media_type": "IMAGE", "like_count": 1, "comments_count": 2}]},
                                 {"data": [{"name": "reach", "value": 5}, {"name": "saved", "value": 0}]},
                                 {"data": [{"name": "views", "values": [{"value": 0}]}]}]
                    argv = [] if m.__name__ == "update_posts" else ["--start", "2026-11-01", "--end", "2026-11-02"]
                    if dry_run:
                        argv += ["--dry-run"]
                    with patch.object(m, "POSTS_PATH", str(path)), patch.object(m, "api_get", side_effect=responses) as api, patch.object(m, "save_posts", wraps=m.save_posts) as save:
                        result = m.main(argv)
                    self.assertEqual(api.call_count, 5)
                    if dry_run:
                        save.assert_not_called()
                        self.assertEqual(path.read_bytes(), before)
                        self.assertEqual(result["diagnostics"]["instagram"]["metrics"]["views"]["zero"], 1)
                        self.assertEqual(result["diagnostics"]["instagram"]["metrics"]["vis"]["preserved"], 1)
                    else:
                        save.assert_called_once()
                        stored = json.loads(path.read_text(encoding="utf-8"))
                        self.assertEqual(stored["instagram"][0]["vis"], 99)
                        self.assertEqual(stored["instagram"][0]["views"], 0)
                        self.assertEqual(stored["instagram"][0]["alc"], 5)
                        self.assertEqual(stored["instagram"][0]["salv"], 0)
                    self.assertEqual(list(Path(directory).iterdir()), [path])


class PostsStorageSafetyTests(PostsTestSetup):
    def test_timezone_conversion_is_identical_and_requires_real_offset(self):
        cases = {"2026-10-01T02:59:59Z": "2026-09-30",
                 "2026-10-01T03:00:00+0000": "2026-10-01",
                 "2026-09-30T23:59:59-03:00": "2026-09-30",
                 "2018-01-01T02:30:00Z": "2018-01-01",
                 "2026-01-02T10:00:00+09:00": "2026-01-01"}
        for m in self.modules:
            for stamp, expected in cases.items():
                self.assertEqual(m.publication_date(stamp, "instagram"), expected)
            for stamp in ("2026-01-01T12:00:00", "2026-02-30T12:00:00Z", "2026-01-01", False):
                self.assertIsNone(m.publication_date(stamp, "instagram"))

    def test_historical_date_conflict_preserves_date_and_other_valid_updates(self):
        for m in self.modules:
            previous = [{"id": "private-id", "data": "2026-09-30", "views": 40,
                         "custom": {"nested": [1, None, True]}}]
            before = copy.deepcopy(previous)
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                result = m.merge_posts(previous, [{"id": "private-id", "data": "2026-10-01", "views": 0}], "instagram")
            self.assertEqual(result[0], {**previous[0], "views": 0})
            self.assertEqual(previous, before)
            self.assertIn("historical_date_conflict", output.getvalue())
            self.assertNotIn("private-id", output.getvalue())
            self.assertEqual(m.merge_posts([], [{"id": "new", "data": "2026-10-01"}])[0]["data"], "2026-10-01")

    @staticmethod
    def document():
        return {"atualizado": "2026-01-01", "facebook": [], "instagram": [
            {"id": "old", "data": "2026-01-01", "views": 8, "vis": 0, "custom": {"list": [1, None, True]}}],
            "unknown": {"nested": [False, "preserve"]}}

    def test_existing_contract_rejects_invalid_fields_without_calls_or_writes(self):
        changes = [lambda d: d.pop("facebook"), lambda d: d.update(instagram={}),
                   lambda d: d.update(atualizado=None), lambda d: d.update(atualizado="2026-02-30"),
                   lambda d: d["instagram"].append(copy.deepcopy(d["instagram"][0])),
                   lambda d: d["instagram"][0].pop("data"), lambda d: d["instagram"][0].update(id=""),
                   lambda d: d["instagram"][0].update(id="bad id"),
                   lambda d: d["instagram"][0].update(data="2026-02-30"),
                   lambda d: d["instagram"][0].update(txt=3)]
        for value in (None, True, False, "0", -1, 1.5, float("nan"), float("inf")):
            changes.append(lambda d, value=value: d["instagram"][0].update(views=value))
        for m in self.modules:
            for change in changes:
                data = self.document(); change(data)
                path = Path(m.POSTS_PATH); raw = json.dumps(data).encode(); path.write_bytes(raw)
                argv = [] if m.__name__ == "update_posts" else ["--start", "2026-01-01"]
                with patch.object(m, "api_get") as api:
                    with self.assertRaises(m.CollectionError) as caught:
                        m.main(argv)
                    self.assertEqual(caught.exception.category, "invalid_document")
                    api.assert_not_called()
                self.assertEqual(path.read_bytes(), raw)
                self.assertFalse(Path(str(path) + ".lock").exists())

    def test_duplicate_json_keys_and_malformed_json_are_not_silently_accepted(self):
        for m in self.modules:
            for raw in (b'{"atualizado":"","facebook":[],"instagram":[],"instagram":[]}',
                        b'{"atualizado":"","facebook":[],"instagram":[{"id":"x","data":"2026-01-01","views":1,"views":0}]}',
                        b'{broken', b'\xff'):
                Path(m.POSTS_PATH).write_bytes(raw)
                with self.assertRaises(m.CollectionError):
                    m.load_posts()
                self.assertEqual(Path(m.POSTS_PATH).read_bytes(), raw)

    def test_unknown_fields_and_full_historical_records_survive_success(self):
        for m in self.modules:
            path = Path(m.POSTS_PATH); original = self.document(); path.write_text(json.dumps(original))
            argv = [] if m.__name__ == "update_posts" else ["--start", "2026-01-01", "--end", "2026-01-31"]
            with patch.object(m, "fetch_fb_posts", return_value=[]), patch.object(m, "get_ig_user_id", return_value="ig"), patch.object(m, "fetch_ig_posts", return_value=[{"id": "old", "views": None, "alc": 0}]):
                m.main(argv)
            stored = json.loads(path.read_text())
            self.assertEqual(stored["unknown"], original["unknown"])
            self.assertEqual(stored["instagram"][0], {**original["instagram"][0], "alc": 0})

    def test_lock_is_shared_between_collectors_and_does_not_get_removed_by_loser(self):
        one, two = self.modules
        with patch.object(two, "POSTS_PATH", one.POSTS_PATH):
            lock = Path(one.POSTS_PATH + ".lock")
            with one.writer_session():
                self.assertTrue(lock.exists())
                with self.assertRaises(two.CollectionError) as caught:
                    with two.writer_session():
                        self.fail("second writer acquired lock")
                self.assertEqual(caught.exception.category, "locked")
                self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())

    def test_lock_exclusion_is_effective_across_processes(self):
        for m in self.modules:
            code = '''import importlib.util,sys
spec=importlib.util.spec_from_file_location("collector",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.POSTS_PATH=sys.argv[2]
try:
 with m.writer_session(): raise SystemExit(9)
except m.CollectionError as error:
 raise SystemExit(0 if error.category=="locked" else 8)
'''
            with m.writer_session():
                result = subprocess.run([sys.executable, "-B", "-c", code, m.__file__, m.POSTS_PATH],
                                        env={**os.environ, "FB_PAGE_ACCESS_TOKEN": "FAKE_OFFLINE_TOKEN"},
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0)

    def test_existing_lock_and_lock_failure_stop_before_network(self):
        for m in self.modules:
            path=Path(m.POSTS_PATH); raw=json.dumps(self.document()).encode(); path.write_bytes(raw)
            lock=Path(str(path)+".lock");lock.write_bytes(b"existing")
            argv=[] if m.__name__=="update_posts" else ["--start","2026-01-01"]
            with patch.object(m,"api_get") as api:
                with self.assertRaises(m.CollectionError):m.main(argv)
                api.assert_not_called()
            self.assertEqual(lock.read_bytes(),b"existing");lock.unlink()
            with patch.object(m.os,"open",side_effect=PermissionError("private details")),patch.object(m,"api_get") as api:
                with self.assertRaises(m.CollectionError) as caught:m.main(argv)
                self.assertEqual(caught.exception.category,"lock_unavailable");api.assert_not_called()
            self.assertEqual(path.read_bytes(),raw)

    def test_external_change_during_collection_prevents_overwrite(self):
        for m in self.modules:
            path=Path(m.POSTS_PATH);path.write_text(json.dumps(self.document()))
            other={**self.document(),"external":"keep","atualizado":"2026-02-01"};raw=json.dumps(other).encode()
            def changed(*args,**kwargs):path.write_bytes(raw);return []
            argv=[] if m.__name__=="update_posts" else ["--start","2026-01-01"]
            with patch.object(m,"fetch_fb_posts",side_effect=changed),patch.object(m,"get_ig_user_id",return_value="ig"),patch.object(m,"fetch_ig_posts",return_value=[]):
                with self.assertRaises(m.CollectionError) as caught:m.main(argv)
            self.assertEqual(caught.exception.category,"concurrent_change")
            self.assertEqual(path.read_bytes(),raw)
            self.assertFalse(Path(str(path)+".lock").exists())

    def test_change_after_temporary_flush_is_detected_and_temporary_is_removed(self):
        for m in self.modules:
            path=Path(m.POSTS_PATH);path.write_text(json.dumps(self.document()))
            raw=json.dumps({**self.document(),"external":"late"}).encode()
            with m.writer_session(),patch.object(m.os,"fsync",side_effect=lambda fd:path.write_bytes(raw)):
                with self.assertRaises(m.CollectionError) as caught:m.save_posts(self.document())
                self.assertEqual(caught.exception.category,"concurrent_change")
            self.assertEqual(path.read_bytes(),raw)
            self.assertEqual(list(path.parent.glob('.posts-*.tmp')),[])

    def test_interval_and_budget_arguments_are_validated_before_any_request(self):
        for m in self.modules:
            base=[] if m.__name__=="update_posts" else ["--start","2026-01-01"]
            self.assertEqual(m.parse_args(base).max_requests,2000)
            self.assertEqual(m.parse_args(base).max_pages,50)
            for flag in ('--max-requests','--max-pages'):
                for value in ('0','-1','1.5','true'):
                    with patch.object(m,"api_get") as api:
                        with self.assertRaises(SystemExit):m.main(base+[flag,value])
                        api.assert_not_called()
            if m.__name__=="backfill_posts":
                for args in (["--start","bad"],["--start","2026-02-30"],["--start","2026-02-01","--end","2026-01-01"]):
                    with self.assertRaises(SystemExit):m.parse_args(args)
                self.assertTrue(m.parse_args(base+["--skip-instagram","--skip-insights"]).skip_insights)

    def test_page_budget_preserves_complete_pagination_or_fails_closed(self):
        for m in self.modules:
            m.MAX_PAGES=1
            for fn,args in ((m.fetch_fb_posts,('a','b')),(m.fetch_ig_posts,('ig','a','b'))):
                with patch.object(m,"api_get",return_value={"data":[],"paging":{"next":"page2"}}) as api:
                    with self.assertRaises(m.CollectionError) as caught:fn(*args)
                    self.assertEqual(caught.exception.category,'page_limit');self.assertEqual(api.call_count,1)
                with patch.object(m,"api_get",return_value={"data":[]}):self.assertEqual(fn(*args),[])

    def test_global_request_limit_counts_retries_and_is_fatal_inside_optional_insights(self):
        for m in self.modules:
            m.MAX_REQUESTS=2
            with patch.object(m.requests,'get',side_effect=m.requests.Timeout('private')) as request:
                with self.assertRaises(m.CollectionError) as caught:m.api_get('url',{})
                self.assertEqual(caught.exception.category,'request_limit');self.assertEqual(request.call_count,2)
            m.REQUEST_COUNTS['requests']=0;m.MAX_REQUESTS=1
            response=PostsTests.response({'error':{'code':100,'message':'invalid metric'}},400)
            with patch.object(m.requests,'get',return_value=response) as request:
                with self.assertRaises(m.CollectionError) as caught:m.fetch_ig_media_insights('id')
                self.assertEqual(caught.exception.category,'request_limit');self.assertEqual(request.call_count,1)

    def test_budget_failure_after_valid_first_page_never_saves_or_advances_date(self):
        for m in self.modules:
            path=Path(m.POSTS_PATH);raw=json.dumps(self.document()).encode();path.write_bytes(raw)
            argv=[] if m.__name__=='update_posts' else ['--start','2026-01-01']
            responses=[PostsTests.response({'data':[]}),PostsTests.response({'instagram_business_account':{'id':'ig'}})]
            with patch.object(m.requests,'get',side_effect=responses) as request:
                with self.assertRaises(m.CollectionError) as caught:m.main(argv+['--max-requests','2'])
                self.assertEqual(caught.exception.category,'request_limit');self.assertEqual(request.call_count,2)
            self.assertEqual(path.read_bytes(),raw)
            with patch.object(m,'api_get',return_value={'data':[], 'paging':{'next':'second'}}):
                with self.assertRaises(m.CollectionError) as caught:m.main(argv+['--max-pages','1'])
                self.assertEqual(caught.exception.category,'page_limit')
            self.assertEqual(path.read_bytes(),raw)

    def test_dry_run_safety_with_limits_and_unknown_fields(self):
        for m in self.modules:
            path=Path(m.POSTS_PATH);raw=json.dumps(self.document()).encode();path.write_bytes(raw)
            argv=['--dry-run'] if m.__name__=='update_posts' else ['--start','2026-01-01','--dry-run']
            with patch.object(m,'api_get',side_effect=[{'data':[]},{'instagram_business_account':{'id':'ig'}},{'data':[]}]),patch.object(m.os,'open',side_effect=AssertionError('dry run created a lock')):
                m.main(argv+['--max-pages','1','--max-requests','3'])
            self.assertEqual(path.read_bytes(),raw);self.assertEqual(list(path.parent.glob('*.lock')),[])

    def test_limits_allow_complete_collection_exactly_at_budget_and_reset_next_run(self):
        for m in self.modules:
            argv=[] if m.__name__=='update_posts' else ['--start','2026-01-01']
            responses=[PostsTests.response({'data':[]}),PostsTests.response({'instagram_business_account':{'id':'ig'}}),PostsTests.response({'data':[]})]
            for _ in range(2):
                with patch.object(m.requests,'get',side_effect=responses) as request:
                    result=m.main(argv+['--max-requests','3','--max-pages','1'])
                    self.assertEqual(request.call_count,3)
                    self.assertEqual(result['operations']['requests'],3)
                    for call in request.call_args_list:self.assertFalse(call.kwargs['allow_redirects'])
                self.assertTrue(Path(m.POSTS_PATH).exists())

    def test_redirects_cannot_bypass_budget_or_reveal_authenticated_destination(self):
        for m in self.modules:
            response=PostsTests.response({},302)
            response.headers={'Location':'https://private/?access_token=FAKE_OFFLINE_TOKEN'}
            with patch.object(m.requests,'get',return_value=response) as request:
                with self.assertRaises(m.CollectionError) as caught:m.api_get('url',{})
                self.assertEqual(caught.exception.category,'redirect_rejected')
                self.assertFalse(request.call_args.kwargs['allow_redirects'])
                self.assertEqual(request.call_count,1)
                self.assertNotIn('FAKE_OFFLINE_TOKEN',str(caught.exception))

    def test_existing_inventory_contract_is_accepted_without_modifying_project_file(self):
        path=Path(__file__).resolve().parents[1]/'dados'/'posts.json'
        raw=path.read_bytes();data=json.loads(raw)
        for m in self.modules:
            self.assertIs(m.validate_document(data),data)
        self.assertEqual(path.read_bytes(),raw)

    def test_timezone_database_failure_is_fatal_before_network_and_storage(self):
        for m in self.modules:
            argv=[] if m.__name__=='update_posts' else ['--start','2026-01-01','--end','2026-01-02']
            with patch.object(m,'ZoneInfo',side_effect=m.ZoneInfoNotFoundError('private')),patch.object(m,'api_get') as api:
                with self.assertRaises(m.CollectionError) as caught:m.main(argv)
                self.assertEqual(caught.exception.category,'timezone_unavailable');api.assert_not_called()
            self.assertFalse(Path(m.POSTS_PATH).exists())
            self.assertFalse(Path(m.POSTS_PATH+'.lock').exists())

    def test_instagram_second_page_limit_discards_in_memory_facebook_updates(self):
        for m in self.modules:
            path=Path(m.POSTS_PATH);raw=json.dumps(self.document()).encode();path.write_bytes(raw)
            argv=[] if m.__name__=='update_posts' else ['--start','2026-01-01','--end','2026-01-31']
            fb={'id':'new-fb','created_time':'2026-01-02T12:00:00Z','shares':{'count':0}}
            ig={'id':'old','timestamp':'2026-01-01T12:00:00Z','like_count':0,'comments_count':0}
            responses=[{'data':[fb]},{'instagram_business_account':{'id':'ig'}},
                       {'data':[ig],'paging':{'next':'next-page'}}]
            with patch.object(m,'api_get',side_effect=responses) as api,patch.object(m,'fetch_fb_post_insights',return_value={}),patch.object(m,'fetch_ig_media_insights',return_value={'views':0}):
                with self.assertRaises(m.CollectionError) as caught:m.main(argv+['--max-pages','1'])
                self.assertEqual(caught.exception.category,'page_limit');self.assertEqual(api.call_count,3)
            self.assertEqual(path.read_bytes(),raw)
            self.assertFalse(Path(m.POSTS_PATH+'.lock').exists())

    def test_file_created_by_external_writer_after_missing_baseline_is_not_overwritten(self):
        for m in self.modules:
            path=Path(m.POSTS_PATH);raw=json.dumps(self.document()).encode()
            def create(*args,**kwargs):path.write_bytes(raw);return []
            argv=[] if m.__name__=='update_posts' else ['--start','2026-01-01','--end','2026-01-31']
            with patch.object(m,'fetch_fb_posts',side_effect=create),patch.object(m,'get_ig_user_id',return_value='ig'),patch.object(m,'fetch_ig_posts',return_value=[]):
                with self.assertRaises(m.CollectionError) as caught:m.main(argv)
                self.assertEqual(caught.exception.category,'concurrent_change')
            self.assertEqual(path.read_bytes(),raw)


if __name__ == "__main__":
    unittest.main()
