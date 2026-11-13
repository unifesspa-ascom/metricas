"""Synthetic end-to-end HTTP fixtures for the integrated one-GET diagnostic."""
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import diagnose_facebook_structure as d
import test_diagnose_facebook_structure as fixture

CURSOR = 'SYNTHETIC_CURSOR_NEVER_LOG'


def link(since=d.SINCE, until=d.UNTIL, **extra):
    query = dict(metric='page_media_view', period='day', since=since, until=until,
                 access_token=fixture.TOKEN)
    query.update(extra)
    return 'https://graph.facebook.com/v21.0/' + fixture.PAGE + '/insights?' + urlencode(query)


class IntegratedDiagnostic(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack(); self.addCleanup(stack.close)
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.socket.sendto',
                       'socket.create_connection', 'socket.getaddrinfo', 'requests.sessions.Session.request'):
            stack.enter_context(patch(target, side_effect=AssertionError('network forbidden')))
        stack.enter_context(patch.dict(os.environ, {'FB_PAGE_ACCESS_TOKEN': fixture.TOKEN, 'FB_PAGE_ID': fixture.PAGE}))
        self.opener = Mock()
        self.build = stack.enter_context(patch.object(d.fb.urllib.request, 'build_opener', return_value=self.opener))

    def run_body(self, body):
        before = deepcopy(body)
        self.opener.open.reset_mock()
        self.opener.open.return_value = fixture.response(body)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('builtins.open', side_effect=AssertionError('disk access')):
            code = d.main(['--authorize-one-get'])
        report = json.loads(output.getvalue())
        self.assertEqual(body, before)
        self.assertEqual(report['attempts'], 1)
        self.opener.open.assert_called_once()
        self.assert_controls(report)
        for secret in (fixture.TOKEN, fixture.PAGE, fixture.RAW, CURSOR, '87654321', '2026-11-13T', 'https://'):
            self.assertNotIn(secret, output.getvalue())
        return code, report

    def assert_controls(self, report):
        for key in ('navigation_approved', 'metric_approved', 'publication_enabled', 'day_assigned'):
            self.assertIs(report[key], False)
        self.assertEqual(report['coverage'], 'unverified')
        self.assertEqual(report['temporal_policy'], 'pending')

    def test_temporal_candidates_are_separate_and_never_assign_day(self):
        body = fixture.payload()
        body['paging'] = {'next': link('2026-11-11', '2026-11-12'),
                          'previous': link('2026-11-13', '2026-11-14')}
        code, report = self.run_body(body)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'structure_valid_navigation_pending')
        self.assertEqual(report['navigation']['next']['window_relation'], 'before')
        self.assertEqual(report['navigation']['previous']['window_relation'], 'after')
        for row in report['navigation'].values():
            self.assertEqual(row['classification'], 'temporal_navigation_candidate')
            self.assertFalse(row['navigation_approved'])
        self.assertEqual(report['observation']['status'], 'valid_observation')
        self.assertEqual(report['observation']['window_compatibility'], 'unverified_timezone')
        self.assertFalse(report['observation']['day_assigned'])

    def test_cursor_and_mixed_links_never_become_temporal_candidates(self):
        body = fixture.payload()
        body['paging'] = {'next': link(after=CURSOR),
                          'previous': link('2026-11-11', '2026-11-12', before=CURSOR)}
        code, report = self.run_body(body)
        self.assertEqual(code, 0)
        self.assertEqual(report['navigation']['next']['classification'], 'cursor_pagination')
        self.assertEqual(report['navigation']['previous']['classification'], 'unknown_navigation')
        self.assertEqual(report['navigation']['previous']['reason'], 'mixed_navigation')

    def test_date_epoch_mismatch_stays_unknown(self):
        body = fixture.payload()
        body['paging'] = {'next': link('1794528000', '1794614400')}
        _, report = self.run_body(body)
        self.assertEqual(report['navigation']['next']['reason'], 'window_basis_unverified')
        self.assertEqual(report['observation']['window_compatibility'], 'unverified_timezone')

    def test_unknown_and_conflicting_link_parameters_remain_unknown(self):
        body = fixture.payload()
        body['paging'] = {'next': link(extra=CURSOR), 'previous': link() + '&period=day'}
        _, report = self.run_body(body)
        self.assertEqual(report['navigation']['next']['reason'], 'unknown_parameter')
        self.assertEqual(report['navigation']['previous']['reason'], 'duplicate_parameter')

    def test_parent_cursor_metadata_is_not_ignored(self):
        body = fixture.payload()
        body['paging'] = {'next': link('2026-11-13', '2026-11-14'), 'cursors': {'after': fixture.RAW}}
        _, report = self.run_body(body)
        self.assertEqual(report['navigation']['next']['classification'], 'unknown_navigation')
        self.assertEqual(report['navigation']['next']['reason'], 'parent_cursor_metadata_unreviewed')

    def test_previous_only_is_pending_without_assuming_past_direction(self):
        body = fixture.payload(); body['paging'] = {'previous': link('2026-11-13', '2026-11-14')}
        _, report = self.run_body(body)
        self.assertEqual(report['status'], 'structure_valid_navigation_pending')
        self.assertEqual(report['navigation']['previous']['window_relation'], 'after')
        self.assertEqual(report['navigation']['next']['reason'], 'link_absent')

    def test_no_links_does_not_imply_coverage_or_day_approval(self):
        body = fixture.payload(); del body['paging']
        code, report = self.run_body(body)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'structure_valid')
        self.assertEqual(report['observation']['observation_count'], 1)

    def test_malformed_paging_does_not_skip_independent_value_validation(self):
        body = fixture.payload(); body['paging'] = None
        code, report = self.run_body(body)
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'malformed_response')
        self.assertEqual(report['navigation']['next']['reason'], 'invalid_paging')
        self.assertEqual(report['observation']['status'], 'valid_observation')

    def test_malformed_structure_maintains_sanitized_shape_and_reasons(self):
        code, report = self.run_body({'data': {}, 'paging': {'next': fixture.RAW}})
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'malformed_response')
        self.assertEqual(report['observation']['status'], 'invalid_observation')
        self.assertIsNone(report['structure']['data_entries'])

    def test_invalid_integer_and_offset_are_not_successful_diagnostics(self):
        for item, reason in (({'value': True, 'end_time': '2026-11-13T00:00:00Z'}, 'invalid_value'),
                             ({'value': 0, 'end_time': '2026-11-13T00:00:00+00:99'}, 'invalid_end_time')):
            body = fixture.payload(); body['data'][0]['values'] = [item]
            code, report = self.run_body(body)
            self.assertEqual(code, 1)
            self.assertEqual(report['status'], 'invalid_observation')
            self.assertEqual(report['observation']['reason'], reason)

    def test_missing_and_multiple_observations_never_become_zero_or_daily_series(self):
        for values, status, reason in (([], 'missing_observation', 'no_observation'),
                ([{'value': 0, 'end_time': '2026-11-13T00:00:00Z'},
                  {'value': 0, 'end_time': '2026-11-14T00:00:00Z'}], 'invalid_observation', 'unexpected_observation_count')):
            body = fixture.payload(); body['data'][0]['values'] = values
            code, report = self.run_body(body)
            self.assertEqual(code, 1)
            self.assertEqual(report['status'], status)
            self.assertEqual(report['observation']['reason'], reason)

    def test_authorization_failure_never_calls_validators_or_network(self):
        with patch.object(d, 'classify_navigation') as classify, patch.object(d, 'validate_observation') as validate:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(d.main([]), 1)
            self.assert_controls(json.loads(out.getvalue()))
            classify.assert_not_called(); validate.assert_not_called(); self.build.assert_not_called()

    def test_transport_http_and_redirect_failures_do_not_validate_or_restart(self):
        for error, status in ((RuntimeError(fixture.RAW), 'transport_error'),
                (urllib.error.HTTPError(fixture.RAW, 400, fixture.RAW, {}, Mock()), 'http_error'),
                (urllib.error.HTTPError(fixture.RAW, 302, fixture.RAW, {}, Mock()), 'redirect_blocked')):
            self.opener.open.reset_mock(); self.opener.open.side_effect = error
            execution = d.Execution()
            with patch.object(d, 'classify_navigation') as classify, patch.object(d, 'validate_observation') as validate:
                report = execution.run(authorized=True)
                with self.assertRaisesRegex(d.DiagnosticError, 'execution_already_used'):
                    execution.run(authorized=True)
                classify.assert_not_called(); validate.assert_not_called()
            self.assertEqual(report['status'], status)
            self.assertEqual(report['attempts'], 1)
            self.assert_controls(report)
            self.assertIsNone(report['navigation']); self.assertIsNone(report['observation'])
            self.opener.open.assert_called_once()

    def test_malformed_json_precedes_both_validators(self):
        self.opener.open.return_value = fixture.response(raw=b'{"data":[],"data":[]}')
        with patch.object(d, 'classify_navigation') as classify, patch.object(d, 'validate_observation') as validate:
            report = d.Execution().run(authorized=True)
            classify.assert_not_called(); validate.assert_not_called()
        self.assertEqual(report['status'], 'malformed_response')
        self.assertEqual(report['attempts'], 1)

    def test_unexpected_validator_failure_is_atomic_and_sanitized(self):
        self.opener.open.return_value = fixture.response(fixture.payload())
        with patch.object(d, 'validate_observation', side_effect=RuntimeError(fixture.RAW)):
            report = d.Execution().run(authorized=True)
        self.assertEqual(report['status'], 'internal_error')
        for key in ('structure', 'navigation', 'observation'):
            self.assertIsNone(report[key])
        self.assertNotIn(fixture.RAW, json.dumps(report))
        self.assertEqual(report['attempts'], 1)
        self.assert_controls(report)


if __name__ == '__main__':
    unittest.main()
