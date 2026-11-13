"""Pure, diagnostic-only Facebook navigation and observation checks.

No I/O, transport, environment access, link following, persistence or day mapping.
Outputs are fixed categories/booleans/counts, never input links or observations.
Candidates are NOT approval of navigation semantics, coverage or reporting time.
"""
from datetime import date, datetime, timedelta, timezone
import re
from urllib.parse import parse_qsl, urlsplit

METRIC = 'page_media_view'
CURSORS = frozenset({'after', 'before', '__paging_token', '__after', '__before'})
ALLOWED = CURSORS | {'metric', 'period', 'since', 'until', 'access_token', 'limit', '__previous'}
MAX_LINK = 8192
MAX_OBSERVATIONS = 1000
STAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:?[0-9]{2})')


def _printable(value):
    return isinstance(value, str) and len(value) <= MAX_LINK and all(33 <= ord(c) <= 126 for c in value)


def _aware_stamp(value):
    if not isinstance(value, str) or len(value) > 32 or not STAMP.fullmatch(value):
        raise ValueError
    if not value.endswith('Z'):
        offset = value[-6:] if value[-3] == ':' else value[-5:]
        digits = offset[1:].replace(':', '')
        # fromisoformat normalizes invalid offset minutes; reject before parsing.
        # RFC3339 -00:00 denotes an unknown offset, not proven UTC.
        if int(digits[:2]) > 23 or int(digits[2:]) > 59 or offset in ('-00:00', '-0000'):
            raise ValueError
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def _boundary(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        return 'date', date.fromisoformat(value)
    if STAMP.fullmatch(value):
        return 'instant', _aware_stamp(value)
    if re.fullmatch(r'(?:0|[1-9][0-9]{0,11})', value):
        return 'instant', datetime.fromtimestamp(int(value), timezone.utc)
    raise ValueError


def _window(since, until):
    kind, start = _boundary(since)
    end_kind, end = _boundary(until)
    if kind != end_kind or not timedelta(0) < end - start <= timedelta(days=31):
        raise ValueError
    return kind, start, end


def _navigation(reason, classification='unknown_navigation', relation='unverified', cursor=None):
    return {'classification': classification, 'reason': reason,
            'window_relation': relation, 'cursor_present': cursor,
            'navigation_approved': False, 'coverage': 'unverified',
            'temporal_policy': 'pending', 'interval_model': 'candidate_half_open'}


def classify_link(link, *, page, since, until, expected_token=None):
    """Inspect a single string; no direction is inferred from its next/previous label.

    Missing filters are not restored. Credentials in links must match a supplied
    request token, otherwise they remain unverified. No sensitive argument escapes.
    Only disjoint, equal-width, cursor-free windows are temporal candidates.
    Relations use a CANDIDATE [since, until) model, not proven Meta exclusivity.
    """
    try:
        if not isinstance(page, str) or not re.fullmatch(r'[0-9]{1,32}', page):
            return _navigation('invalid_context')
        original_kind, original_start, original_end = _window(since, until)
        if (not isinstance(link, str) or not link or len(link) > MAX_LINK
                or not _printable(link)):
            return _navigation('invalid_link')
        parts = urlsplit(link)
        if (parts.scheme != 'https' or parts.hostname != 'graph.facebook.com'
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or '#' in link
                or parts.netloc.lower() not in ('graph.facebook.com', 'graph.facebook.com:443')
                or parts.path != '/v21.0/' + page + '/insights'):
            return _navigation('unexpected_endpoint')
        if re.search(r'%(?![0-9a-fA-F]{2})', parts.query):
            return _navigation('invalid_query')
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=16, errors='strict')
        if len({key for key, _ in pairs}) != len(pairs):
            return _navigation('duplicate_parameter')
        if any(not _printable(key) or not _printable(value) for key, value in pairs):
            return _navigation('invalid_query')
        query = dict(pairs)
        if query.keys() - ALLOWED:
            return _navigation('unknown_parameter')
        if 'access_token' in query:
            if not _printable(expected_token) or not expected_token:
                return _navigation('credentials_unverified')
            if query['access_token'] != expected_token:
                return _navigation('credential_mismatch')
        if query.get('metric') != METRIC or query.get('period') != 'day':
            return _navigation('missing_or_changed_filter')
        if 'limit' in query and (not re.fullmatch(r'[1-9][0-9]{0,3}', query['limit'])
                                 or int(query['limit']) > MAX_OBSERVATIONS):
            return _navigation('invalid_limit')
        markers = query.keys() & CURSORS
        cursor = bool(markers)
        if len(markers) > 1 or any(not query[key].strip() for key in markers):
            return _navigation('conflicting_or_empty_cursor', cursor=cursor)
        if '__previous' in query and (query['__previous'] not in ('0', '1') or not cursor):
            return _navigation('unreviewed_direction_marker', cursor=cursor)
        if 'limit' in query and not cursor:
            return _navigation('unreviewed_temporal_limit', cursor=False)
        if 'since' not in query or 'until' not in query:
            return _navigation('missing_window', cursor=cursor)
        link_kind, start, end = _window(query['since'], query['until'])
        if link_kind != original_kind:
            return _navigation('window_basis_unverified', cursor=cursor)
        relation = ('same' if (start, end) == (original_start, original_end) else
                    'before' if end <= original_start else
                    'after' if start >= original_end else 'overlap')
        if cursor:
            if relation == 'same':
                return _navigation('same_window_cursor', 'cursor_pagination', relation, True)
            return _navigation('mixed_navigation', relation=relation, cursor=True)
        if relation in ('before', 'after') and end - start == original_end - original_start:
            return _navigation('disjoint_equal_width_window', 'temporal_navigation_candidate', relation, False)
        return _navigation('window_not_pure_temporal_navigation', relation=relation, cursor=False)
    except (ValueError, TypeError, OverflowError, OSError, UnicodeError):
        return _navigation('invalid_context_or_query')


def classify_navigation(paging, *, page, since, until, expected_token=None):
    """Independent labels; neither next nor previous implies a time direction."""
    if not isinstance(paging, dict):
        return {key: _navigation('invalid_paging') for key in ('next', 'previous')}
    if 'cursors' in paging:
        # Cursor metadata outside the URLs can contradict a temporal-only guess.
        # Its relationship to either link has not been established.
        cursor = True if isinstance(paging['cursors'], dict) and paging['cursors'] else None
        return {key: _navigation('parent_cursor_metadata_unreviewed', cursor=cursor)
                for key in ('next', 'previous')}
    if paging.keys() - {'next', 'previous'}:
        return {key: _navigation('unknown_paging_metadata') for key in ('next', 'previous')}
    return {key: (classify_link(paging[key], page=page, since=since, until=until,
                               expected_token=expected_token)
                  if key in paging else _navigation('link_absent')) for key in ('next', 'previous')}


class _ObservationError(ValueError):
    pass


def _observations(data):
    """Bounded, atomic validation. Retain only instants internally, never values."""
    def require(condition, reason):
        if not condition:
            raise _ObservationError(reason)
    require(isinstance(data, list), 'invalid_data')
    if not data:
        return []
    require(len(data) == 1, 'ambiguous_metric')
    entry = data[0]
    require(isinstance(entry, dict), 'invalid_observation')
    require(entry.get('name') == METRIC, 'wrong_metric')
    require(entry.get('period') == 'day', 'wrong_period')
    values = entry.get('values')
    require(isinstance(values, list), 'invalid_values')
    require(len(values) <= MAX_OBSERVATIONS, 'observation_limit_exceeded')
    instants, seen = [], set()
    for item in values:
        require(isinstance(item, dict), 'invalid_observation')
        value = item.get('value')
        require(type(value) is int and 0 <= value <= 9007199254740991, 'invalid_value')
        try:
            stamp = _aware_stamp(item.get('end_time'))
        except (ValueError, TypeError, OverflowError):
            raise _ObservationError('invalid_end_time') from None
        require(stamp not in seen, 'duplicate_end_time')
        seen.add(stamp)
        instants.append(stamp)
    return instants


def validate_observation(payload, *, since, until):
    """Validate decoded observations independently of navigation, without accepting a day.

    Absolute-bound comparisons are only candidate exclusive-end checks, never
    an audited reporting policy. Date-only requests cannot map aware timestamps
    to calendar days without an approved timezone. JSON decoding is upstream.
    """
    report = {'status': 'invalid_observation', 'reason': 'invalid_structure',
              'observation_count': None, 'window_compatibility': 'not_evaluated',
              'temporal_policy': 'pending', 'coverage': 'unverified',
              'interval_model': 'candidate_exclusive_end',
              'metric_approved': False, 'publication_enabled': False, 'day_assigned': False}
    try:
        kind, start, end = _window(since, until)
    except (ValueError, TypeError, OverflowError, OSError, UnicodeError):
        report['reason'] = 'invalid_window'
        return report
    if not isinstance(payload, dict) or 'error' in payload:
        return report
    data = payload.get('data')
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        values = data[0].get('values')
        if isinstance(values, list):
            if len(values) > MAX_OBSERVATIONS:
                report['reason'] = 'observation_limit_exceeded'
                return report
            report['observation_count'] = len(values)
    try:
        # Intentionally separate pure value validation from the pagination gate.
        # This report is diagnostic only: no observation/series is returned.
        observations = _observations(data)
    except _ObservationError as error:
        allowed = {'invalid_data', 'ambiguous_metric', 'wrong_metric', 'wrong_period', 'invalid_values',
                   'invalid_observation', 'invalid_value', 'invalid_end_time', 'naive_end_time',
                   'duplicate_end_time', 'observation_limit_exceeded'}
        report['reason'] = str(error) if str(error) in allowed else 'invalid_structure'
        return report
    if not observations:
        report.update(status='missing_observation', reason='no_observation', observation_count=0)
        return report
    if len(observations) != 1:
        report['reason'] = 'unexpected_observation_count'
        return report
    report.update(status='valid_observation', reason='value_and_timestamp_valid', observation_count=1)
    stamp = observations[0]
    if kind == 'date':
        report['window_compatibility'] = 'unverified_timezone'
    else:
        report['window_compatibility'] = ('candidate_end_matches_until' if stamp == end else
            'candidate_end_inside_window' if start < stamp < end else 'outside_window')
    return report
