#!/usr/bin/env python3
"""Account backfill is suspended before credentials, disk or network access.

The Facebook adapter remains available for offline contract tests only.
"""
import argparse
import os

from facebook_account_metrics import Client as FacebookClient, METRICS as FB_METRICS, collect_series

PAGE_ID = os.environ.get('FB_PAGE_ID', '447748898661292')


def api_get(*args, **kwargs):
    raise RuntimeError('account_backfill_suspended')


def fetch_fb_insights_month(since, until_, *, day_zone=None):
    client = FacebookClient(os.environ.get('FB_PAGE_ACCESS_TOKEN'))
    series, statuses = collect_series(client, PAGE_ID, since, until_, day_zone=day_zone)
    for key, status in statuses.items():
        print('FB {}: {}'.format(key, status))
    return series


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--year', type=int)
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--skip-facebook', action='store_true')
    parser.add_argument('--skip-instagram', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.parse_args()
    parser.error('account_backfill_suspended: nenhuma consulta ou gravacao autorizada')


if __name__ == '__main__':
    main()
