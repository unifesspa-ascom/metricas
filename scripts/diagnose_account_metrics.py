#!/usr/bin/env python3
"""Independent, read-only Meta probe. No collector imports or data-file writes."""
import argparse
import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

MAX_REQUESTS = 16
MAX_BODY = 262144
HOST = 'https://graph.facebook.com/v21.0/'
FB = ('page_total_media_view_unique', 'page_media_view',
      'page_post_engagements', 'page_total_actions',
      'page_daily_follows_unique', 'page_views_total',
      'page_impressions_unique', 'page_impressions')
IG = ('reach', 'accounts_engaged', 'profile_links_taps')
ROOT = Path(__file__).resolve().parents[1]


def page_id_from_source(path=ROOT / 'scripts/update_metrics.py'):
    """Read the published non-sensitive default without importing the collector."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and
                any(isinstance(t, ast.Name) and t.id == 'PAGE_ID' for t in node.targets)):
            call = node.value
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and ast.unparse(call.func) == 'os.environ.get'
                    and len(call.args) == 2
                    and ast.literal_eval(call.args[0]) == 'FB_PAGE_ID'):
                value = ast.literal_eval(call.args[1])
                if isinstance(value, str) and re.fullmatch(r'[0-9]{1,32}', value):
                    return value
    raise ValueError('configuration_unavailable')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, token):
        if not token or any(c.isspace() for c in token):
            raise ValueError('invalid_credentials')
        self.token = token
        self.count = 0
        # No retry handler; redirects, including same-host ones, are rejected.
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, account, metric, since, until):
        if not re.fullmatch(r'[0-9]{1,32}', account):
            raise ValueError('invalid_configuration')
        if metric not in (*FB, *IG, 'discovery'):
            raise ValueError('invalid_probe')
        if self.count >= MAX_REQUESTS:
            raise ValueError('request_budget_exhausted')
        params = {'since': since, 'until': until, 'period': 'day'}
        if metric == 'discovery':
            path = account
            params = {'fields': 'instagram_business_account'}
        elif metric in FB:
            path = account + '/insights/' + metric + '/day'
        else:
            path = account + '/insights'
            params['metric'] = metric
            if metric != 'reach':
                params['metric_type'] = 'total_value'
        request = urllib.request.Request(HOST + path + '?' + urllib.parse.urlencode(params),
            headers={'Authorization': 'Bearer ' + self.token}, method='GET')
        self.count += 1  # Failed attempts consume budget as well.
        try:
            with self.opener.open(request, timeout=20) as response:
                if response.status != 200:
                    return 'http_error', None
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    return 'oversize', None
                data = json.loads(raw)
                if not isinstance(data, dict):
                    return 'invalid_response', None
                if 'error' in data:
                    return 'api_error', None
                return 'ok', data
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            return ('redirect_blocked' if 300 <= status < 400 else 'http_error'), None
        except Exception:
            # Never format exceptions: they can contain authenticated URLs/headers.
            return 'transport_or_response_error', None


def diagnose(client, page, since, until):
    results = []
    for metric in FB:
        status, data = client.get(page, metric, since, until)
        results.append({'probe': metric, 'status': classify(status, data)})
    status, data = client.get(page, 'discovery', since, until)
    ig = data.get('instagram_business_account') if status == 'ok' else None
    account = ig.get('id') if isinstance(ig, dict) else None
    valid = isinstance(account, str) and re.fullmatch(r'[0-9]{1,32}', account)
    results.append({'probe': 'discovery', 'status': 'ok' if valid else 'unavailable'})
    for metric in IG:
        if valid:
            status, data = client.get(account, metric, since, until)
            status = classify(status, data)
        else:
            status = 'skipped'
        results.append({'probe': metric, 'status': status})
    return {'requests': client.count, 'limit': MAX_REQUESTS, 'results': results}


def classify(status, data):
    if status != 'ok':
        return status
    entries = data.get('data')
    return 'data_present' if isinstance(entries, list) and entries else 'empty_or_missing'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-read-only', action='store_true')
    args = parser.parse_args()
    try:
        page = page_id_from_source()
        if not args.execute_read_only:
            print('Preflight OK: 12 planned GETs; hard limit 16; no network used.')
            return 0
        client = Client(os.environ.get('FB_PAGE_ACCESS_TOKEN', ''))
        today = datetime.now(timezone(timedelta(hours=-3))).date()
        result = diagnose(client, page, (today - timedelta(days=1)).isoformat(), today.isoformat())
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        print('diagnostic_failed_safely')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
