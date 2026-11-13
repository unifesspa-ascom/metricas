"""Offline regressions adapted from the ten pinned-response characterization tests.

Load current account modules in memory; no GitHub/Meta access or collector import.
Navigation remains unreviewed and must never yield accepted observations.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
METRIC = 'page_media_view'
START, END = '2026-01-01', '2026-01-02'
SECRET = 'SYNTHETIC_SECRET_NOT_FOR_OUTPUT'


def payload(metric=METRIC):
    return {'data': [{'name': metric, 'period': 'day', 'values': [
        {'value': 0, 'end_time': END + 'T00:00:00-0300'}]}]}


class PagingDiagnosis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {name: (ROOT / 'scripts' / (name + '.py')).read_text(encoding='utf-8')
            for name in ('facebook_account_metrics', 'account_metrics_safety', 'diagnose_facebook_six')}

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.socket.sendto', 'socket.create_connection', 'socket.getaddrinfo',
                       'requests.sessions.Session.request'):
            stack.enter_context(patch(target, side_effect=AssertionError('offline: network forbidden')))
        modules = {name: ModuleType(name) for name in self.sources}
        stack.enter_context(patch.dict(sys.modules, modules))
        stack.enter_context(patch.object(sys, 'dont_write_bytecode', True))
        for name, source in self.sources.items():
            exec(compile(source, str(ROOT / 'scripts' / (name + '.py')), 'exec'), modules[name].__dict__)
        self.fb = modules['facebook_account_metrics']
        self.diagnostic = modules['diagnose_facebook_six']

    def assert_category(self, body, category):
        with self.assertRaisesRegex(self.fb.MetricError, '^' + category + '$'):
            self.fb.parse_observations(body, METRIC)

    def test_non_object_paging_is_classified_as_invalid(self):
        for paging in (None, False, 0, [], ['synthetic'], 'synthetic'):
            with self.subTest(type=type(paging).__name__):
                body = payload(); body['paging'] = paging
                self.assert_category(body, 'invalid_paging')

    def test_nonempty_string_next_is_pending_and_wrong_types_are_invalid(self):
        for following in ('synthetic-link', True, 1, ['synthetic'], {'synthetic': True}):
            with self.subTest(type=type(following).__name__):
                body = payload(); body['paging'] = {'next': following}
                self.assert_category(body, 'pagination_pending' if isinstance(following, str) else 'invalid_paging')

    def test_cursor_and_time_navigation_have_identical_classification(self):
        # Synthetic URLs only. Neither legitimacy nor reporting semantics is established.
        for query in ('since=2026-01-01&until=2026-01-02&after=synthetic',
                      'since=2026-01-02&until=2026-01-03'):
            body = payload()
            body['paging'] = {'next': 'https://example.invalid/synthetic?' + query}
            self.assert_category(body, 'pagination_pending')

    def test_paging_guard_masks_missing_data_wrong_metric_and_invalid_value(self):
        missing = {}
        wrong_metric = payload('synthetic_wrong_metric')
        invalid_value = payload(); invalid_value['data'][0]['values'][0]['value'] = False
        for body in (missing, wrong_metric, invalid_value, {'data': []}):
            body['paging'] = {'next': 'synthetic-link'}
            self.assert_category(body, 'pagination_pending')

    def test_no_paging_or_empty_object_preserves_legitimate_zero(self):
        for paging in ({}, {'previous': 'synthetic-link'}, {'cursors': {'after': 'synthetic'}}):
            body = payload(); body['paging'] = paging
            self.assertEqual(self.fb.parse_observations(body, METRIC)[0][1], 0)
        self.assertEqual(self.fb.parse_observations(payload(), METRIC)[0][1], 0)

    def test_falsey_malformed_next_is_rejected(self):
        for following in (None, '', '   ', False, 0, [], {}):
            body = payload(); body['paging'] = {'next': following}
            self.assert_category(body, 'invalid_paging')

    def test_other_structure_errors_are_distinct_without_paging(self):
        for body, category in (({}, 'invalid_data'), ({'data': {}}, 'invalid_data'),
                               (payload('synthetic_wrong_metric'), 'wrong_metric'),
                               ({'error': {}}, 'invalid_response')):
            with self.assertRaisesRegex(self.fb.MetricError, '^' + category + '$'):
                self.fb.parse_observations(body, METRIC)

    def test_since_until_do_not_enter_parser_or_explain_this_category_directly(self):
        self.assertEqual(tuple(__import__('inspect').signature(self.fb.parse_observations).parameters),
                         ('payload', 'metric'))
        with self.assertRaisesRegex(self.fb.MetricError, '^invalid_window$'):
            self.fb.window(END, START)
        self.fb.window(START, END)

    def test_six_http_200_responses_reproduce_report_without_followups(self):
        opener = Mock()
        def open_response(request, **kwargs):
            from urllib.parse import parse_qs, urlsplit
            metric = parse_qs(urlsplit(request.full_url).query)['metric'][0]
            body = payload(metric)
            body['paging'] = {'next': 'https://example.invalid/?access_token=' + SECRET}
            response = Mock(status=200)
            response.read.return_value = json.dumps(body).encode()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            return response
        opener.open.side_effect = open_response
        output = io.StringIO()
        with patch.dict('os.environ', {'FB_PAGE_ACCESS_TOKEN': SECRET, 'FB_PAGE_ID': '123'}), \
                patch.object(self.fb.urllib.request, 'build_opener', return_value=opener) as build, \
                contextlib.redirect_stdout(output):
            code = self.diagnostic.main(['--day', START, '--authorize-six-gets'])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(report['attempts'], 6)
        self.assertEqual(report['planned_metrics'], 6)
        self.assertFalse(report['publication_enabled'])
        self.assertEqual({row['status'] for row in report['results']}, {'pagination_pending'})
        self.assertTrue(all(row['metric_approved'] is False for row in report['results']))
        self.assertEqual(opener.open.call_count, 6)
        build.assert_called_once()
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn('https://', output.getvalue())

    def test_publication_and_date_policy_gates_still_hold(self):
        self.assertEqual(self.fb.MAX_REQUESTS, 6)
        self.assertIsNone(self.fb.APPROVED_DAY_ZONE)
        self.assertFalse(self.fb.PUBLICATION_ENABLED)
        with self.assertRaisesRegex(self.fb.MetricError, '^account_publication_suspended$'):
            self.fb.require_publication_disabled()


if __name__ == '__main__':
    unittest.main()
