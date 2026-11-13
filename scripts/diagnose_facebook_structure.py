#!/usr/bin/env python3
"""One authorized Facebook GET, diagnostic only, fixed closed day 2026-11-12.

Never follows links, maps days, approves metrics, or writes data.
Navigation classification and observation validation never establish coverage.
Credentials only from the process environment; never pass them as arguments.
"""
import sys
sys.dont_write_bytecode = True

import argparse
from datetime import date, datetime
import json
import os
import re
from threading import Lock
from zoneinfo import ZoneInfo

import facebook_account_metrics as fb
from account_metrics_safety import CollectionError, decode
from facebook_navigation_validation import classify_navigation, validate_observation

METRIC = 'page_media_view'
SINCE, UNTIL = '2026-11-12', '2026-11-13'
MAX_COUNT = 1000


class DiagnosticError(ValueError):
    pass


def kind(value):
    if value is None:
        return 'null'
    return {dict: 'object', list: 'array', bool: 'boolean', str: 'string',
            int: 'integer', float: 'number'}.get(type(value), 'other')


def summarize(payload):
    """Only bounded shape metadata escapes; no observation contents are returned.

    Structural validity checks field presence, not values or timestamp semantics.
    A next string establishes pending navigation, never incomplete coverage.
    """
    if not isinstance(payload, dict) or 'error' in payload:
        return 'malformed_response', None
    paging_present = 'paging' in payload
    paging = payload.get('paging', {})
    paging_object = isinstance(paging, dict)
    next_present = 'next' in paging if paging_object else None
    metadata = {
        'paging_present': paging_present,
        'paging_type': kind(paging) if paging_present else 'absent',
        'next_present': next_present,
        'next_type': kind(paging['next']) if next_present else ('absent' if paging_object else 'unavailable'),
        'previous_present': 'previous' in paging if paging_object else None,
        'data_entries': None, 'data_entries_capped': False,
        'values_observations': None, 'values_observations_capped': False,
    }
    data = payload.get('data')
    data_valid = isinstance(data, list)
    entries_valid = False
    if data_valid:
        metadata['data_entries'] = min(len(data), MAX_COUNT)
        metadata['data_entries_capped'] = len(data) > MAX_COUNT
        if not data:
            entries_valid = True
        elif len(data) == 1 and isinstance(data[0], dict):
            entry = data[0]
            values = entry.get('values')
            if isinstance(values, list):
                metadata['values_observations'] = min(len(values), MAX_COUNT)
                metadata['values_observations_capped'] = len(values) > MAX_COUNT
                entries_valid = (entry.get('name') == METRIC and entry.get('period') == 'day'
                                 and len(values) <= MAX_COUNT
                                 and all(isinstance(item, dict) and 'value' in item and 'end_time' in item
                                         for item in values))
    next_valid = paging_object
    if paging_object and next_present:
        next_valid = isinstance(paging['next'], str) and bool(paging['next'].strip())
    if not (paging_object and next_valid and data_valid and entries_valid):
        return 'malformed_response', metadata
    return ('structure_valid_navigation_pending' if next_present else 'structure_valid'), metadata


def inspect_response(payload, *, page, token):
    """Independent, sanitized analyses; never return links or observations.

    Navigation candidates do not unblock publication or establish daily coverage.
    All results are built before updating the execution report, so unexpected
    validation failures cannot leave a partially successful result behind.
    """
    status, structure = summarize(payload)
    paging = payload.get('paging', {}) if isinstance(payload, dict) else None
    navigation = classify_navigation(paging, page=page, since=SINCE, until=UNTIL, expected_token=token)
    observation = validate_observation(payload, since=SINCE, until=UNTIL)
    if status != 'malformed_response':
        if observation['status'] == 'invalid_observation':
            status = 'invalid_observation'
        elif observation['status'] == 'missing_observation':
            status = 'missing_observation'
        elif paging:
            # Includes previous-only, cursor metadata and unknown navigation.
            status = 'structure_valid_navigation_pending'
    return {'status': status, 'structure': structure, 'navigation': navigation, 'observation': observation}


def _empty_report(status='internal_error'):
    return {'status': status, 'attempts': 0, 'structure': None, 'navigation': None, 'observation': None,
            'publication_enabled': False, 'navigation_approved': False, 'metric_approved': False,
            'day_assigned': False, 'coverage': 'unverified', 'temporal_policy': 'pending'}


class StrictResponse:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)

    @property
    def status(self):
        return self.response.status

    def read(self, limit):
        raw = self.response.read(limit)
        if len(raw) <= fb.MAX_BODY:
            try:
                decode(raw)
            except CollectionError:
                raise fb.MetricError('invalid_response') from None
        return raw


class OneGetOpener:
    """Budget consumed before the only transport attempt, including any failure."""
    def __init__(self, opener):
        self._opener = opener
        self._once = Lock()
        self.attempts = 0

    def open(self, *args, **kwargs):
        if not self._once.acquire(blocking=False):
            raise fb.MetricError('request_budget_exhausted')
        self.attempts = 1
        return StrictResponse(self._opener.open(*args, **kwargs))


class Execution:
    def __init__(self):
        self._once = Lock()

    def run(self, *, authorized=False):
        if authorized is not True:
            raise DiagnosticError('explicit_authorization_required')
        if not self._once.acquire(blocking=False):
            raise DiagnosticError('execution_already_used')
        if date.fromisoformat(UNTIL) > datetime.now(ZoneInfo('America/Sao_Paulo')).date():
            raise DiagnosticError('day_not_closed')
        page = os.environ.get('FB_PAGE_ID')
        if not isinstance(page, str) or not re.fullmatch(r'[0-9]{1,32}', page):
            raise DiagnosticError('invalid_credentials')
        token = os.environ.get('FB_PAGE_ACCESS_TOKEN')
        try:
            client = fb.Client(token)
        except fb.MetricError:
            raise DiagnosticError('invalid_credentials') from None
        budget = OneGetOpener(client.opener)
        client.opener = budget
        report = _empty_report()
        try:
            payload = client.get(page, METRIC, SINCE, UNTIL)
            report.update(inspect_response(payload, page=page, token=token))
        except fb.MetricError as error:
            category = str(error)
            if category in ('http_error', 'redirect_blocked', 'api_error', 'request_budget_exhausted'):
                report['status'] = category
            elif category == 'transport_or_response_error':
                report['status'] = 'transport_error'
            elif category in ('invalid_response', 'response_too_large'):
                report['status'] = 'malformed_response'
        except Exception:
            pass  # Fixed internal_error only; never stringify a raw exception.
        report['attempts'] = budget.attempts
        return report


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise DiagnosticError('invalid_arguments')


def main(argv=None):
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--authorize-one-get', action='store_true', default=False)
    try:
        args = parser.parse_args(argv)
        report = Execution().run(authorized=args.authorize_one_get)
    except DiagnosticError as error:
        category = str(error)
        allowed = {'explicit_authorization_required', 'execution_already_used',
                   'day_not_closed', 'invalid_credentials', 'invalid_arguments'}
        report = _empty_report(category if category in allowed else 'internal_error')
    except Exception:
        report = _empty_report()
    print(json.dumps(report, allow_nan=False))
    # Exit zero means diagnostic inspection succeeded, never metric/day approval.
    return 0 if report['status'] in ('structure_valid', 'structure_valid_navigation_pending') else 1


if __name__ == '__main__':
    sys.exit(main())
