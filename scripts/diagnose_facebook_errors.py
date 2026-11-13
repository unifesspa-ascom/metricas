#!/usr/bin/env python3
"""Two fixed read-only Facebook probes; numeric error metadata only."""
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

MAX_REQUESTS = 2
MAX_BODY = 65536
HOST = 'https://graph.facebook.com/v21.0/'
METRIC = 'page_media_view'
PROBES = ('legacy_path', 'metric_parameter')
ROOT = Path(__file__).resolve().parents[1]


def page_id_from_source(path=ROOT / 'scripts/update_metrics.py'):
    # Parse trusted pinned configuration; never import the production collector.
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


def number(value):
    # Reject bools, floats, strings (including numeric strings), huge values.
    return value if type(value) is int and 0 <= value <= 2147483647 else None


def result(probe, status, http_status=None):
    return {'probe': probe, 'status': status, 'http_status': http_status,
            'meta_code': None, 'meta_subcode': None}


def summarize(probe, status, raw):
    http = status if type(status) is int and 100 <= status <= 599 else None
    out = result(probe, 'http_error' if http and http >= 400 else 'unexpected_http', http)
    if http is not None and 300 <= http < 400:
        out['status'] = 'redirect_blocked'
        return out  # Never parse a redirect response body or follow Location.
    if len(raw) > MAX_BODY:
        out['status'] = 'oversize'
        return out
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        if http == 200:
            out['status'] = 'invalid_response'
        return out
    if not isinstance(data, dict):
        if http == 200:
            out['status'] = 'invalid_response'
        return out
    error = data.get('error')
    if isinstance(error, dict):
        out['meta_code'] = number(error.get('code'))
        out['meta_subcode'] = number(error.get('error_subcode'))
        out['status'] = 'api_error' if http == 200 else out['status']
    elif http == 200:
        entries = data.get('data')
        out['status'] = 'data_present' if isinstance(entries, list) and entries else 'empty_or_missing'
    return out


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, token):
        if not token or any(c.isspace() for c in token):
            raise ValueError('invalid_credentials')
        self.token = token
        self.count = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, page, probe, since, until):
        if probe not in PROBES or not re.fullmatch(r'[0-9]{1,32}', page):
            raise ValueError('invalid_probe')
        if not all(re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', s) for s in (since, until)):
            raise ValueError('invalid_window')
        if self.count >= MAX_REQUESTS:
            raise ValueError('request_budget_exhausted')
        params = {'since': since, 'until': until, 'period': 'day'}
        path = page + '/insights'
        if probe == 'legacy_path':
            path += '/' + METRIC + '/day'
        else:
            params['metric'] = METRIC
        req = urllib.request.Request(HOST + path + '?' + urllib.parse.urlencode(params),
            headers={'Authorization': 'Bearer ' + self.token}, method='GET')
        self.count += 1  # Failed attempts count; no automatic retries.
        try:
            with self.opener.open(req, timeout=20) as response:
                if 300 <= response.status < 400:
                    return result(probe, 'redirect_blocked', response.status)
                return summarize(probe, response.status, response.read(MAX_BODY + 1))
        except urllib.error.HTTPError as error:
            try:
                if 300 <= error.code < 400:
                    return result(probe, 'redirect_blocked', error.code)
                return summarize(probe, error.code, error.read(MAX_BODY + 1))
            except Exception:
                return result(probe, 'body_read_error',
                              error.code if type(error.code) is int and 100 <= error.code <= 599 else None)
            finally:
                error.close()
        except Exception:
            return result(probe, 'transport_or_response_error')


def diagnose(client, page, since, until):
    results = [client.get(page, probe, since, until) for probe in PROBES]
    return {'requests': client.count, 'limit': MAX_REQUESTS, 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-read-only', action='store_true')
    args = parser.parse_args()
    try:
        page = page_id_from_source()
        if not args.execute_read_only:
            print('Preflight OK: 2 fixed GETs; hard limit 2; no network used.')
            return 0
        client = Client(os.environ.get('FB_PAGE_ACCESS_TOKEN', ''))
        today = datetime.now(timezone(timedelta(hours=-3))).date()
        report = diagnose(client, page, (today - timedelta(days=1)).isoformat(), today.isoformat())
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 0
    except Exception:
        print('diagnostic_failed_safely')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
