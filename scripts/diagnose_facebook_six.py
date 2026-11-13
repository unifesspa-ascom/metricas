#!/usr/bin/env python3
"""Manual Facebook-only observation, never approval or publication.

Disabled by default. After separate authorization, supply --day YYYY-MM-DD
and --authorize-six-gets, with FB_PAGE_ACCESS_TOKEN / FB_PAGE_ID in the process
environment. Never pass credentials as arguments. A closed civil date is
selected in America/Sao_Paulo only to exclude today/future; it does not establish
Meta's reporting timezone. No day mapping or historical file access occurs.
No workflow is provided. Use python -B to avoid bytecode files.
"""
import sys

# Also prevent imported local modules from creating bytecode when -B is omitted.
sys.dont_write_bytecode = True

import argparse
from datetime import date, datetime, timedelta
import json
import os
import re
from threading import Lock
from zoneinfo import ZoneInfo

import facebook_account_metrics as fb
from account_metrics_safety import CollectionError, decode

PLANNED_METRICS = (
    'page_media_view', 'page_total_media_view_unique', 'page_post_engagements',
    'page_total_actions', 'page_daily_follows_unique', 'page_views_total',
)
LIMIT = 6


class DiagnosticError(ValueError):
    pass


def closed_window(day):
    try:
        start = date.fromisoformat(day)
        if start.isoformat() != day or start >= datetime.now(ZoneInfo('America/Sao_Paulo')).date():
            raise ValueError
        end = start + timedelta(days=1)
    except (TypeError, ValueError, OverflowError):
        raise DiagnosticError('invalid_closed_day') from None
    return start.isoformat(), end.isoformat()


class StrictResponse:
    """Add the recovered JSON decoder without changing Facebook's transport."""
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
                decode(raw)  # Reject duplicate keys and nonfinite numbers in memory.
            except CollectionError:
                raise fb.MetricError('invalid_response') from None
        return raw


class StrictOpener:
    def __init__(self, opener):
        self.opener = opener

    def open(self, *args, **kwargs):
        return StrictResponse(self.opener.open(*args, **kwargs))


def observation(client, page, metric, since, until):
    result = {'metric': metric, 'status': 'invalid_structure',
              'temporal_policy': 'pending', 'daily_coverage': 'unverified',
              'metric_approved': False}
    try:
        payload = client.get(page, metric, since, until)
        values = fb.parse_observations(payload, metric)
        result['status'] = 'valid_values_temporal_pending' if values else 'no_data'
    except fb.MetricError as error:
        # Never emit str(error) directly, including unexpected categories.
        category = str(error)
        if category in ('http_error', 'redirect_blocked', 'api_error',
                        'response_too_large', 'incomplete_response', 'request_budget_exhausted'):
            result['status'] = category
        elif category == 'invalid_value':
            result['status'] = 'invalid_values'
        elif category == 'transport_or_response_error':
            result['status'] = 'transport_error'
    except Exception:
        result['status'] = 'internal_error'
    return result


class Execution:
    """One-shot execution: one client, one window, one shared six-attempt budget.

    The lock is never released: a failed or completed run cannot restart this
    execution, even concurrently. No factory/retry/resume option exists.
    """
    def __init__(self):
        self._once = Lock()
        self.attempts = 0

    def run(self, day, *, authorized=False):
        if authorized is not True:
            raise DiagnosticError('explicit_authorization_required')
        if not self._once.acquire(blocking=False):
            raise DiagnosticError('execution_already_used')
        since, until = closed_window(day)
        if (len(PLANNED_METRICS) != LIMIT or len(set(PLANNED_METRICS)) != LIMIT
                or set(PLANNED_METRICS) != set(fb.METRICS.values()) or fb.MAX_REQUESTS != LIMIT):
            raise DiagnosticError('plan_mismatch')
        # Read credentials only after authorization and preflight. No default ID.
        page = os.environ.get('FB_PAGE_ID')
        token = os.environ.get('FB_PAGE_ACCESS_TOKEN')
        if not isinstance(page, str) or not re.fullmatch(r'[0-9]{1,32}', page):
            raise DiagnosticError('invalid_credentials')
        try:
            client = fb.Client(token)
        except fb.MetricError:
            raise DiagnosticError('invalid_credentials') from None
        client.opener = StrictOpener(client.opener)
        results = []
        for metric in PLANNED_METRICS:
            if self.attempts >= LIMIT:
                raise DiagnosticError('execution_budget_exhausted')
            self.attempts += 1  # Errors consume the budget; never create a replacement client.
            results.append(observation(client, page, metric, since, until))
        return {'planned_metrics': LIMIT, 'attempts': self.attempts,
                'publication_enabled': False, 'results': results}


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error includes raw argument values.
        raise DiagnosticError('invalid_arguments')


def main(argv=None):
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--day', required=True)
    parser.add_argument('--authorize-six-gets', action='store_true', default=False)
    try:
        args = parser.parse_args(argv)
        report = Execution().run(args.day, authorized=args.authorize_six_gets)
    except DiagnosticError as error:
        allowed = {'explicit_authorization_required', 'invalid_closed_day', 'plan_mismatch',
                   'invalid_credentials', 'invalid_arguments', 'execution_already_used',
                   'execution_budget_exhausted'}
        status = str(error) if str(error) in allowed else 'internal_error'
        print(json.dumps({'status': status, 'publication_enabled': False}))
        return 2
    except Exception:
        print(json.dumps({'status': 'internal_error', 'publication_enabled': False}))
        return 1
    print(json.dumps(report, allow_nan=False))
    # A completed diagnostic is never proof of metric availability or semantics.
    return 0 if all(r['status'] in ('valid_values_temporal_pending', 'no_data')
                    for r in report['results']) else 1


if __name__ == '__main__':
    sys.exit(main())
