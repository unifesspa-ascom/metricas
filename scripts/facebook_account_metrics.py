"""Facebook account Insights only. No publication, collector imports or fallback metrics."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE_URL = 'https://graph.facebook.com/v21.0'
MAX_REQUESTS = 6
MAX_BODY = 262144
METRICS = {
    'visualizadores': 'page_total_media_view_unique',
    'visualizacoes': 'page_media_view',
    'interacoes': 'page_post_engagements',
    'cliques': 'page_total_actions',
    'seguidores': 'page_daily_follows_unique',
    'visitas': 'page_views_total',
}
# No date convention or metric has been approved for production writes.
# A new review must replace these gates after real semantic validation.
APPROVED_DAY_ZONE = None
PUBLICATION_ENABLED = False


class MetricError(ValueError):
    """Only fixed categories may cross the transport/parser boundary."""


def require(condition, category):
    if not condition:
        raise MetricError(category)


def window(since, until):
    require(isinstance(since, str) and isinstance(until, str), 'invalid_window')
    try:
        start, end = date.fromisoformat(since), date.fromisoformat(until)
        require(start.isoformat() == since and end.isoformat() == until, 'invalid_window')
        require(0 < (end - start).days <= 31, 'invalid_window')
    except (ValueError, TypeError):
        raise MetricError('invalid_window') from None
    return start, end


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    """One fixed metric per GET. Failed attempts consume the six-request budget."""
    def __init__(self, token):
        require(isinstance(token, str) and bool(token) and
                not any(c.isspace() for c in token), 'invalid_credentials')
        self.token = token
        self.count = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, page, metric, since, until):
        require(isinstance(page, str) and re.fullmatch(r'[0-9]{1,32}', page), 'invalid_page')
        require(metric in METRICS.values(), 'unknown_metric')
        window(since, until)
        require(self.count < MAX_REQUESTS, 'request_budget_exhausted')
        query = urllib.parse.urlencode({'metric': metric, 'period': 'day',
                                      'since': since, 'until': until})
        req = urllib.request.Request(BASE_URL + '/' + page + '/insights?' + query,
            headers={'Authorization': 'Bearer ' + self.token}, method='GET')
        self.count += 1
        try:
            with self.opener.open(req, timeout=20) as response:
                require(response.status == 200, 'http_error')
                raw = response.read(MAX_BODY + 1)
                require(len(raw) <= MAX_BODY, 'response_too_large')
                payload = json.loads(raw)
                require(isinstance(payload, dict), 'invalid_response')
                require('error' not in payload, 'api_error')
                return payload
        except urllib.error.HTTPError as error:
            category = 'redirect_blocked' if 300 <= error.code < 400 else 'http_error'
            error.close()
            raise MetricError(category) from None
        except MetricError:
            raise
        except Exception:
            raise MetricError('transport_or_response_error') from None


def parse_observations(payload, metric):
    """Validate the entire response atomically. Missing data stays absent, never zero."""
    require(metric in METRICS.values(), 'unknown_metric')
    require(isinstance(payload, dict) and 'error' not in payload, 'invalid_response')
    paging = payload.get('paging', {})
    require(isinstance(paging, dict) and not paging.get('next'), 'incomplete_response')
    require('data' in payload and isinstance(payload['data'], list), 'invalid_data')
    if not payload['data']:
        return []
    require(len(payload['data']) == 1, 'ambiguous_metric')
    entry = payload['data'][0]
    require(isinstance(entry, dict) and entry.get('name') == metric, 'wrong_metric')
    require(entry.get('period') == 'day', 'wrong_period')
    require('values' in entry and isinstance(entry['values'], list), 'invalid_values')
    observations = []
    seen = set()
    for item in entry['values']:
        require(isinstance(item, dict), 'invalid_observation')
        value = item.get('value')
        require(type(value) is int and 0 <= value <= 9007199254740991, 'invalid_value')
        stamp = item.get('end_time')
        require(isinstance(stamp, str) and re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:?[0-9]{2})', stamp),
            'invalid_end_time')
        try:
            end = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        except ValueError:
            raise MetricError('invalid_end_time') from None
        require(end.utcoffset() is not None, 'naive_end_time')
        require(end not in seen, 'duplicate_end_time')
        seen.add(end)
        observations.append((end, value))
    return observations


def map_days(observations, since, until, day_zone):
    """Explicit candidate policy only: midnight is the exclusive end of the previous day.

    No default zone is invented from the timestamp or from the operator's timezone.
    This is a candidate rule to test against real responses, not an approved policy.
    """
    start, finish = window(since, until)
    require(isinstance(day_zone, str) and bool(day_zone), 'date_policy_unreviewed')
    try:
        zone = ZoneInfo(day_zone)
    except Exception:
        raise MetricError('invalid_day_zone') from None
    series = {}
    for end, value in observations:
        local = end.astimezone(zone)
        require(local.time() == time(0), 'unexpected_day_boundary')
        day = local.date() - timedelta(days=1)  # Calendar day, including DST transitions.
        require(start <= day < finish, 'out_of_window')
        key = day.isoformat()
        require(key not in series, 'duplicate_day')
        series[key] = value
    return series


def collect_series(client, page, since, until, *, day_zone=APPROVED_DAY_ZONE):
    """At most six requests. Every metric is independent; no fallback or false zeros."""
    window(since, until)
    series, statuses = {}, {}
    for key, metric in METRICS.items():
        try:
            observations = parse_observations(client.get(page, metric, since, until), metric)
            series[key] = map_days(observations, since, until, day_zone) if observations else {}
            statuses[key] = 'validated_candidate' if observations else 'missing'
        except MetricError as error:
            series[key] = {}
            statuses[key] = str(error)
    return series, statuses


def append_candidate(existing, additions, *, since, until):
    """Pure future merge candidate: no file I/O, no overwrites, no historical repairs."""
    start, finish = window(since, until)
    require(isinstance(existing, dict) and isinstance(additions, dict), 'invalid_series')
    candidate = deepcopy(existing)
    for key, days in additions.items():
        require(key in METRICS and isinstance(days, dict), 'invalid_series')
        require(key not in candidate or isinstance(candidate[key], dict), 'invalid_series')
        for day, value in days.items():
            require(isinstance(day, str), 'invalid_day')
            try:
                parsed = date.fromisoformat(day)
            except ValueError:
                raise MetricError('invalid_day') from None
            require(parsed.isoformat() == day and start <= parsed < finish, 'out_of_window')
            require(type(value) is int and 0 <= value <= 9007199254740991, 'invalid_value')
            previous = candidate.get(key, {})
            require(day not in previous or (type(previous[day]) is int and previous[day] == value),
                    'history_conflict')
            candidate.setdefault(key, {})[day] = value
    return candidate


def require_publication_disabled():
    # Intentionally unconditional. No CLI flag/env var may silently unlock writes.
    raise MetricError('account_publication_suspended')
