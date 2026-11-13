"""Pure synthetic navigation and observation fixtures; every I/O path is denied."""
import contextlib
import io
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import facebook_navigation_validation as v

CONTEXT = {'page': '123456789', 'since': '2026-11-12', 'until': '2026-11-13'}
SECRET = 'SYNTHETIC_SECRET_NEVER_LOG'
BASE = 'https://graph.facebook.com/v21.0/123456789/insights'


def link(since='2026-11-12', until='2026-11-13', **extra):
    query = dict(metric='page_media_view', period='day', since=since, until=until)
    query.update(extra)
    return BASE + '?' + urlencode(query)


def observation(value=0, stamp='2026-11-13T00:00:00-0300'):
    return {'data': [{'name': v.METRIC, 'period': 'day', 'values': [{'value': value, 'end_time': stamp}]}]}


class PureFixtures(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack(); self.addCleanup(stack.close)
        for name in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.socket.sendto',
                     'socket.create_connection', 'socket.getaddrinfo', 'urllib.request.urlopen',
                     'subprocess.run', 'builtins.open', 'os.environ.get'):
            stack.enter_context(patch(name, side_effect=AssertionError('pure function attempted I/O')))

    def classify(self, url, **overrides):
        result = v.classify_link(url, **dict(CONTEXT, **overrides))
        self.assertFalse(result['navigation_approved'])
        self.assertEqual(result['coverage'], 'unverified')
        self.assertEqual(result['temporal_policy'], 'pending')
        self.assertEqual(result['interval_model'], 'candidate_half_open')
        return result

    def validate(self, body, **overrides):
        context = dict(since=CONTEXT['since'], until=CONTEXT['until']); context.update(overrides)
        result = v.validate_observation(body, **context)
        for field in ('metric_approved', 'publication_enabled', 'day_assigned'):
            self.assertFalse(result[field])
        self.assertEqual(result['temporal_policy'], 'pending')
        self.assertEqual(result['coverage'], 'unverified')
        return result

    def test_temporal_before_and_after_candidates(self):
        for since, until, relation in (('2026-11-11', '2026-11-12', 'before'),
                                       ('2026-11-13', '2026-11-14', 'after')):
            result = self.classify(link(since, until))
            self.assertEqual(result['classification'], 'temporal_navigation_candidate')
            self.assertEqual(result['window_relation'], relation)
            self.assertFalse(result['cursor_present'])

    def test_next_and_previous_never_determine_direction(self):
        paging = {'next': link('2026-11-11', '2026-11-12'),
                  'previous': link('2026-11-13', '2026-11-14')}
        before = deepcopy(paging)
        result = v.classify_navigation(paging, **CONTEXT)
        self.assertEqual(result['next']['window_relation'], 'before')
        self.assertEqual(result['previous']['window_relation'], 'after')
        self.assertEqual(paging, before)

    def test_cursor_markers_only_with_same_proven_window(self):
        for key in v.CURSORS:
            result = self.classify(link(**{key: SECRET}))
            self.assertEqual(result['classification'], 'cursor_pagination')
            self.assertEqual(result['window_relation'], 'same')
            self.assertTrue(result['cursor_present'])
            self.assertNotIn(SECRET, json.dumps(result))

    def test_mixed_time_and_cursor_is_unknown(self):
        result = self.classify(link('2026-11-13', '2026-11-14', after=SECRET))
        self.assertEqual(result['classification'], 'unknown_navigation')
        self.assertEqual(result['reason'], 'mixed_navigation')

    def test_conflicting_and_empty_cursors(self):
        for extra in ({'after': ''}, {'after': '  '}, {'before': SECRET, 'after': SECRET}):
            self.assertEqual(self.classify(link(**extra))['classification'], 'unknown_navigation')

    def test_same_window_without_cursor_is_unexplained(self):
        self.assertEqual(self.classify(link())['classification'], 'unknown_navigation')

    def test_overlap_and_changed_window_width_are_unknown(self):
        for url in (link('2026-11-12', '2026-11-14'), link('2026-11-13', '2026-11-15')):
            self.assertEqual(self.classify(url)['classification'], 'unknown_navigation')

    def test_protocol_host_credentials_fragment_and_endpoint_are_checked(self):
        good = link('2026-11-13', '2026-11-14')
        bad = (good.replace('https:', 'http:'), good.replace('graph.facebook.com', 'graph.facebook.com.evil.invalid'),
               good.replace('graph.facebook.com', 'user@graph.facebook.com'),
               good.replace('graph.facebook.com', 'graph.facebook.com:444'), good + '#',
               good.replace('/v21.0/', '/v22.0/'), good.replace('/123456789/', '/987654321/'),
               good.replace('/insights?', '/media?'), good.replace('/insights?', '/insights/?'),
               good.replace('https:', ''), ' ' + good)
        for url in bad:
            self.assertEqual(self.classify(url)['classification'], 'unknown_navigation')
        self.assertEqual(self.classify(good.replace('graph.facebook.com', 'graph.facebook.com:443'))['classification'],
                         'temporal_navigation_candidate')

    def test_metric_and_period_must_be_present_and_exact(self):
        for url in (link(metric='other'), link(period='week'),
                    link().replace('metric=page_media_view&', ''), link().replace('period=day&', '')):
            self.assertEqual(self.classify(url)['reason'], 'missing_or_changed_filter')

    def test_missing_window_is_not_restored_even_with_cursor(self):
        for field in ('since=2026-11-12&', '&until=2026-11-13'):
            result = self.classify(link(after=SECRET).replace(field, ''))
            self.assertEqual(result['classification'], 'unknown_navigation')
            self.assertEqual(result['reason'], 'missing_window')

    def test_unknown_parameters_duplicates_and_encoded_duplicate_keys(self):
        for url in (link(extra=SECRET), link(offset=1), link() + '&metric=page_media_view',
                    link() + '&%6Detric=page_media_view', link() + '&after=x&after=y'):
            self.assertEqual(self.classify(url)['classification'], 'unknown_navigation')

    def test_invalid_queries_and_resource_limits(self):
        for url in (link() + '&bad=%ZZ', link() + '&bad', link() + '&', link() + '&x=' + 'a' * v.MAX_LINK,
                    BASE + '?' + '&'.join('after=x' for _ in range(17)), None, {}, ''):
            self.assertEqual(self.classify(url)['classification'], 'unknown_navigation')

    def test_optional_limits_are_bounded(self):
        for limit in ('0', '-1', '1001', '1.0', '01', SECRET):
            self.assertEqual(self.classify(link(limit=limit))['reason'], 'invalid_limit')
        self.assertEqual(self.classify(link(after=SECRET, limit=1000))['classification'], 'cursor_pagination')

    def test_direction_flags_alone_do_not_prove_temporal_navigation(self):
        self.assertEqual(self.classify(link('2026-11-13', '2026-11-14', __previous=1))['classification'], 'unknown_navigation')
        self.assertEqual(self.classify(link(after=SECRET, __previous=1))['classification'], 'cursor_pagination')
        self.assertEqual(self.classify(link(after=SECRET, __previous=SECRET))['classification'], 'unknown_navigation')

    def test_sensitive_query_credentials_require_exact_request_context(self):
        url = link('2026-11-13', '2026-11-14', access_token=SECRET)
        self.assertEqual(self.classify(url)['reason'], 'credentials_unverified')
        self.assertEqual(self.classify(url, expected_token='OTHER_SYNTHETIC')['reason'], 'credential_mismatch')
        result = self.classify(url, expected_token=SECRET)
        self.assertEqual(result['classification'], 'temporal_navigation_candidate')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_date_only_vs_unix_seconds_never_guesses_a_timezone(self):
        result = self.classify(link('1794528000', '1794614400'))
        self.assertEqual(result['reason'], 'window_basis_unverified')
        self.assertEqual(result['classification'], 'unknown_navigation')

    def test_explicit_instants_and_epoch_have_proven_equivalence(self):
        start = '2026-11-12T00:00:00Z'; end = '2026-11-13T00:00:00Z'
        epoch_start = str(int(datetime(2026, 11, 12, tzinfo=timezone.utc).timestamp()))
        epoch_end = str(int(datetime(2026, 11, 13, tzinfo=timezone.utc).timestamp()))
        result = self.classify(link(epoch_start, epoch_end, after=SECRET), since=start, until=end)
        self.assertEqual(result['classification'], 'cursor_pagination')
        result = self.classify(link('2026-11-11T21:00:00-0300', '2026-11-12T21:00:00-0300', after=SECRET),
                               since=start, until=end)
        self.assertEqual(result['classification'], 'cursor_pagination')

    def test_invalid_context_or_calendar_boundaries_remain_unknown(self):
        for overrides in ({'page': SECRET}, {'since': '2026-02-30'}, {'since': CONTEXT['until']},
                          {'until': '2027-11-13'}, {'since': None}):
            self.assertEqual(self.classify(link(), **overrides)['classification'], 'unknown_navigation')
        self.assertEqual(self.classify(link('2026-02-30', '2026-03-01'))['classification'], 'unknown_navigation')

    def test_absent_links_and_malformed_paging_do_not_get_classified_as_temporal(self):
        for paging in ({}, None, [], SECRET, {'next': None}):
            result = v.classify_navigation(paging, **CONTEXT)
            self.assertTrue(all(x['classification'] == 'unknown_navigation' for x in result.values()))

    def test_valid_zero_and_positive_do_not_assign_a_calendar_day(self):
        for value in (0, 1, 9007199254740991):
            result = self.validate(observation(value))
            self.assertEqual(result['status'], 'valid_observation')
            self.assertEqual(result['observation_count'], 1)
            self.assertEqual(result['window_compatibility'], 'unverified_timezone')

    def test_bool_float_string_negative_and_large_integer_rejected(self):
        for value in (True, False, -1, 0.0, 1.5, '0', None, {}, [], 9007199254740992):
            self.assertEqual(self.validate(observation(value))['reason'], 'invalid_value')

    def test_missing_value_and_timestamp_are_not_filled(self):
        for field in ('value', 'end_time'):
            body = observation(); del body['data'][0]['values'][0][field]
            self.assertEqual(self.validate(body)['status'], 'invalid_observation')

    def test_invalid_or_naive_timestamps_rejected(self):
        for stamp in ('2026-11-13', '2026-11-13T00:00:00', '2026-02-30T00:00:00Z', SECRET, None):
            self.assertEqual(self.validate(observation(stamp=stamp))['reason'], 'invalid_end_time')

    def test_duplicate_instants_and_multiple_observations_rejected_atomically(self):
        body = observation(); body['data'][0]['values'].append({'value': 0, 'end_time': '2026-11-13T03:00:00Z'})
        self.assertEqual(self.validate(body)['reason'], 'duplicate_end_time')
        body['data'][0]['values'][1]['end_time'] = '2026-11-14T00:00:00-0300'
        self.assertEqual(self.validate(body)['reason'], 'unexpected_observation_count')

    def test_missing_observations_are_absent_not_zero(self):
        for body in ({'data': []}, {'data': [{'name': v.METRIC, 'period': 'day', 'values': []}]}):
            self.assertEqual(self.validate(body)['status'], 'missing_observation')

    def test_exact_name_period_envelope_and_single_metric(self):
        for field, value, reason in (('name', SECRET, 'wrong_metric'), ('period', 'week', 'wrong_period')):
            body = observation(); body['data'][0][field] = value
            self.assertEqual(self.validate(body)['reason'], reason)
        body = observation(); body['data'].append(deepcopy(body['data'][0]))
        self.assertEqual(self.validate(body)['reason'], 'ambiguous_metric')
        for body in ({}, {'data': None}, {'error': {'message': SECRET}}, []):
            self.assertEqual(self.validate(body)['status'], 'invalid_observation')

    def test_invalid_request_window_does_not_validate_observation(self):
        self.assertEqual(self.validate(observation(), until=CONTEXT['since'])['reason'], 'invalid_window')

    def test_explicit_bounds_only_report_candidate_compatibility(self):
        context = {'since': '2026-11-12T03:00:00Z', 'until': '2026-11-13T03:00:00Z'}
        self.assertEqual(self.validate(observation(), **context)['window_compatibility'], 'candidate_end_matches_until')
        self.assertEqual(self.validate(observation(stamp='2026-11-12T12:00:00Z'), **context)['window_compatibility'], 'candidate_end_inside_window')
        for stamp in ('2026-11-12T03:00:00Z', '2026-11-14T03:00:00Z'):
            self.assertEqual(self.validate(observation(stamp=stamp), **context)['window_compatibility'], 'outside_window')

    def test_navigation_never_approves_observation_or_is_followed_by_validator(self):
        body = observation(); body['paging'] = {'next': SECRET}
        result = self.validate(body)
        self.assertEqual(result['status'], 'valid_observation')
        self.assertEqual(result['window_compatibility'], 'unverified_timezone')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(v.classify_navigation(body['paging'], **CONTEXT)['next']['classification'], 'unknown_navigation')

    def test_bound_on_observation_count(self):
        body = observation(); body['data'][0]['values'] *= v.MAX_OBSERVATIONS + 1
        self.assertEqual(self.validate(body)['reason'], 'observation_limit_exceeded')

    def test_outputs_never_include_sensitive_inputs_and_inputs_are_unchanged(self):
        body = observation(987654321); body['paging'] = {'next': SECRET}
        snapshot = deepcopy(body)
        result = self.validate(body)
        result.update(v.classify_navigation({'next': link(after=SECRET)}, **CONTEXT))
        text = json.dumps(result)
        for value in (SECRET, BASE, CONTEXT['page'], '987654321', '2026-11-13T', 'end_time', 'access_token'):
            self.assertNotIn(value, text)
        self.assertEqual(body, snapshot)

    def test_invalid_offset_minutes_never_normalized_into_valid_instants(self):
        for offset in ('+00:60', '+00:99', '-0399', '+24:00', '-00:00', '-0000'):
            stamp = '2026-11-13T00:00:00' + offset
            self.assertEqual(self.validate(observation(stamp=stamp))['reason'], 'invalid_end_time')
            start = '2026-11-12T00:00:00' + offset
            end = '2026-11-13T00:00:00' + offset
            self.assertEqual(self.classify(link(start, end), since=start, until=end)['classification'],
                             'unknown_navigation')

    def test_controls_and_non_ascii_in_raw_or_decoded_urls_are_rejected(self):
        for value in ('x\x00', 'x\n', 'x\x7f', 'x y', '\ud800', '\u00e9'):
            if value != '\ud800':
                self.assertEqual(self.classify(link(after=value))['classification'], 'unknown_navigation')
            self.assertEqual(self.classify(link(after='x') + value)['classification'], 'unknown_navigation')
        for raw in ('%00', '%0a', '%7f', '%FF', '%ED%A0%80'):
            self.assertEqual(self.classify(link(after='x') + raw)['classification'], 'unknown_navigation')

    def test_control_characters_in_matching_credentials_are_not_accepted(self):
        for token in (SECRET + '\x00', SECRET + '\x7f', SECRET + '\n', '\u00e9'):
            result = self.classify(link('2026-11-13', '2026-11-14', access_token=token), expected_token=token)
            self.assertEqual(result['classification'], 'unknown_navigation')

    def test_temporal_limit_without_comparable_request_limit_is_unknown(self):
        for limit in (1, 10, 1000):
            result = self.classify(link('2026-11-13', '2026-11-14', limit=limit))
            self.assertEqual(result['classification'], 'unknown_navigation')
            self.assertEqual(result['reason'], 'unreviewed_temporal_limit')

    def test_noncanonical_authorities_and_redirect_parameters_are_rejected(self):
        url = link('2026-11-13', '2026-11-14')
        for authority in ('graph.facebook.com:', 'graph.facebook.com:0443', 'graph.facebook.com.',
                          'graph.facebook.com@evil.invalid', 'evil.invalid@graph.facebook.com',
                          'graph.facebook.com:65536', '[::1]', 'graph.facebook.com%2eevil.invalid'):
            self.assertEqual(self.classify(url.replace('graph.facebook.com', authority))['classification'], 'unknown_navigation')
        for field in ('redirect', 'redirect_uri', 'url', 'next'):
            self.assertEqual(self.classify(link('2026-11-13', '2026-11-14', **{field: SECRET}))['classification'],
                             'unknown_navigation')

    def test_calendar_extremes_epoch_extremes_and_overflow_are_sanitized(self):
        for since, until in (('999999999999', '9999999999999'), ('0', '999999999999'),
                             ('NaN', 'Infinity'), ('2026-11-12', '2026-11-11'),
                             ('0001-01-01T00:00:00+23:59', '0001-01-02T00:00:00+23:59'),
                             ('9999-12-30T00:00:00-23:59', '9999-12-31T23:59:59-23:59'),
                             ('0' * 10000, '1' * 10000)):
            self.assertEqual(self.classify(link(), since=since, until=until)['classification'], 'unknown_navigation')
            self.assertEqual(self.validate(observation(), since=since, until=until)['reason'], 'invalid_window')

    def test_fractional_timestamps_are_valid_but_never_assign_a_day(self):
        result = self.validate(observation(stamp='2026-11-13T00:00:00.123456-0300'))
        self.assertEqual(result['status'], 'valid_observation')
        self.assertEqual(result['window_compatibility'], 'unverified_timezone')
        body = observation(stamp='2026-11-13T00:00:00.123456-0300')
        body['data'][0]['values'].append({'value': 0, 'end_time': '2026-11-13T03:00:00.123456Z'})
        self.assertEqual(self.validate(body)['reason'], 'duplicate_end_time')

    def test_half_open_adjacency_and_exclusive_end_are_only_candidates(self):
        start = '2026-11-12T00:00:00Z'; end = '2026-11-13T00:00:00Z'
        result = self.classify(link(end, '2026-11-14T00:00:00Z'), since=start, until=end)
        self.assertEqual(result['classification'], 'temporal_navigation_candidate')
        for stamp, relation in ((start, 'outside_window'), (end, 'candidate_end_matches_until')):
            result = self.validate(observation(stamp=stamp), since=start, until=end)
            self.assertEqual(result['window_compatibility'], relation)
            self.assertEqual(result['interval_model'], 'candidate_exclusive_end')

    def test_offset_changes_and_leap_days_compare_instants_without_iana_assumptions(self):
        start = '2026-11-01T00:00:00-0700'; end = '2026-11-02T00:00:00-0800'
        result = self.classify(link('2026-11-01T07:00:00Z', '2026-11-02T08:00:00Z', after=SECRET), since=start, until=end)
        self.assertEqual(result['classification'], 'cursor_pagination')
        result = self.classify(link('2024-02-29', '2024-03-01'), since='2024-02-28', until='2024-02-29')
        self.assertEqual(result['classification'], 'temporal_navigation_candidate')

    def test_unexpected_structures_and_numeric_extremes_never_escape(self):
        for value in (float('nan'), float('inf'), float('-inf'), 10 ** 1000):
            self.assertEqual(self.validate(observation(value))['reason'], 'invalid_value')
        for data in ([None], [[]], [{'name': v.METRIC, 'period': 'day', 'values': [None]}],
                     [{'name': v.METRIC, 'period': 'day', 'values': {}}]):
            self.assertEqual(self.validate({'data': data})['status'], 'invalid_observation')

    def test_no_logs_or_exception_text_escape_public_functions(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch('logging.Logger._log', side_effect=AssertionError('unexpected log')):
            self.classify(link('2026-11-13', '2026-11-14', access_token=SECRET), expected_token=SECRET)
            self.validate(observation(SECRET, SECRET))
            with patch.object(v, '_boundary', side_effect=ValueError(SECRET)):
                result = self.classify(link())
                self.assertNotIn(SECRET, json.dumps(result))
            with patch.object(v, '_aware_stamp', side_effect=ValueError(SECRET)):
                result = self.validate(observation())
                self.assertNotIn(SECRET, json.dumps(result))
            with patch.object(v, '_observations', side_effect=v._ObservationError(SECRET)):
                self.assertEqual(self.validate(observation())['reason'], 'invalid_structure')
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(err.getvalue(), '')

    def test_parent_cursor_metadata_never_allows_temporal_only_classification(self):
        for cursors in ({'after': SECRET}, {}, None, SECRET):
            paging = {'next': link('2026-11-13', '2026-11-14'), 'cursors': cursors}
            result = v.classify_navigation(paging, **CONTEXT)
            self.assertTrue(all(row['classification'] == 'unknown_navigation' for row in result.values()))
            self.assertTrue(all(row['navigation_approved'] is False for row in result.values()))
            self.assertNotIn(SECRET, json.dumps(result))

    def test_unknown_parent_metadata_and_oversized_credential_context_are_conservative(self):
        result = v.classify_navigation({'next': link('2026-11-13', '2026-11-14'), 'synthetic': SECRET}, **CONTEXT)
        self.assertTrue(all(row['classification'] == 'unknown_navigation' for row in result.values()))
        result = self.classify(link(access_token=SECRET), expected_token='x' * (v.MAX_LINK + 1))
        self.assertEqual(result['reason'], 'credentials_unverified')


if __name__ == '__main__':
    unittest.main()
