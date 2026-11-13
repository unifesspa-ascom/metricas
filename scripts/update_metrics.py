#!/usr/bin/env python3
"""Account observation only. Publication is unconditionally suspended.

--dry-run makes bounded API queries; it is NOT the six-request Facebook diagnostic.
No inventories, story insights or quarterly recalculations run here.
"""
import argparse
from datetime import datetime, timedelta
import os
import sys

from facebook_account_metrics import (
    Client as FacebookClient, METRICS as FB_METRICS, collect_series,
    require_publication_disabled,
)
import account_metrics_safety as safety

BRT = safety.BRT
PAGE_ID = os.environ.get('FB_PAGE_ID', '447748898661292')
today = datetime.now(BRT).date()
yesterday = (today - timedelta(days=1)).isoformat()
since = yesterday
until_ = today.isoformat()


def api_get(*args, **kwargs):
    """Legacy unbounded entry point remains unavailable, including direct imports."""
    raise safety.CollectionError('legacy_transport_disabled')


def fetch_facebook_metrics(*, day_zone=None):
    client = FacebookClient(os.environ.get('FB_PAGE_ACCESS_TOKEN'))
    series, statuses = collect_series(client, PAGE_ID, since, until_, day_zone=day_zone)
    for key, status in statuses.items():
        print('FB {}: {}'.format(key, status))
    return {key: days[yesterday] for key, days in series.items() if yesterday in days}


def fetch_instagram_metrics():
    client = safety.Client(os.environ.get('FB_PAGE_ACCESS_TOKEN'))
    safety.require(isinstance(PAGE_ID, str) and bool(safety.re.fullmatch(r'[0-9]{1,32}', PAGE_ID)), 'invalid_account')
    owner = safety.instagram_owner(client, PAGE_ID)
    fields = safety.instagram_series(client, owner, since, until_)
    return {key: days[yesterday] for key, days in fields.items() if yesterday in days}


def update_json(fb_metrics, ig_metrics, dry_run=False):
    if not dry_run:
        require_publication_disabled()
    with safety.Store(dry_run=True) as store:
        results = {network: {key: {yesterday: value} for key, value in fields.items()}
                   for network, fields in (('facebook', fb_metrics), ('instagram', ig_metrics))}
        candidate, confirmed = safety.apply_results(store.document, results, advance=False)
        store.save(candidate)  # Validation/concurrency checks only; never writes.
    print('account: dry_run=true confirmed={} written=false'.format(confirmed))
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not args.dry_run:
        parser.error('account_publication_suspended: somente observacao sem gravacao')
    try:
        fb = fetch_facebook_metrics()
        ig = fetch_instagram_metrics()
        update_json(fb, ig, dry_run=True)
    except safety.CollectionError as error:
        safety.warn('all', 'execution', '-', error.category, error)
        return 1
    except Exception:
        safety.warn('all', 'execution', '-', 'internal_or_storage')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
