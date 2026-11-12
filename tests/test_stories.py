"""Offline story collector tests. Every HTTP request and socket is blocked.

All write tests use TemporaryDirectory, never the project's data files.
"""

import contextlib
import copy
from datetime import datetime, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests


PAGE = "91110000"
USER = "92220000"
ONE = "93330001"
TWO = "93330002"


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []
        self.adapters = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responder(url, kwargs)
        if isinstance(response, Exception):
            raise response
        return response


class StoriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        filename = Path(__file__).resolve().parents[1] / "scripts" / "update_stories.py"
        spec = importlib.util.spec_from_file_location("update_stories", filename)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP forbidden")))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Socket forbidden")))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Socket forbidden")))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))
        self.zone = ZoneInfo("America/Sao_Paulo")
        self.now = "2026-11-12T20:00:00+00:00"
        self.stack.enter_context(patch.object(self.m, "utc_now", side_effect=lambda: self.now))
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "stories.json"
        self.stack.enter_context(patch.object(self.m, "STORIES_PATH", self.path))

    def item(self, key=ONE, timestamp="2026-11-12T04:00:00+0000"):
        return {"id": key, "timestamp": timestamp}

    def metric(self, name, value=10, shape="values"):
        content = {"values": [{"value": value}]} if shape == "values" else {shape: {"value": value} if shape == "total_value" else value}
        return {"data": [{"name": name, "period": "lifetime", **content}]}

    def following(self, cursor="PRIVATE_CURSOR", user=USER):
        return self.m.BASE_URL + "/" + user + "/stories?access_token=PRIVATE_TOKEN&after=" + cursor

    def responder(self, url, kwargs):
        path = url.removeprefix(self.m.BASE_URL + "/")
        if path == PAGE:
            return Response({"instagram_business_account": {"id": USER}})
        if path == USER + "/stories":
            return Response({"data": [self.item(), self.item(TWO)]})
        if path in (ONE + "/insights", TWO + "/insights"):
            metric = kwargs["params"]["metric"]
            return Response(self.metric(metric, 10 if path.startswith(ONE) else 20))
        raise AssertionError("Unexpected request")

    def collect(self, data=None, responder=None, budget=100, pages=10):
        session = Session(responder or self.responder)
        client = self.m.Client(session, "PRIVATE_TOKEN", budget)
        result, run = self.m.collect(data or self.m.empty_document(), client, PAGE, self.zone, pages)
        return result, run, session

    def run_main(self, argv=None, responder=None):
        session = Session(responder or self.responder)
        with patch.dict(os.environ, {"FB_PAGE_ACCESS_TOKEN": "PRIVATE_TOKEN", "FB_PAGE_ID": PAGE}), patch.object(self.m.requests, "Session", return_value=session):
            code = self.m.main(argv or [])
        return code, session

    def assert_safe_logs(self):
        for value in ("PRIVATE_TOKEN", "PRIVATE_CURSOR", "PRIVATE_URL", PAGE, USER, ONE, TWO,
                      "access_token", "https://", "Traceback", "RAW_RESPONSE"):
            self.assertNotIn(value, self.output.getvalue())

    def test_empty_project_document_has_no_invented_history(self):
        with self.subTest(inventory="temporary_initialization"):
            self.assertFalse(self.path.exists())
            data, original = self.m.load_document(self.path, self.zone)
            self.assertIsNone(original)
            self.assertEqual(data, self.m.empty_document())
            self.assertEqual(data["stories"], {})
            self.assertEqual(data["runs"], [])
            self.assertIsNone(data["source"]["account_fingerprint"])
            self.m.validate_document(data, self.zone)
            self.assertFalse(self.path.exists())

            fixture = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            self.path.write_bytes(fixture)
            loaded, raw = self.m.load_document(self.path, self.zone)
            self.assertEqual(loaded, data)
            self.assertEqual(raw, fixture)
            self.assertEqual(self.path.read_bytes(), fixture)

        with self.subTest(inventory="real_contract_read_only"):
            path = Path(__file__).resolve().parents[1] / "dados" / "stories.json"
            before = path.read_bytes()
            data, raw = self.m.load_document(path, self.zone)
            snapshot = copy.deepcopy(data)
            self.m.validate_document(data, self.zone)
            self.assertEqual(data, snapshot)
            self.assertEqual(raw, before)
            self.assertEqual(path.read_bytes(), before)

    def test_successful_observation_is_separate_per_story_and_uses_only_views_reach(self):
        original = self.m.empty_document()
        result, run, session = self.collect(original)
        self.assertEqual(original, self.m.empty_document())
        self.assertEqual(set(result["stories"]), {ONE, TWO})
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["listing_status"], "complete")
        self.assertEqual(run["distinct_stories_observed"], 2)
        self.assertEqual(run["requests_used"], 6)
        for key, value in ((ONE, 10), (TWO, 20)):
            record = result["stories"][key]
            self.assertEqual(record["publication_date_brt"], "2026-11-12")
            self.assertEqual(record["first_seen_at"], self.now)
            self.assertEqual(record["last_seen_at"], self.now)
            self.assertEqual(self.m.latest_confirmed(record, "views")["value"], value)
            self.assertEqual(len(record["snapshots"]), 1)
        for url, kwargs in session.calls:
            self.assertTrue(url.startswith(self.m.BASE_URL + "/"))
            self.assertEqual(kwargs["timeout"], self.m.TIMEOUT)
            self.assertFalse(kwargs["allow_redirects"])
            if url.endswith("/insights"):
                self.assertIn(kwargs["params"]["metric"], ("views", "reach"))
        self.assertNotIn("impressions", json.dumps(result))

    def test_expiration_or_empty_listing_preserves_records_last_seen_and_snapshots(self):
        data, _, _ = self.collect()
        before = copy.deepcopy(data["stories"])
        self.now = "2026-11-14T20:00:00+00:00"
        def empty(url, kwargs):
            return Response({"data": []}) if url.endswith("/stories") else self.responder(url, kwargs)
        result, run, session = self.collect(data, empty)
        self.assertEqual(result["stories"], before)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["distinct_stories_observed"], 0)
        self.assertEqual(run["insights_status"], "not_needed")
        self.assertEqual(len(session.calls), 2)
        self.assertNotIn("series", result)
        self.assertNotIn("daily_totals", result)

    def test_snapshots_are_append_only_latest_is_not_a_sum_and_confirmed_decreases_count(self):
        data, _, _ = self.collect()
        first_seen = data["stories"][ONE]["first_seen_at"]
        self.now = "2026-11-13T01:00:00+00:00"
        def lower(url, kwargs):
            if url.endswith("/insights"):
                return Response(self.metric(kwargs["params"]["metric"], 3))
            return self.responder(url, kwargs)
        result, _, _ = self.collect(data, lower)
        story = result["stories"][ONE]
        self.assertEqual(len(story["snapshots"]), 2)
        self.assertEqual(story["first_seen_at"], first_seen)
        self.assertEqual(story["last_seen_at"], self.now)
        self.assertEqual(self.m.latest_confirmed(story, "views")["value"], 3)
        self.assertEqual(data["stories"][ONE]["snapshots"][0], story["snapshots"][0])
        again, _, _ = self.collect(result, lower)
        self.assertEqual(len(again["stories"][ONE]["snapshots"]), 3)
        self.assertEqual(self.m.latest_confirmed(again["stories"][ONE], "views")["value"], 3)

    def test_failed_later_metric_keeps_its_previous_confirmation_and_updates_other_metric(self):
        data, _, _ = self.collect()
        self.now = "2026-11-13T01:00:00+00:00"
        def partial(url, kwargs):
            if url.endswith("/insights"):
                metric = kwargs["params"]["metric"]
                return Response({"data": []}) if metric == "views" else Response(self.metric(metric, 0))
            return self.responder(url, kwargs)
        result, run, _ = self.collect(data, partial)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["listing_status"], "complete")
        story = result["stories"][ONE]
        self.assertEqual(self.m.latest_confirmed(story, "views"), self.m.latest_confirmed(data["stories"][ONE], "views"))
        self.assertEqual(self.m.latest_confirmed(story, "reach")["value"], 0)
        self.assertEqual(story["snapshots"][-1]["metrics"]["views"]["status"], "unavailable")

    def test_deduplication_within_pages_across_pages_and_runs(self):
        def paged(url, kwargs):
            if url.endswith("/stories"):
                if "after" in kwargs["params"]:
                    return Response({"data": [self.item(), self.item(TWO)]})
                return Response({"data": [self.item(), self.item()], "paging": {"next": self.following()}})
            return self.responder(url, kwargs)
        data, run, session = self.collect(responder=paged)
        self.assertEqual(run["duplicates"], 2)
        self.assertEqual(run["distinct_stories_observed"], 2)
        self.assertEqual(len(session.calls), 7)
        again, _, _ = self.collect(data, paged)
        self.assertEqual(len(again["stories"]), 2)
        self.assertTrue(all(len(x["snapshots"]) == 2 for x in again["stories"].values()))
        self.assert_safe_logs()

    def test_first_and_last_observation_are_distinct_for_duplicate_on_later_page(self):
        start = datetime.fromisoformat(self.now)
        instants = iter((start + timedelta(seconds=i)).isoformat() for i in range(100))
        def paged(url, kwargs):
            if url.endswith("/stories"):
                return Response({"data": [self.item()]}) if "after" in kwargs["params"] else Response({"data": [self.item()], "paging": {"next": self.following()}})
            return self.responder(url, kwargs)
        with patch.object(self.m, "utc_now", side_effect=lambda: next(instants)):
            result, _, _ = self.collect(responder=paged)
        story = result["stories"][ONE]
        self.assertEqual(story["first_seen_at"], (start + timedelta(seconds=1)).isoformat())
        self.assertEqual(story["last_seen_at"], (start + timedelta(seconds=2)).isoformat())

    def test_timezone_midnight_quarter_boundary_and_historical_dst(self):
        self.assertEqual(self.m.local_date("2026-10-01T02:59:59Z", self.zone), "2026-09-30")
        self.assertEqual(self.m.local_date("2026-10-01T03:00:00Z", self.zone), "2026-10-01")
        self.assertEqual(self.m.local_date("2018-01-01T02:30:00Z", self.zone), "2018-01-01")
        self.assertEqual(self.m.parse_stamp("2026-11-12T01:00:00-0300"), self.m.parse_stamp("2026-11-12T04:00:00Z"))

    def test_invalid_dates_future_timestamps_and_ids_are_not_stored(self):
        def bad(url, kwargs):
            if url.endswith("/stories"):
                return Response({"data": [self.item(), self.item("PRIVATE_TOKEN"),
                                          self.item(TWO, "2026-02-30T00:00:00Z"),
                                          self.item(TWO, "2026-11-12T04:00:00"),
                                          self.item(TWO, "2026-11-14T04:00:00Z"), None]})
            return self.responder(url, kwargs)
        data, run, _ = self.collect(responder=bad)
        self.assertEqual(set(data["stories"]), {ONE})
        self.assertEqual(run["invalid_records"], 5)
        self.assertEqual(run["listing_status"], "partial")
        self.assertEqual(data["stories"][ONE]["snapshots"][0]["inventory_observation"], "partial")
        self.assert_safe_logs()

    def test_conflicting_timestamps_quarantine_id_and_preserve_previous_record(self):
        data, _, _ = self.collect()
        def conflicting(url, kwargs):
            if url.endswith("/stories"):
                return Response({"data": [self.item(), self.item(timestamp="2026-11-12T05:00:00Z")]})
            return self.responder(url, kwargs)
        result, run, session = self.collect(data, conflicting)
        self.assertEqual(result["stories"], data["stories"])
        self.assertEqual(run["conflicting_timestamps"], 1)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(len(session.calls), 2)

    def test_equivalent_timestamp_offsets_are_not_conflicts(self):
        data, _, _ = self.collect()
        def normalized(url, kwargs):
            if url.endswith("/stories"):
                return Response({"data": [self.item(timestamp="2026-11-12T01:00:00-03:00")]})
            return self.responder(url, kwargs)
        _, run, _ = self.collect(data, normalized)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["conflicting_timestamps"], 0)

    def test_second_page_failure_keeps_only_observations_and_marks_partial(self):
        def failure(url, kwargs):
            if url.endswith("/stories"):
                if "after" in kwargs["params"]:
                    return Response({"error": {"code": 2}}, 503)
                return Response({"data": [self.item()], "paging": {"next": self.following()}})
            return self.responder(url, kwargs)
        data, run, session = self.collect(responder=failure)
        self.assertEqual(run["listing_status"], "partial")
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["pages_received"], 1)
        self.assertEqual(set(data["stories"]), {ONE})
        self.assertIsNone(self.m.latest_confirmed(data["stories"][ONE], "views"))
        self.assertEqual(len(session.calls), 3)
        self.assert_safe_logs()

    def test_page_limit_and_cyclic_pagination_do_not_become_complete(self):
        def endless(url, kwargs):
            return Response({"data": [self.item()], "paging": {"next": self.following()}}) if url.endswith("/stories") else self.responder(url, kwargs)
        _, run, _ = self.collect(responder=endless, pages=1)
        self.assertEqual(run["listing_status"], "partial")
        self.assertEqual(run["pagination_status"], "pending")
        _, run, session = self.collect(responder=endless, pages=5)
        self.assertEqual(run["pagination_status"], "pagination_cycle")
        self.assertEqual(len([x for x, _ in session.calls if x.endswith("/stories")]), 2)

    def test_pagination_rebuilds_requests_and_rejects_untrusted_urls(self):
        for next_value in (False, "", "https://PRIVATE_URL/?access_token=PRIVATE_TOKEN",
                           self.m.BASE_URL + "/other/stories?after=x", self.following() + "&after=other"):
            def invalid(url, kwargs):
                return Response({"data": [self.item()], "paging": {"next": next_value}}) if url.endswith("/stories") else self.responder(url, kwargs)
            _, run, session = self.collect(responder=invalid)
            self.assertEqual(run["pagination_status"], "invalid_pagination")
            self.assertTrue(all(url.startswith(self.m.BASE_URL + "/") for url, _ in session.calls))
        self.assert_safe_logs()

    def test_budget_counts_failures_and_cannot_be_exceeded(self):
        for status in (200, 500):
            session = Session(lambda *args: Response({}, status))
            client = self.m.Client(session, "PRIVATE_TOKEN", 3)
            for _ in range(3):
                client.stopped = False
                try:
                    client.get(PAGE, {})
                except self.m.Problem:
                    pass
            with self.assertRaises(self.m.Problem) as error:
                client.get(PAGE, {})
            self.assertEqual(error.exception.category, "budget_exhausted")
            self.assertEqual(len(session.calls), 3)
        data, run, session = self.collect(budget=3)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(len(data["stories"]), 2)
        self.assertEqual(run["requests_used"], 3)
        self.assertIsNotNone(self.m.latest_confirmed(data["stories"][ONE], "views"))
        self.assertIsNone(self.m.latest_confirmed(data["stories"][ONE], "reach"))

    def test_invalid_limits_are_rejected_before_requests(self):
        for value in (0, -1, 501, True, "100"):
            with self.assertRaises(self.m.Problem):
                self.m.Client(Session(self.responder), "PRIVATE_TOKEN", value)
        for argv in (["--max-requests", "501"], ["--max-requests", "0"], ["--max-pages", "51"]):
            with self.assertRaises(SystemExit):
                self.m.parse_args(argv)

    def test_valid_shapes_confirm_zero_and_positive_without_aliases(self):
        for shape in ("values", "value", "total_value"):
            for value in (0, 15):
                result = self.m.parse_metric(self.metric("views", value, shape), "views")
                self.assertEqual(result["value"], value)
                self.assertEqual(result["status"], "confirmed")
                self.assertEqual(result["api_metric"], "views")
                self.assertEqual(result["response_structure"], shape)

    def test_missing_null_bool_negative_fraction_and_other_invalid_values(self):
        for value in (None, True, False, -1, 1.5, "0", float("nan"), float("inf"), {}, []):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(self.m.Problem):
                self.m.parse_metric(self.metric("views", value), "views")
        for payload in ({}, {"data": []}, {"data": [None]},
                        {"data": [{"name": "reach", "value": 10}]},
                        {"data": [{"name": "views", "value": 10}] * 2},
                        {"data": [{"name": "views", "values": []}]},
                        {"data": [{"name": "views", "values": [{"value": 1}, {"value": 2}]}]},
                        {"data": [{"name": "views", "value": 1, "total_value": {"value": 1}}]},
                        {"data": [{"name": "views", "period": "day", "value": 1}]}):
            with self.assertRaises(self.m.Problem):
                self.m.parse_metric(payload, "views")

    def test_pending_insight_pagination_never_confirms_first_page(self):
        for paging in ({"next": "https://PRIVATE_URL/"}, {"next": False}, []):
            with self.assertRaises(self.m.Problem):
                self.m.parse_metric({**self.metric("views"), "paging": paging}, "views")

    def test_general_failure_suspends_insights_but_preserves_prior_confirmations(self):
        original, _, _ = self.collect()
        def error(url, kwargs):
            if url.endswith("/insights"):
                return Response({"error": {"code": 190, "message": "PRIVATE_TOKEN https://PRIVATE_URL/"}}, 400)
            return self.responder(url, kwargs)
        result, run, session = self.collect(original, error)
        self.assertEqual(len([url for url, _ in session.calls if url.endswith("/insights")]), 1)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(self.m.latest_confirmed(result["stories"][ONE], "views"), self.m.latest_confirmed(original["stories"][ONE], "views"))
        self.assert_safe_logs()

    def test_incompatible_metric_does_not_block_other_metric(self):
        def error(url, kwargs):
            if url.endswith("/insights") and kwargs["params"]["metric"] == "views":
                return Response({"error": {"code": 100, "message": "Invalid metric PRIVATE_TOKEN"}}, 400)
            return self.responder(url, kwargs)
        result, run, _ = self.collect(responder=error)
        self.assertEqual(run["status"], "partial")
        self.assertIsNone(self.m.latest_confirmed(result["stories"][ONE], "views"))
        self.assertEqual(self.m.latest_confirmed(result["stories"][ONE], "reach")["value"], 10)
        self.assert_safe_logs()

    def test_account_failures_preserve_existing_history_and_record_failure(self):
        original, _, _ = self.collect()
        for response in (Response({"error": {"code": 190}}, 400), Response({}),
                         Response({"instagram_business_account": {"id": "PRIVATE_TOKEN"}})):
            result, run, session = self.collect(original, lambda *args: response)
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["listing_status"], "not_started")
            self.assertEqual(result["stories"], original["stories"])
            self.assertEqual(len(session.calls), 1)

    def test_changed_instagram_account_cannot_contaminate_inventory(self):
        original, _, _ = self.collect()
        result, run, session = self.collect(original, lambda *args: Response({"instagram_business_account": {"id": "99999999"}}))
        self.assertEqual(result["stories"], original["stories"])
        self.assertEqual(result["source"], original["source"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(len(session.calls), 1)
        self.assertIn("categoria=account_mismatch", self.output.getvalue())

    def test_raw_responses_tokens_cursors_and_unknown_api_fields_are_not_persisted(self):
        def injected(url, kwargs):
            response = self.responder(url, kwargs)
            response.payload["RAW_RESPONSE"] = "PRIVATE_TOKEN https://PRIVATE_URL/"
            if url.endswith("/stories"):
                response.payload["data"][0]["caption"] = "PRIVATE_TOKEN"
                response.payload["paging"] = {"cursors": {"after": "PRIVATE_CURSOR"}}
            return response
        result, _, _ = self.collect(responder=injected)
        text = json.dumps(result)
        for secret in ("PRIVATE_TOKEN", "PRIVATE_CURSOR", "PRIVATE_URL", "RAW_RESPONSE", "access_token", "paging", "caption"):
            self.assertNotIn(secret, text)
        self.assert_safe_logs()

    def test_unknown_safe_stored_fields_and_old_snapshots_are_preserved(self):
        original, _, _ = self.collect()
        original["extension"] = {"version": 7}
        original["stories"][ONE]["annotation"] = "retained"
        result, _, _ = self.collect(original)
        self.assertEqual(result["extension"], original["extension"])
        self.assertEqual(result["stories"][ONE]["annotation"], "retained")
        self.assertEqual(result["stories"][ONE]["snapshots"][0], original["stories"][ONE]["snapshots"][0])

    def test_atomic_save_roundtrip_and_no_temporary_files_left(self):
        data, _, _ = self.collect()
        with self.m.exclusive_lock(self.path):
            self.m.save_document(self.path, data, None, self.zone)
        loaded, original = self.m.load_document(self.path, self.zone)
        self.assertEqual(loaded, data)
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(list(Path(self.directory).iterdir()), [self.path])

    def test_serialization_fsync_and_replace_failures_preserve_original_bytes(self):
        data, _, _ = self.collect()
        original = b'ORIGINAL_BYTES'
        for target in ("json.dumps", "os.fsync", "os.replace"):
            self.path.write_bytes(original)
            owner, attr = target.split(".")
            with patch.object(getattr(self.m, owner), attr, side_effect=ValueError("PRIVATE_TOKEN") if attr == "dumps" else OSError("PRIVATE_TOKEN")):
                with self.assertRaises((self.m.Problem, OSError)):
                    self.m.save_document(self.path, data, original, self.zone)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list(Path(self.directory).iterdir()), [self.path])

    def test_write_and_flush_failures_preserve_original_and_cleanup_temp(self):
        data, _, _ = self.collect()
        real_temp = self.m.tempfile.NamedTemporaryFile
        for failing in ("write", "flush"):
            self.path.write_bytes(b"ORIGINAL")
            class Broken:
                def __init__(self, **kwargs):
                    self.real = real_temp(**kwargs)
                    self.name = self.real.name
                def __enter__(self):
                    self.real.__enter__()
                    return self
                def __exit__(self, *args):
                    return self.real.__exit__(*args)
                def write(self, text):
                    if failing == "write":
                        raise OSError("PRIVATE_TOKEN")
                    return self.real.write(text)
                def flush(self):
                    if failing == "flush":
                        raise OSError("PRIVATE_TOKEN")
                    return self.real.flush()
                def fileno(self):
                    return self.real.fileno()
            with patch.object(self.m.tempfile, "NamedTemporaryFile", Broken), self.assertRaises(OSError):
                self.m.save_document(self.path, data, b"ORIGINAL", self.zone)
            self.assertEqual(self.path.read_bytes(), b"ORIGINAL")
            self.assertEqual(list(Path(self.directory).iterdir()), [self.path])

    def test_concurrent_external_modification_is_not_overwritten(self):
        data, _, _ = self.collect()
        self.path.write_bytes(b"EXTERNAL")
        with self.assertRaises(self.m.Problem) as error:
            self.m.save_document(self.path, data, b"ORIGINAL", self.zone)
        self.assertEqual(error.exception.category, "concurrent_change")
        self.assertEqual(self.path.read_bytes(), b"EXTERNAL")
        self.assertEqual(list(Path(self.directory).iterdir()), [self.path])

    def test_exclusive_lock_prevents_second_writer_and_is_released(self):
        with self.m.exclusive_lock(self.path):
            with self.assertRaises(self.m.Problem) as error:
                with self.m.exclusive_lock(self.path):
                    self.fail("Second writer entered")
            self.assertEqual(error.exception.category, "locked")
            self.assertTrue(Path(str(self.path) + ".lock").exists())
        self.assertFalse(Path(str(self.path) + ".lock").exists())
        with self.assertRaises(RuntimeError):
            with self.m.exclusive_lock(self.path):
                raise RuntimeError()
        self.assertFalse(Path(str(self.path) + ".lock").exists())

    def test_schema_corruption_nonfinite_and_duplicate_keys_fail_without_reset(self):
        for raw in (b"{}", b'{"schema_version":2}', b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}', b"INVALID"):
            self.path.write_bytes(raw)
            with self.assertRaises(self.m.Problem):
                self.m.load_document(self.path, self.zone)
            self.assertEqual(self.path.read_bytes(), raw)
        valid, _, _ = self.collect()
        for mutator in (lambda d: d["stories"][ONE].update(publication_date_brt="2026-11-11"),
                        lambda d: d["stories"][ONE]["snapshots"][0]["metrics"]["views"].update(value=True),
                        lambda d: d["stories"][ONE]["snapshots"].append(d["stories"][ONE]["snapshots"][0]),
                        lambda d: d["runs"].append(d["runs"][0])):
            broken = copy.deepcopy(valid)
            mutator(broken)
            with self.assertRaises(self.m.Problem):
                self.m.validate_document(broken, self.zone)

    def test_full_execution_persists_and_dry_run_simulates_identical_processing(self):
        code, session = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(session.adapters["https://"].max_retries.total, 0)
        first = self.path.read_bytes()
        persisted = json.loads(first)
        self.assertEqual(len(persisted["stories"]), 2)
        self.now = "2026-11-13T01:00:00+00:00"
        code, _ = self.run_main(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_bytes(), first)
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(self.path.read_bytes())["stories"][ONE]["snapshots"]), 2)
        self.assertEqual(list(Path(self.directory).iterdir()), [self.path])

    def test_dry_run_never_saves_locks_or_creates_temp_on_success_partial_or_failure(self):
        for response in (None, Response({"error": {"code": 190}}, 400), RuntimeError("PRIVATE_TOKEN")):
            responder = self.responder if response is None else lambda *args: response
            with patch.object(self.m, "save_document", side_effect=AssertionError("Save forbidden")), patch.object(self.m, "exclusive_lock", side_effect=AssertionError("Lock forbidden")), patch.object(self.m.tempfile, "NamedTemporaryFile", side_effect=AssertionError("Temp forbidden")):
                code, _ = self.run_main(["--dry-run"], responder)
            self.assertEqual(code, 0 if response is None else 2 if isinstance(response, Response) else 1)
            self.assertEqual(list(Path(self.directory).iterdir()), [])
        self.assert_safe_logs()

    def test_main_write_failure_and_lock_conflict_are_safe_fatal_results(self):
        initial = json.dumps(self.m.empty_document()).encode()
        self.path.write_bytes(initial)
        with patch.object(self.m.os, "replace", side_effect=OSError("PRIVATE_TOKEN https://PRIVATE_URL/")):
            code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_bytes(), initial)
        with self.m.exclusive_lock(self.path):
            code, session = self.run_main()
            self.assertEqual(code, 1)
            self.assertEqual(len(session.calls), 0)
        self.assert_safe_logs()

    def test_failed_observation_is_recorded_without_changing_old_story_values(self):
        self.run_main()
        before = json.loads(self.path.read_bytes())
        code, _ = self.run_main(responder=lambda *args: Response({"error": {"code": 190}}, 400))
        after = json.loads(self.path.read_bytes())
        self.assertEqual(code, 2)
        self.assertEqual(after["stories"], before["stories"])
        self.assertEqual(after["runs"][-1]["status"], "failed")

    def test_main_external_change_during_collection_is_preserved(self):
        initial = json.dumps(self.m.empty_document()).encode()
        self.path.write_bytes(initial)
        def changed(url, kwargs):
            self.path.write_bytes(b"EXTERNAL")
            return self.responder(url, kwargs)
        code, _ = self.run_main(responder=changed)
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_bytes(), b"EXTERNAL")
        self.assertIn("categoria=concurrent_change", self.output.getvalue())

    def test_redirects_connections_invalid_json_and_errors_never_leak_or_retry(self):
        for response in (Response({}, 302), requests.Timeout("PRIVATE_TOKEN https://PRIVATE_URL/"),
                         Response(ValueError("PRIVATE_TOKEN")), Response({"error": {"code": "PRIVATE_TOKEN", "error_subcode": "PRIVATE_URL", "message": "RAW_RESPONSE"}}, 400)):
            _, run, session = self.collect(responder=lambda *args: response)
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(run["status"], "failed")
            self.assert_safe_logs()

    def test_import_and_missing_environment_do_not_query_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(self.m.requests, "Session", side_effect=AssertionError("No session expected")):
            self.assertEqual(self.m.main(["--dry-run"]), 1)
        self.assertEqual(list(Path(self.directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
