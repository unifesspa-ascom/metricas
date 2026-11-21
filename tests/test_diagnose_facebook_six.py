"""All HTTP responses are synthetic; real sockets and other collectors forbidden."""
import contextlib
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import diagnose_facebook_six as d

DAY = '2026-01-01'
TOKEN, PAGE = 'SYNTHETIC_SECRET', '123456789'
RAW = 'SECRET_BODY https://private.invalid/?access_token=SYNTHETIC_SECRET cursor PII'


def payload(metric, value=7654321):
    return {'data': [{'name': metric, 'period': 'day', 'values': [
        {'end_time': '2026-01-02T00:00:00-0300', 'value': value}]}]}


def response(body=None, raw=None):
    result = Mock(status=200)
    result.read.return_value = raw if raw is not None else json.dumps(body).encode()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class SixRequestDiagnostic(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for target in ('socket.socket.connect', 'socket.create_connection',
                       'requests.sessions.Session.request'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.dict(os.environ, {'FB_PAGE_ACCESS_TOKEN': TOKEN, 'FB_PAGE_ID': PAGE}))
        self.opener = Mock()
        self.build = self.stack.enter_context(patch.object(d.fb.urllib.request, 'build_opener', return_value=self.opener))
        self.opener.open.side_effect = lambda req, **kw: response(payload(
            urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)['metric'][0]))

    def execute(self):
        return d.Execution().run(DAY, authorized=True)

    def test_default_denies_before_credentials_disk_or_client(self):
        with patch.object(d.os.environ, 'get', side_effect=AssertionError('credentials read')), \
                patch('builtins.open', side_effect=AssertionError('disk access')):
            with self.assertRaisesRegex(d.DiagnosticError, 'explicit_authorization_required'):
                d.Execution().run(DAY)
        self.build.assert_not_called()

    def test_six_fixed_metrics_one_window_header_get_only(self):
        report = self.execute()
        self.assertEqual(report['attempts'], 6)
        self.assertEqual(len(report['results']), 6)
        self.build.assert_called_once()
        self.assertEqual(self.opener.open.call_count, 6)
        metrics = []
        for call in self.opener.open.call_args_list:
            req = call.args[0]; parts = urllib.parse.urlsplit(req.full_url)
            query = urllib.parse.parse_qs(parts.query)
            self.assertEqual((parts.scheme, parts.netloc, parts.path),
                             ('https', 'graph.facebook.com', '/v21.0/' + PAGE + '/insights'))
            metrics.append(query.pop('metric')[0])
            self.assertEqual(query, {'period': ['day'], 'since': [DAY], 'until': ['2026-01-02']})
            self.assertEqual(req.get_method(), 'GET')
            self.assertEqual(req.get_header('Authorization'), 'Bearer ' + TOKEN)
            self.assertNotIn(TOKEN, req.full_url)
            self.assertEqual(call.kwargs, {'timeout': 20})
        self.assertEqual(tuple(metrics), d.PLANNED_METRICS)

    def test_global_budget_all_failures_and_cannot_restart(self):
        self.opener.open.side_effect = RuntimeError(RAW)
        execution = d.Execution()
        report = execution.run(DAY, authorized=True)
        for _ in range(2):
            with self.assertRaisesRegex(d.DiagnosticError, 'execution_already_used'):
                execution.run(DAY, authorized=True)
        self.assertEqual(execution.attempts, 6)
        self.assertEqual(self.opener.open.call_count, 6)
        self.build.assert_called_once()
        self.assertEqual({r['status'] for r in report['results']}, {'transport_error'})
        self.assertNotIn(RAW, json.dumps(report))

    def test_http_errors_redirect_and_pending_pagination_never_followed(self):
        body = Mock()
        pending = payload(d.PLANNED_METRICS[2]); pending['paging'] = {'next': RAW}
        self.opener.open.side_effect = [
            urllib.error.HTTPError(RAW, 400, RAW, {}, body),
            urllib.error.HTTPError(RAW, 302, RAW, {}, body),
            response(pending), response({'error': {'message': RAW}}),
            response({}), response({'data': []}),
        ]
        report = self.execute()
        self.assertEqual([r['status'] for r in report['results']],
                         ['http_error', 'redirect_blocked', 'incomplete_response',
                          'api_error', 'invalid_structure', 'no_data'])
        self.assertEqual(self.opener.open.call_count, 6)
        body.read.assert_not_called()
        self.assertIsNone(d.fb.NoRedirect().redirect_request(None, None, 302, RAW, {}, RAW))

    def test_zero_valid_absence_invalid_values_and_pending_policy_are_distinct(self):
        bodies = [payload(d.PLANNED_METRICS[0], 0), {'data': []},
                  payload(d.PLANNED_METRICS[2], False), payload('wrong_metric'),
                  payload(d.PLANNED_METRICS[4], -1), payload(d.PLANNED_METRICS[5])]
        self.opener.open.side_effect = [response(b) for b in bodies]
        report = self.execute()
        self.assertEqual([r['status'] for r in report['results']],
                         ['valid_values_temporal_pending', 'no_data', 'invalid_values',
                          'invalid_structure', 'invalid_values', 'valid_values_temporal_pending'])
        for result in report['results']:
            self.assertEqual(result['temporal_policy'], 'pending')
            self.assertEqual(result['daily_coverage'], 'unverified')
            self.assertFalse(result['metric_approved'])

    def test_raw_json_rejects_duplicates_nonfinite_malformed_and_large_body(self):
        self.opener.open.side_effect = [response(raw=raw) for raw in (
            b'{"data":[],"data":[]}', b'{"x":NaN}', b'{"x":1e999}',
            b'{' + RAW.encode(), b'x' * (d.fb.MAX_BODY + 1), b'[]')]
        self.assertEqual([r['status'] for r in self.execute()['results']],
                         ['invalid_structure'] * 4 + ['response_too_large', 'invalid_structure'])

    def test_bad_today_future_and_multiday_inputs_fail_before_client(self):
        today = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
        for day in (today.isoformat(), (today + timedelta(days=1)).isoformat(),
                    '2026-02-30', DAY + '/' + '2026-01-03', RAW, None):
            with self.subTest(day=day), self.assertRaises(d.DiagnosticError):
                d.Execution().run(day, authorized=True)
        self.build.assert_not_called()

    def test_missing_credentials_have_no_defaults_or_requests(self):
        for env in ({}, {'FB_PAGE_ID': PAGE}, {'FB_PAGE_ACCESS_TOKEN': TOKEN},
                    {'FB_PAGE_ID': RAW, 'FB_PAGE_ACCESS_TOKEN': TOKEN}):
            with patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(d.DiagnosticError, 'invalid_credentials'):
                self.execute()
        self.build.assert_not_called()

    def test_cli_sanitizes_arguments_errors_values_and_writes_nothing(self):
        for args in ([], ['--day', DAY], ['--day', RAW, '--authorize-six-gets'],
                     ['--day', DAY, '--token', TOKEN], ['--day', DAY, '--authorize-six']):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch('builtins.open', side_effect=AssertionError('disk access')):
                self.assertEqual(d.main(args), 2)
            self.assertNotIn(RAW, out.getvalue()); self.assertNotIn(TOKEN, out.getvalue())
        self.build.assert_not_called()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('builtins.open', side_effect=AssertionError('disk access')):
            self.assertEqual(d.main(['--day', DAY, '--authorize-six-gets']), 0)
        for forbidden in (PAGE, TOKEN, '7654321', 'end_time', 'data_present', 'https://'):
            self.assertNotIn(forbidden, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['attempts'], 6)

    def test_unexpected_exception_text_is_never_emitted(self):
        with patch.object(d.fb, 'parse_observations', side_effect=ValueError(RAW)):
            report = self.execute()
        self.assertEqual({r['status'] for r in report['results']}, {'internal_error'})
        self.assertNotIn(RAW, json.dumps(report))

    def test_modified_plan_and_preflight_failure_cannot_restart_execution(self):
        execution = d.Execution()
        with patch.object(d, 'PLANNED_METRICS', (d.PLANNED_METRICS[0],) * 6):
            with self.assertRaisesRegex(d.DiagnosticError, 'plan_mismatch'):
                execution.run(DAY, authorized=True)
        with self.assertRaisesRegex(d.DiagnosticError, 'execution_already_used'):
            execution.run(DAY, authorized=True)
        self.build.assert_not_called()

    def test_unknown_error_categories_are_not_copied_into_report(self):
        client = Mock()
        client.get.side_effect = d.fb.MetricError(RAW)
        report = d.observation(client, PAGE, d.PLANNED_METRICS[0], DAY, '2026-01-02')
        self.assertEqual(report['status'], 'invalid_structure')
        self.assertNotIn(RAW, json.dumps(report))

    def test_six_http_failures_share_budget_and_do_not_restart(self):
        bodies = [Mock() for _ in range(6)]
        self.opener.open.side_effect = [
            urllib.error.HTTPError(RAW, code, RAW, {}, body)
            for code, body in zip((400, 401, 403, 429, 500, 503), bodies)]
        execution = d.Execution()
        report = execution.run(DAY, authorized=True)
        with self.assertRaisesRegex(d.DiagnosticError, 'execution_already_used'):
            execution.run(DAY, authorized=True)
        self.assertEqual(report['attempts'], 6)
        self.assertEqual(self.opener.open.call_count, 6)
        self.build.assert_called_once()
        self.assertEqual({item['status'] for item in report['results']}, {'http_error'})
        for body in bodies:
            body.read.assert_not_called()

    def test_http_200_does_not_certify_period_timestamps_or_values(self):
        bodies = [payload(metric) for metric in d.PLANNED_METRICS]
        bodies[0]['data'][0]['period'] = 'week'
        bodies[1]['data'][0]['values'][0]['end_time'] = '2026-01-02'
        bodies[2]['data'][0]['values'][0]['value'] = '0'
        bodies[3]['data'].append(dict(bodies[3]['data'][0]))
        bodies[4]['data'][0]['values'] = []
        self.opener.open.side_effect = [response(body) for body in bodies]
        report = self.execute()
        self.assertEqual([item['status'] for item in report['results']],
                         ['invalid_structure', 'invalid_structure', 'invalid_values',
                          'invalid_structure', 'no_data', 'valid_values_temporal_pending'])
        self.assertTrue(all(item['metric_approved'] is False for item in report['results']))

    def test_bytecode_writes_are_disabled_even_without_B_flag(self):
        source = Path(d.__file__).read_text(encoding='utf-8')
        with patch.object(sys, 'dont_write_bytecode', False):
            exec(compile(source, d.__file__, 'exec'), {'__name__': 'offline_import_fixture'})
            self.assertTrue(sys.dont_write_bytecode)
        self.build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
