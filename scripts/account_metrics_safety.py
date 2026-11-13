#!/usr/bin/env python3
"""Account safety recovered selectively from fb875e6 and 020974a.

Facebook transport stays exclusively in facebook_account_metrics. Store's
atomic writing implementation remains unconditionally gated; only offline
tests replace that gate against temporary fixtures. No collector writes here.
A successful observation is not per-field certification.

Legacy data is preserved, including invalid historical counts. New counts
must be nonnegative integers. No reporting-time or full-inventory contract has
been externally verified: the registries below intentionally start empty.
Native end_time series cannot update daily cells until an audited contract is
configured. Query-scoped total_value keeps its explicitly requested day/window;
this is not certification of the API's reporting timezone. Publication objects
use their actual timestamp in America/Sao_Paulo. An exhausted inventory is an
observation, not proof of historical completeness.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import requests
from facebook_account_metrics import METRICS as FB_METRICS, require_publication_disabled

API_VERSION = 'v21.0'
BASE_URL = 'https://graph.facebook.com/' + API_VERSION
METRICS_PATH = 'dados/metricas.json'
BRT = ZoneInfo('America/Sao_Paulo')
# No fallback to impressions, users, likes or other different definitions.
IG_DIRECT_METRICS = {'alcance': 'reach'}
IG_TOTAL_VALUE_METRICS = {'interacoes': 'accounts_engaged', 'cliques': 'profile_links_taps'}
COUNTS = set(FB_METRICS) | set(IG_DIRECT_METRICS) | {
    'publicacoes', 'stories', 'visualizacoes_stories'}
OPTIONAL = {'metric_incompatible', 'invalid_response', 'invalid_structure',
            'invalid_value', 'invalid_date', 'unavailable', 'duplicate_date',
            'date_contract_unverified', 'insights_navigation_unresolved',
            'window_equivalence_unverified'}


# These are INTERNAL audited-policy registries, never populated from API
# payloads, dates, a numeric value, environment variables or old JSON content.
# Reporting contract: kind='exclusive_end', timezone=<IANA zone>, reference=
# <external evidence identifying the boundary semantics and reporting zone>.
# Until that evidence exists, preserve native daily history instead of guessing.
REPORTING_CONTRACTS = {}
# Full inventory contract: policy='complete_daily_inventory_v1', reference=
# <audited evidence that this endpoint/account/window covers ALL publications>.
# No such guarantee has been established; production reductions/additions of
# publication totals therefore remain disabled. Tests inject SYNTHETIC policies.
PUBLICATION_COVERAGE_CONTRACTS = {}


class PublicationObservations(dict):
    """Observed daily IDs, with separate, optional full-coverage evidence."""
    def __init__(self, values=(), *, network=None, account=None, evidence=None):
        super().__init__(values)
        self.network = network
        self.account = account
        self.evidence = dict(evidence or {})
        self.listing_complete = True


def audited_contract(contract, kind):
    return (isinstance(contract, dict) and contract.get('kind') == kind
            and isinstance(contract.get('reference'), str)
            and bool(contract['reference'].strip()))


class CollectionError(RuntimeError):
    def __init__(self, category, http=None, code=None, subcode=None):
        super().__init__(category)
        self.category = category
        self.http, self.code, self.subcode = http, code, subcode


def require(condition, category):
    if not condition:
        raise CollectionError(category)


def count(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def valid_day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def stamp(value):
    require(isinstance(value, str) and bool(re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})', value)), 'invalid_date')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None, 'invalid_date')
        return result
    except ValueError:
        raise CollectionError('invalid_date') from None


def reporting_day(end_time, contract=None):
    """Normalize the instant, then apply ONLY an audited daily boundary rule.

    The supported contract is a local-midnight EXCLUSIVE end in a specified
    reporting timezone. Subtracting one civil day is justified only after that
    boundary is verified. Other boundaries/unknown contracts are unavailable,
    not dates inferred from the timestamp's textual offset. This never migrates
    or re-labels any existing JSON date.
    """
    instant = stamp(end_time).astimezone(timezone.utc)
    require(audited_contract(contract, 'exclusive_end'), 'date_contract_unverified')
    try:
        zone = ZoneInfo(contract['timezone'])
        end = instant.astimezone(zone)
    except (KeyError, TypeError, ValueError):
        raise CollectionError('date_contract_unverified') from None
    require((end.hour, end.minute, end.second, end.microsecond) == (0, 0, 0, 0), 'invalid_date')
    try:
        return (end.date() - timedelta(days=1)).isoformat()
    except OverflowError:
        raise CollectionError('invalid_date') from None


def publication_day(timestamp):
    return stamp(timestamp).astimezone(BRT).date().isoformat()


def warn(network, stage, metric, category, error=None):
    # Only fixed/local identifiers and numeric error codes may reach logs.
    clean = lambda value: value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value) else '-'
    text = 'rede={} etapa={} metrica={} categoria={}'.format(
        clean(network), clean(stage), clean(metric), clean(category))
    if error:
        safe = lambda v: str(v) if type(v) is int and 0 <= v <= 999999999 else '-'
        text += ' http={} meta_code={} meta_subcode={}'.format(
            safe(error.http), safe(error.code), safe(error.subcode))
    print(text, file=sys.stderr)


def pairs(items):
    out = {}
    for key, value in items:
        require(key not in out, 'duplicate_json_key')
        out[key] = value
    return out


def decode(raw):
    def finite(value):
        number = float(value)
        require(math.isfinite(number), 'invalid_document')
        return number
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_float=finite,
                          parse_constant=lambda _: (_ for _ in ()).throw(CollectionError('invalid_document')))
    except (ValueError, UnicodeError, TypeError):
        raise CollectionError('invalid_document') from None


def validate_document(doc):
    require(isinstance(doc, dict) and isinstance(doc.get('series'), dict), 'invalid_document')
    require(doc.get('atualizado') == '' or valid_day(doc.get('atualizado')), 'invalid_document')
    for network in ('facebook', 'instagram'):
        series = doc['series'].get(network)
        require(isinstance(series, dict), 'invalid_document')
        for metric, values in series.items():
            require(isinstance(metric, str) and isinstance(values, dict), 'invalid_document')
            for day, value in values.items():
                require(valid_day(day), 'invalid_document')
                if metric in COUNTS:
                    # Archival negatives are retained, never accepted as NEW values.
                    require(type(value) is int, 'invalid_document')
    if 'conteudo' in doc:
        require(isinstance(doc['conteudo'], dict), 'invalid_document')
    return doc


def preserve_history(original, candidate):
    """Independent storage check: no deletion or replacement of historical cells."""
    for key, value in original.items():
        require(key in candidate, 'history_conflict')
        if key == 'series':
            for network, metrics in value.items():
                require(network in candidate['series'], 'history_conflict')
                if network not in ('facebook', 'instagram'):
                    require(candidate['series'][network] == metrics, 'history_conflict')
                    continue
                for metric, days in metrics.items():
                    following = candidate['series'][network].get(metric)
                    require(isinstance(following, dict), 'history_conflict')
                    for day, count_value in days.items():
                        require(day in following and type(following[day]) is type(count_value)
                                and following[day] == count_value, 'history_conflict')
        elif key == 'atualizado':
            require(candidate[key] >= value, 'history_conflict')
        else:
            require(type(candidate[key]) is type(value) and candidate[key] == value, 'history_conflict')


class Store:
    def __init__(self, path=METRICS_PATH, dry_run=False):
        self.path = Path(path)
        self.dry_run = dry_run
        self.lock = self.path.with_name(self.path.name + '.lock')
        self.nonce = uuid.uuid4().hex.encode()
        self.owned = False

    def __enter__(self):
        if not self.dry_run:
            require_publication_disabled()
        try:
            require(not self.path.is_symlink(), 'invalid_document')
            if not self.dry_run:
                try:
                    fd = os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    raise CollectionError('locked') from None
                self.owned = True
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(self.nonce)
                    handle.flush()
                    os.fsync(handle.fileno())
            self.original = self.path.read_bytes()
            self.document = validate_document(decode(self.original))
            return self
        except BaseException:
            self.release()
            raise

    def release(self):
        if self.owned:
            try:
                if self.lock.read_bytes() == self.nonce:
                    self.lock.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # A committed replacement cannot be rolled back safely here.
                # Keep the lock for manual review, report cleanup explicitly,
                # and never report a successful write as an uncommitted one.
                warn('all', 'storage', '-', 'lock_cleanup_failed')
            finally:
                self.owned = False

    def __exit__(self, *_):
        self.release()

    def save(self, document):
        if not self.dry_run:
            require_publication_disabled()
        validate_document(document)
        # Serialization MUST finish before a temporary file or target is opened.
        try:
            raw = json.dumps(document, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
        except (TypeError, ValueError, UnicodeError):
            raise CollectionError('serialization') from None
        preserve_history(decode(self.original), document)
        require(not self.path.is_symlink() and self.path.read_bytes() == self.original, 'concurrent_change')
        if self.dry_run:
            return False
        require(self.owned and self.lock.read_bytes() == self.nonce, 'lock_changed')
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode='wb', prefix='.' + self.path.name + '.',
                                             suffix='.tmp', dir=self.path.parent, delete=False) as handle:
                temp = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            require(self.path.read_bytes() == self.original, 'concurrent_change')
            require(self.lock.read_bytes() == self.nonce, 'lock_changed')
            os.replace(temp, self.path)
            temp = None
        except OSError:
            raise CollectionError('storage') from None
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        return True


def pagination_endpoint(parts, endpoint):
    try:
        return (parts.scheme == 'https' and parts.hostname == 'graph.facebook.com'
                and parts.port in (None, 443) and not parts.username and not parts.password
                and parts.path == urlsplit(endpoint).path and not parts.fragment)
    except ValueError:
        return False


def query_pairs(query):
    items = parse_qsl(query, keep_blank_values=True)
    require(len({key for key, _ in items}) == len(items), 'invalid_pagination')
    return dict(items)


def query_value(key, value):
    value = str(value)
    if key in ('fields', 'metric'):
        names = [name.strip() for name in value.split(',')]
        require(all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) for name in names), 'invalid_pagination')
        return tuple(sorted(set(names)))
    if key in ('since', 'until'):
        if valid_day(value):
            # A bare API date has no proven timezone. Do NOT silently equate
            # it with UTC epoch midnight. An explicit offset/epoch is an instant.
            return ('date', value)
        try:
            if re.fullmatch(r'-?\d+', value):
                instant = datetime.fromtimestamp(int(value), timezone.utc)
            else:
                instant = stamp(value).astimezone(timezone.utc)
            return ('instant', instant.isoformat())
        except (ValueError, OverflowError, OSError, CollectionError):
            raise CollectionError('window_equivalence_unverified') from None
    if key == 'limit':
        require(bool(re.fullmatch(r'\d+', value)) and int(value) > 0, 'invalid_pagination')
        return int(value)
    return value


def window_relation(original, following):
    """Same window, proven forward navigation, or refusal; never infer a zone."""
    changed = False
    for key in ('since', 'until'):
        if key not in following:
            continue  # Original filter is explicitly restored on the request.
        require(key in original, 'pagination_window_changed')
        old, new = query_value(key, original[key]), query_value(key, following[key])
        require(old[0] == new[0], 'window_equivalence_unverified')
        changed = changed or old != new
    if not changed:
        return 'same'
    require(all(key in original and key in following for key in ('since', 'until')), 'pagination_window_changed')
    start, end = (query_value(key, following[key]) for key in ('since', 'until'))
    old_end = query_value('until', original['until'])
    require(start[0] == end[0] == old_end[0] and start[1] < end[1], 'pagination_window_changed')
    if start[1] >= old_end[1]:
        return 'forward_navigation'
    raise CollectionError('pagination_window_changed')


MAX_REQUESTS = 100
MAX_PAGES = 10
MAX_BODY = 262144


class Client:
    def __init__(self, token, max_requests=MAX_REQUESTS, max_pages=MAX_PAGES, attempts=1):
        require(isinstance(token, str) and bool(token) and not any(c.isspace() for c in token), 'authentication')
        require(type(max_requests) is int and 0 < max_requests <= MAX_REQUESTS and
                type(max_pages) is int and 0 < max_pages <= MAX_PAGES and attempts == 1, 'limits')
        self.token, self.max_requests, self.max_pages = token, max_requests, max_pages
        self.requests = 0

    def get(self, url, params):
        try:
            parts = urlsplit(url)
            require(parts.scheme == 'https' and parts.hostname == 'graph.facebook.com'
                    and not parts.username and not parts.password and parts.port in (None, 443)
                    and not parts.fragment and parts.path.startswith('/' + API_VERSION + '/'), 'invalid_pagination')
        except ValueError:
            raise CollectionError('invalid_pagination') from None
        query = query_pairs(parts.query)
        require(not query.keys() & params.keys(), 'invalid_pagination')
        query.update(params)
        require('access_token' not in query or query['access_token'] == self.token, 'invalid_pagination')
        query.pop('access_token', None)
        require(self.requests < self.max_requests, 'request_limit')
        self.requests += 1
        try:
            response = requests.get(parts._replace(query='').geturl(), params=query,
                                    headers={'Authorization': 'Bearer ' + self.token},
                                    timeout=20, allow_redirects=False, stream=True)
        except requests.RequestException:
            raise CollectionError('connection') from None
        status = response.status_code
        try:
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                require(len(raw) + len(chunk) <= MAX_BODY, 'response_too_large')
                raw.extend(chunk)
            data = decode(raw)
        except CollectionError as error:
            if error.category == 'response_too_large':
                raise
            data = None
        except (ValueError, TypeError, requests.RequestException):
            data = None
        finally:
            response.close()
        error = data.get('error') if isinstance(data, dict) else None
        has_error = isinstance(data, dict) and 'error' in data
        if status == 200 and not has_error:
            require(isinstance(data, dict), 'invalid_response')
            return data
        code = error.get('code') if isinstance(error, dict) else None
        subcode = error.get('error_subcode') if isinstance(error, dict) else None
        if 300 <= status < 400:
            category = 'redirect'
        elif status == 401 or code in (102, 190):
            category = 'authentication'
        elif status == 403 or code in (10, 200):
            category = 'permission'
        elif status == 429 or code in (4, 17, 32, 613):
            category = 'rate_limit'
        elif status >= 500 or code in (1, 2):
            category = 'service'
        elif status == 400 and code in (100, 12):
            category = 'metric_incompatible'
        else:
            category = 'api_error'
        raise CollectionError(category, status, code, subcode)

    def pages(self, path, params, *, completion_check=None):
        endpoint = BASE_URL + '/' + path.lstrip('/')
        url, query = endpoint, dict(params, access_token=self.token)
        visited, page_bodies, result = set(), set(), []
        cursor_keys = {'after', 'before', '__paging_token', '__after', '__before'}
        for _ in range(self.max_pages):
            parts = urlsplit(url)
            require(pagination_endpoint(parts, endpoint), 'invalid_pagination')
            effective = query_pairs(parts.query)
            require(not (effective.keys() & query.keys()), 'invalid_pagination')
            effective.update(query)
            identity = (parts.path, tuple(sorted((key, query_value(key, value))
                        for key, value in effective.items() if key != 'access_token')))
            require(identity not in visited, 'pagination_cycle')
            visited.add(identity)
            data = self.get(url, query)
            require(isinstance(data.get('data'), list), 'invalid_structure')
            if data['data']:
                try:
                    records = [json.dumps(item, sort_keys=True, allow_nan=False, separators=(',', ':'))
                               for item in data['data']]
                    body = ('[' + ','.join(sorted(records)) + ']').encode('utf-8')
                except (TypeError, ValueError, UnicodeError):
                    raise CollectionError('invalid_structure') from None
                fingerprint = hashlib.sha256(body).digest()
                require(fingerprint not in page_bodies, 'duplicate_page')
                page_bodies.add(fingerprint)
            result.extend(data['data'])
            paging = data.get('paging', {})
            require(isinstance(paging, dict), 'invalid_pagination')
            if 'next' not in paging:
                return result
            next_url = paging['next']
            require(isinstance(next_url, str) and bool(next_url), 'invalid_pagination')
            parts = urlsplit(next_url)
            require(pagination_endpoint(parts, endpoint), 'invalid_pagination')
            following = query_pairs(parts.query)
            allowed = set(params) | cursor_keys | {'access_token', 'limit'}
            require(following.keys() <= allowed, 'pagination_filter_changed')
            require('access_token' not in following or following['access_token'] == self.token, 'invalid_pagination')
            for key, value in following.items():
                if key in cursor_keys:
                    require(bool(value), 'invalid_pagination')
                elif key not in ('access_token', 'since', 'until', 'limit'):
                    require(key in params and query_value(key, value) == query_value(key, params[key]), 'pagination_filter_changed')
                elif key == 'limit':
                    query_value(key, value)
            relation = window_relation(params, following)
            if relation != 'same':
                require(not (following.keys() & cursor_keys), 'pagination_window_changed')
                require(completion_check is not None, 'pagination_window_changed')
                # A navigation link is NOT another page of this inventory.
                # Ignore it only when all requested values are demonstrably
                # complete; otherwise discard this metric without guessing.
                require(completion_check(result), 'insights_navigation_unresolved')
                return result
            url = next_url
            query = {key: value for key, value in params.items() if key not in following}
            if 'access_token' not in following:
                query['access_token'] = self.token
        raise CollectionError('page_limit')


def daily_values(entries, metric, start, end, network):
    values, rejected, date_warnings = {}, set(), set()
    for entry in entries:
        require(isinstance(entry, dict) and entry.get('name') == metric
                and entry.get('period') == 'day' and isinstance(entry.get('values'), list), 'invalid_structure')
        for item in entry['values']:
            require(isinstance(item, dict), 'invalid_structure')
            try:
                day = reporting_day(item.get('end_time'), REPORTING_CONTRACTS.get((network, metric)))
            except CollectionError as error:
                if error.category not in date_warnings:
                    warn(network, 'insights', metric, error.category)
                    date_warnings.add(error.category)
                continue
            if not start <= day < end:
                continue
            value = item.get('value')
            if not count(value):
                warn(network, 'insights', metric, 'invalid_value')
                rejected.add(day)
                values.pop(day, None)
                continue
            if day in values and values[day] != value:
                rejected.add(day)
                values.pop(day, None)
                warn(network, 'insights', metric, 'duplicate_date')
            elif day not in rejected:
                values[day] = value
    return values


def insight_complete(entries, metric, start, end, network, total=False):
    if total:
        return (len(entries) == 1 and isinstance(entries[0], dict)
                and entries[0].get('name') == metric and entries[0].get('period') == 'day'
                and isinstance(entries[0].get('total_value'), dict)
                and count(entries[0]['total_value'].get('value')))
    try:
        values = daily_values(entries, metric, start, end, network)
    except CollectionError:
        return False
    expected = (date.fromisoformat(end) - date.fromisoformat(start)).days
    return len(values) == expected


def insight_series(client, owner, metric, start, end, network, total=False):
    require(valid_day(start) and valid_day(end) and start < end, 'date_interval')
    if total:
        require(date.fromisoformat(end) - date.fromisoformat(start) == timedelta(days=1), 'date_interval')
    params = {'metric': metric, 'period': 'day', 'since': start, 'until': end}
    if total:
        params['metric_type'] = 'total_value'
    try:
        entries = client.pages(owner + '/insights', params, completion_check=lambda entries:
                               insight_complete(entries, metric, start, end, network, total))
        if total:
            require(not entries or (len(entries) == 1 and isinstance(entries[0], dict)
                    and entries[0].get('name') == metric and entries[0].get('period') == 'day'), 'invalid_structure')
            if not entries:
                values = {}
            else:
                tv = entries[0].get('total_value')
                require(isinstance(tv, dict), 'invalid_structure')
                value = tv.get('value')
                values = {start: value} if count(value) else {}
                if not count(value):
                    warn(network, 'insights', metric, 'invalid_value')
        else:
            values = daily_values(entries, metric, start, end, network)
        if not values:
            warn(network, 'insights', metric, 'unavailable')
        return values
    except CollectionError as error:
        # General failures and incomplete cursor pagination remain fatal.
        # Unproven temporal navigation discards this optional metric entirely;
        # never accept a partial first page or hide a programming bug.
        if error.category not in OPTIONAL:
            raise
        warn(network, 'insights', metric, error.category, error)
        return {}


def instagram_owner(client, page_id):
    obj = client.get(BASE_URL + '/' + page_id, {'fields': 'instagram_business_account', 'access_token': client.token})
    account = obj.get('instagram_business_account')
    require(isinstance(account, dict) and isinstance(account.get('id'), str)
            and bool(re.fullmatch(r'[0-9]+', account['id'])), 'invalid_account')
    return account['id']


def instagram_series(client, owner, start, end):
    result = {'alcance': insight_series(client, owner, 'reach', start, end, 'instagram')}
    day = date.fromisoformat(start)
    while day.isoformat() < end:
        next_day = (day + timedelta(days=1)).isoformat()
        for field, metric in IG_TOTAL_VALUE_METRICS.items():
            values = insight_series(client, owner, metric, day.isoformat(), next_day, 'instagram', total=True)
            result.setdefault(field, {}).update(values)
        day += timedelta(days=1)
    # /media media_type does not establish STORY membership. Do not replace
    # archived stories/impressions with zeros or new views from another source.
    warn('instagram', 'inventory', 'stories', 'legacy_preserved')
    warn('instagram', 'inventory', 'visualizacoes_stories', 'legacy_preserved')
    return result


def publication_counts(client, owner, start, end, network):
    fb = network == 'facebook'
    entries = client.pages(owner + ('/published_posts' if fb else '/media'),
                           {'fields': 'id,created_time' if fb else 'id,timestamp,media_type',
                            'since': start, 'until': end, 'limit': 100})
    records = {}
    for item in entries:
        require(isinstance(item, dict) and isinstance(item.get('id'), str)
                and bool(re.fullmatch(r'[A-Za-z0-9_-]+', item['id'])), 'invalid_listing')
        if not fb:
            require(item.get('media_type') in ('IMAGE', 'VIDEO', 'CAROUSEL_ALBUM', 'REEL'), 'invalid_listing')
        day = publication_day(item.get('created_time' if fb else 'timestamp'))
        pid = item['id']
        require(pid not in records or records[pid] == day, 'conflicting_publication_date')
        records[pid] = day
    counts = {}
    for day in records.values():
        if start <= day < end:
            counts[day] = counts.get(day, 0) + 1
    # Even an exhausted empty listing proves no observed records, not zero
    # actually published for that day (historic availability may be limited).
    account = hashlib.sha256(owner.encode('utf-8')).hexdigest()
    contract = PUBLICATION_COVERAGE_CONTRACTS.get((network, account))
    evidence = {}
    if (isinstance(contract, dict) and contract.get('policy') == 'complete_daily_inventory_v1'
            and isinstance(contract.get('reference'), str) and contract['reference'].strip()
            and valid_day(contract.get('start')) and valid_day(contract.get('end'))
            and contract['start'] <= start < end <= contract['end']):
        day = date.fromisoformat(start)
        while day.isoformat() < end:
            key = day.isoformat()
            counts.setdefault(key, 0)
            evidence[key] = (network, account, contract['reference'], 'complete_daily_inventory_v1')
            day += timedelta(days=1)
    if not counts:
        warn(network, 'inventory', 'publicacoes', 'unavailable')
    return PublicationObservations(counts, network=network, account=account, evidence=evidence)


def join_observations(existing, incoming, network):
    require(isinstance(incoming, dict), 'invalid_new_value')
    for day, value in incoming.items():
        require(valid_day(day) and count(value), 'invalid_new_value')
        require(day not in existing or existing[day] == value, 'conflicting_observations')
    if isinstance(incoming, PublicationObservations):
        require(incoming.network == network and incoming.listing_complete, 'invalid_listing')
        require(existing.account is None or existing.account == incoming.account, 'invalid_account')
        existing.account = incoming.account
        existing.evidence.update(incoming.evidence)
    existing.update(incoming)


def publication_evidence(confirmed, network, day):
    if not isinstance(confirmed, PublicationObservations):
        return False
    proof = confirmed.evidence.get(day)
    contract = PUBLICATION_COVERAGE_CONTRACTS.get((network, confirmed.account))
    return (confirmed.listing_complete and confirmed.network == network
            and isinstance(contract, dict) and contract.get('policy') == 'complete_daily_inventory_v1'
            and isinstance(contract.get('reference'), str) and bool(contract['reference'].strip())
            and valid_day(contract.get('start')) and valid_day(contract.get('end'))
            and contract['start'] <= day < contract['end']
            and proof == (network, confirmed.account, contract['reference'], 'complete_daily_inventory_v1'))


def merge_series(existing, confirmed, *, metric=None, network='all'):
    require(isinstance(existing, dict) and isinstance(confirmed, dict), 'invalid_document')
    for day, value in confirmed.items():
        require(valid_day(day) and count(value), 'invalid_new_value')
    if metric == 'publicacoes' or isinstance(confirmed, PublicationObservations):
        # Validate all eligible changes before mutating even this in-memory dict.
        for day, value in confirmed.items():
            if publication_evidence(confirmed, network, day):
                require(day not in existing or (type(existing[day]) is int and existing[day] == value), 'history_conflict')
        applied = 0
        for day, value in confirmed.items():
            if publication_evidence(confirmed, network, day):
                existing[day] = value
                applied += 1
            else:
                category = ('observed_count_without_total' if day not in existing else
                            'observed_count_matches_history' if existing[day] == value else
                            'observed_count_divergence_preserved')
                warn(network, 'inventory', 'publicacoes', category)
        return applied
    for day, value in confirmed.items():
        require(day not in existing or (type(existing[day]) is int and existing[day] == value), 'history_conflict')
    existing.update(confirmed)
    return len(confirmed)


def apply_results(document, results, advance=False):
    validate_document(document)
    candidate = copy.deepcopy(document)
    confirmed = 0
    days = []
    for network, fields in results.items():
        require(network in ('facebook', 'instagram') and isinstance(fields, dict), 'invalid_new_value')
        for metric, values in fields.items():
            require(metric in COUNTS and isinstance(values, dict), 'invalid_new_value')
            if values:
                existing = candidate['series'][network].get(metric, {})
                applied = merge_series(existing, values, metric=metric, network=network)
                if applied:
                    candidate['series'][network][metric] = existing
                    confirmed += applied
                    days.extend(day for day in values if metric != 'publicacoes' or publication_evidence(values, network, day))
    if advance and days:
        candidate['atualizado'] = max(candidate['atualizado'], max(days))
    # Legacy conteudo is never recalculated from partial daily inventories.
    return candidate, confirmed
