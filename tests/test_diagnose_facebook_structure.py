"""Offline one-GET structure contracts. All response contents are synthetic."""
import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import diagnose_facebook_structure as d

TOKEN, PAGE = 'SYNTHETIC_SECRET', '123456789'
RAW = 'SYNTHETIC_PII https://example.invalid/?token=SYNTHETIC_SECRET cursor message'


def payload():
    return {'data': [{'name': d.METRIC, 'period': 'day', 'values': [
        {'value': 87654321, 'end_time': '2026-11-13T00:00:00-0300'}]}],
        'paging': {'next': RAW, 'previous': RAW}}


def response(body=None, raw=None):
    result = Mock(status=200)
    result.read.return_value = json.dumps(body).encode() if raw is None else raw
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class StructureDiagnostic(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo',
                     'requests.sessions.Session.request'):
            self.stack.enter_context(patch(name, side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.dict(os.environ, {'FB_PAGE_ACCESS_TOKEN': TOKEN, 'FB_PAGE_ID': PAGE}))
        self.opener = Mock(); self.opener.open.return_value = response(payload())
        self.build = self.stack.enter_context(patch.object(d.fb.urllib.request, 'build_opener', return_value=self.opener))

    def test_no_authorization_precedes_credentials_disk_and_network(self):
        out = io.StringIO()
        original_get = os.environ.get
        def get(key, *args):
            if key in ('FB_PAGE_ACCESS_TOKEN', 'FB_PAGE_ID'):
                raise AssertionError('credentials read')
            return original_get(key, *args)
        with patch.object(os.environ, 'get', side_effect=get), \
                patch('builtins.open', side_effect=AssertionError('disk access')), contextlib.redirect_stdout(out):
            self.assertEqual(d.main([]), 1)
        self.assertEqual(json.loads(out.getvalue())['status'], 'explicit_authorization_required')
        self.build.assert_not_called()

    def test_one_get_fixed_metric_window_header_and_safe_structure(self):
        report = d.Execution().run(authorized=True)
        self.assertEqual(report['status'], 'structure_valid_navigation_pending')
        self.assertEqual(report['attempts'], 1)
        req = self.opener.open.call_args.args[0]
        parts = urllib.parse.urlsplit(req.full_url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path),
                         ('https', 'graph.facebook.com', '/v21.0/' + PAGE + '/insights'))
        self.assertEqual(urllib.parse.parse_qs(parts.query), {'metric': ['page_media_view'], 'period': ['day'],
            'since': ['2026-11-12'], 'until': ['2026-11-13']})
        self.assertEqual(req.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertEqual(req.get_method(), 'GET')
        self.assertNotIn(TOKEN, req.full_url)
        self.assertEqual(self.opener.open.call_args.kwargs, {'timeout': 20})
        self.assertEqual(report['structure'], {'paging_present': True, 'paging_type': 'object',
            'next_present': True, 'next_type': 'string', 'previous_present': True,
            'data_entries': 1, 'data_entries_capped': False,
            'values_observations': 1, 'values_observations_capped': False})
        self.assertFalse(report['metric_approved']); self.assertFalse(report['publication_enabled'])
        self.assertEqual(report['temporal_policy'], 'pending')
        self.opener.open.assert_called_once(); self.build.assert_called_once()

    def test_execution_cannot_restart_after_http_or_transport_failure(self):
        for error, status in ((RuntimeError(RAW), 'transport_error'),
                (urllib.error.HTTPError(RAW, 400, RAW, {}, Mock()), 'http_error')):
            self.opener.open.reset_mock(); self.opener.open.side_effect = error
            execution = d.Execution()
            report = execution.run(authorized=True)
            with self.assertRaisesRegex(d.DiagnosticError, 'execution_already_used'):
                execution.run(authorized=True)
            self.assertEqual(report['attempts'], 1)
            self.assertEqual(report['status'], status)
            self.opener.open.assert_called_once()

    def test_transport_budget_cannot_reset_even_when_client_allows_six(self):
        client = d.fb.Client(TOKEN)
        budget = d.OneGetOpener(client.opener); client.opener = budget
        self.opener.open.side_effect = RuntimeError(RAW)
        for _ in range(6):
            with self.assertRaises(d.fb.MetricError):
                client.get(PAGE, d.METRIC, d.SINCE, d.UNTIL)
        self.assertEqual(budget.attempts, 1)
        self.opener.open.assert_called_once()

    def test_success_cannot_restart_or_make_a_second_transport_request(self):
        execution = d.Execution()
        self.assertEqual(execution.run(authorized=True)['attempts'], 1)
        with self.assertRaisesRegex(d.DiagnosticError, 'execution_already_used'):
            execution.run(authorized=True)
        self.opener.open.assert_called_once()
        self.opener.open.reset_mock()
        client = d.fb.Client(TOKEN)
        budget = d.OneGetOpener(client.opener); client.opener = budget
        client.get(PAGE, d.METRIC, d.SINCE, d.UNTIL)
        with self.assertRaisesRegex(d.fb.MetricError, 'request_budget_exhausted'):
            client.get(PAGE, d.METRIC, d.SINCE, d.UNTIL)
        self.assertEqual(budget.attempts, 1)
        self.opener.open.assert_called_once()

    def test_redirects_never_followed_and_error_body_never_read(self):
        body = Mock()
        self.opener.open.side_effect = urllib.error.HTTPError(RAW, 302, RAW, {}, body)
        report = d.Execution().run(authorized=True)
        self.assertEqual(report['status'], 'redirect_blocked')
        self.opener.open.assert_called_once(); body.read.assert_not_called()
        self.assertIsNone(d.fb.NoRedirect().redirect_request(None, None, 302, RAW, {}, RAW))

    def test_malformed_paging_still_reports_types_without_contents(self):
        for paging, expected in ((None, 'null'), (RAW, 'string'), ([], 'array'), (False, 'boolean')):
            body = payload(); body['paging'] = paging
            status, metadata = d.summarize(body)
            self.assertEqual(status, 'malformed_response')
            self.assertEqual(metadata['paging_type'], expected)
            self.assertIsNone(metadata['next_present'])
            self.assertNotIn(RAW, json.dumps(metadata))

    def test_invalid_next_type_or_empty_string_is_not_valid_navigation(self):
        for value in (None, False, 0, [], {}, '', '  '):
            body = payload(); body['paging']['next'] = value
            status, metadata = d.summarize(body)
            self.assertEqual(status, 'malformed_response')
            self.assertTrue(metadata['next_present'])
            self.assertEqual(metadata['next_type'], d.kind(value))

    def test_absent_fields_empty_arrays_and_previous_presence_are_distinct(self):
        status, metadata = d.summarize({'data': []})
        self.assertEqual(status, 'structure_valid')
        self.assertFalse(metadata['paging_present']); self.assertEqual(metadata['paging_type'], 'absent')
        self.assertEqual(metadata['data_entries'], 0)
        self.assertIsNone(metadata['values_observations'])
        body = payload(); body['paging'] = {'previous': RAW}; body['data'][0]['values'] = []
        status, metadata = d.summarize(body)
        self.assertEqual(status, 'structure_valid')
        self.assertTrue(metadata['previous_present']); self.assertFalse(metadata['next_present'])
        self.assertEqual(metadata['values_observations'], 0)
        status, metadata = d.summarize({'paging': {}})
        self.assertEqual(status, 'malformed_response'); self.assertIsNone(metadata['data_entries'])

    def test_count_caps_do_not_misrepresent_truncated_structure_as_valid(self):
        status, metadata = d.summarize({'data': [{}] * (d.MAX_COUNT + 1)})
        self.assertEqual(status, 'malformed_response')
        self.assertEqual(metadata['data_entries'], d.MAX_COUNT); self.assertTrue(metadata['data_entries_capped'])
        body = payload(); body['data'][0]['values'] *= d.MAX_COUNT + 1
        status, metadata = d.summarize(body)
        self.assertEqual(status, 'malformed_response')
        self.assertEqual(metadata['values_observations'], d.MAX_COUNT)
        self.assertTrue(metadata['values_observations_capped'])

    def test_bad_json_duplicate_keys_and_size_are_sanitized(self):
        for raw in (b'{', b'{"data":[],"data":[]}', b'{"x":NaN}', b'x' * (d.fb.MAX_BODY + 1)):
            self.opener.open.return_value = response(raw=raw)
            report = d.Execution().run(authorized=True)
            self.assertEqual(report['status'], 'malformed_response')
            self.assertIsNone(report['structure'])

    def test_structure_valid_does_not_approve_numeric_or_timestamp_semantics(self):
        body = payload(); body['data'][0]['values'][0] = {'value': RAW, 'end_time': RAW}
        status, metadata = d.summarize(body)
        self.assertEqual(status, 'structure_valid_navigation_pending')
        self.assertNotIn(RAW, json.dumps(metadata))
        body['data'][0]['period'] = 'week'
        self.assertEqual(d.summarize(body)[0], 'malformed_response')

    def test_stdout_has_no_values_ids_timestamps_raw_links_or_argument_echo(self):
        for args in (['--authorize-one-get'], ['--token', TOKEN], ['--day', RAW], ['--authorize-one']):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch('builtins.open', side_effect=AssertionError('file write')):
                d.main(args)
            for secret in (TOKEN, PAGE, RAW, '87654321', '2026-11-13T', 'https://'):
                self.assertNotIn(secret, out.getvalue())

    def test_fixed_day_must_be_closed_before_credentials_are_read(self):
        with patch.object(d, 'datetime') as clock, patch.object(os.environ, 'get') as get:
            clock.now.return_value.date.return_value = datetime(2026, 11, 12).date()
            with self.assertRaisesRegex(d.DiagnosticError, 'day_not_closed'):
                d.Execution().run(authorized=True)
            get.assert_not_called()
        self.build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
