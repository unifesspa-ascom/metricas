"""Offline account safety tests. All network access is blocked at module scope."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

sys.path.insert(0, str(ROOT / 'scripts'))
m = module('safe_accounts', 'scripts/account_metrics_safety.py')



# Deliberately synthetic: these fixtures DO NOT establish a Meta contract.
SYNTHETIC_CONTRACT = {'kind': 'exclusive_end', 'timezone': 'Etc/GMT+8',
                      'reference': 'offline-fixture-only-not-Meta-evidence'}

def synthetic_contracts(core):
    return {(network, metric): dict(SYNTHETIC_CONTRACT)
            for network, metrics in (('facebook', core.FB_METRICS.values()), ('instagram', ['reach']))
            for metric in metrics}


def setUpModule():
    global blockers
    blockers = [patch('socket.socket.connect', side_effect=AssertionError('Network blocked')),
                patch('socket.create_connection', side_effect=AssertionError('Network blocked')),
                patch('requests.sessions.Session.request', side_effect=AssertionError('Real API blocked'))]
    for blocker in blockers:
        blocker.start()


def tearDownModule():
    for blocker in reversed(blockers):
        blocker.stop()


def document():
    return {'atualizado': '2026-11-01', 'series': {
        'facebook': {'visualizacoes': {'2026-11-01': 8}},
        'instagram': {'interacoes': {'2025-01-09': -1, '2026-11-01': 9},
                      'stories': {'2026-11-01': 4}, 'visualizacoes_stories': {'2026-11-01': 20}}},
        'conteudo': {'2026': {'instagram': [{'feed': 100, 'stories': 40}]}},
        'unknown': {'original': [1, None, 'keep']}}


def response(data, status=200):
    obj = Mock(status_code=status)
    obj.iter_content.return_value = [json.dumps(data).encode()]
    return obj


def series(metric='reach', value=7, end='2026-11-02T08:00:00+0000'):
    return {'name': metric, 'period': 'day', 'values': [{'end_time': end, 'value': value}]}


def total(metric='accounts_engaged', value=7):
    return {'name': metric, 'period': 'day', 'total_value': {'value': value}}


class SafetyTests(unittest.TestCase):
    def setUp(self):
        contracts = patch.dict(m.REPORTING_CONTRACTS, synthetic_contracts(m))
        contracts.start(); self.addCleanup(contracts.stop)
        self.sink = io.StringIO()
        self.redirects = [contextlib.redirect_stdout(self.sink), contextlib.redirect_stderr(self.sink)]
        for redirect in self.redirects:
            redirect.__enter__()

    def tearDown(self):
        for redirect in reversed(self.redirects):
            redirect.__exit__(None, None, None)

    def client(self, **kw):
        return m.Client('synthetic-secret', **kw)

    def values(self, entries):
        return m.daily_values(entries, 'reach', '2026-11-01', '2026-11-03', 'instagram')

    def test_zero_confirmed_and_positive(self):
        self.assertEqual(self.values([series(value=0)]), {'2026-11-01': 0})
        self.assertEqual(self.values([series(value=8)]), {'2026-11-01': 8})

    def test_invalid_counts_are_omitted(self):
        for value in (None, False, True, -1, 1.5, '3', float('inf'), float('nan'), {}, []):
            with self.subTest(value=repr(value)):
                self.assertEqual(self.values([series(value=value)]), {})

    def test_missing_value_is_not_zero(self):
        entry = series()
        del entry['values'][0]['value']
        self.assertEqual(self.values([entry]), {})

    def test_no_fallback_to_another_date(self):
        self.assertEqual(self.values([series(end='2026-11-05T08:00:00Z')]), {})

    def test_exclusive_end_time_and_offsets(self):
        self.assertEqual(m.reporting_day('2026-11-02T08:00:00+0000', SYNTHETIC_CONTRACT), '2026-11-01')
        self.assertEqual(m.reporting_day('2026-11-02T00:00:00-0800', SYNTHETIC_CONTRACT), '2026-11-01')
        self.assertEqual(m.reporting_day('2026-11-02T00:00:00Z', dict(SYNTHETIC_CONTRACT, timezone='UTC')), '2026-11-01')

    def test_publication_timestamp_uses_brasilia(self):
        self.assertEqual(m.publication_day('2026-11-02T01:00:00Z'), '2026-11-01')
        self.assertEqual(m.publication_day('2026-11-02T05:00:00+0200'), '2026-11-02')

    def test_invalid_and_naive_dates_rejected(self):
        for value in (None, '', '2026-02-30T00:00:00Z', '2026-11-02', '2026-11-02T08:00:00', '2026-11-02junk'):
            with self.subTest(value=value), self.assertRaises(m.CollectionError):
                m.publication_day(value)

    def test_invalid_date_does_not_take_another_days_value(self):
        self.assertEqual(self.values([series(end='2026-02-30T00:00:00Z')]), {})

    def test_identical_duplicate_daily_values_deduplicated(self):
        self.assertEqual(self.values([series(), series()]), {'2026-11-01': 7})

    def test_conflicting_duplicate_dates_omitted(self):
        self.assertEqual(self.values([series(value=7), series(value=8), series(value=7)]), {})

    def test_invalid_duplicate_does_not_restore_value(self):
        self.assertEqual(self.values([series(value=None), series(value=7)]), {})

    def test_unexpected_metric_or_period_is_not_accepted(self):
        for entry in (series(metric='impressions'), dict(series(), period='week'), {}, []):
            with self.subTest(entry=entry), self.assertRaises(m.CollectionError):
                self.values([entry])

    def test_total_value_zero_and_positive(self):
        client = Mock()
        for value in (0, 7):
            client.pages.return_value = [total(value=value)]
            self.assertEqual(m.insight_series(client, '1', 'accounts_engaged', '2026-11-01', '2026-11-02', 'instagram', True), {'2026-11-01': value})

    def test_total_value_invalid_or_empty_omitted(self):
        client = Mock()
        for value in (None, True, -1, 1.5, '3'):
            client.pages.return_value = [total(value=value)]
            self.assertEqual(m.insight_series(client, '1', 'accounts_engaged', '2026-11-01', '2026-11-02', 'instagram', True), {})
        for entries in ([], [{'name': 'accounts_engaged', 'period': 'day', 'total_value': {}}], [total(metric='other')], [total(), total()]):
            client.pages.return_value = entries
            self.assertEqual(m.insight_series(client, '1', 'accounts_engaged', '2026-11-01', '2026-11-02', 'instagram', True), {})

    def test_total_value_does_not_ignore_pending_pagination(self):
        client = self.client(max_pages=1)
        payload = {'data': [total()], 'paging': {'next': m.BASE_URL + '/1/insights?after=x'}}
        with patch.object(m.requests, 'get', return_value=response(payload)), self.assertRaisesRegex(m.CollectionError, 'page_limit'):
            m.insight_series(client, '1', 'accounts_engaged', '2026-11-01', '2026-11-02', 'instagram', True)

    def test_successful_pagination_collects_all_pages(self):
        client = self.client()
        with patch.object(m.requests, 'get', side_effect=[response({'data': [series()], 'paging': {'next': m.BASE_URL + '/1/insights?after=x'}}), response({'data': [series(end='2026-11-03T08:00:00Z', value=9)]})]):
            self.assertEqual(m.insight_series(client, '1', 'reach', '2026-11-01', '2026-11-03', 'instagram'), {'2026-11-01': 7, '2026-11-02': 9})

    def test_second_page_service_failure_fatal(self):
        client = self.client()
        with patch.object(m.requests, 'get', side_effect=[response({'data': [series()], 'paging': {'next': m.BASE_URL + '/1/insights?after=x'}}), response({'error': {'code': 2}}, 503)]), self.assertRaisesRegex(m.CollectionError, 'service'):
            m.insight_series(client, '1', 'reach', '2026-11-01', '2026-11-03', 'instagram')

    def test_second_page_invalid_structure_drops_entire_metric(self):
        client = self.client()
        with patch.object(m.requests, 'get', side_effect=[response({'data': [series()], 'paging': {'next': m.BASE_URL + '/1/insights?after=x'}}), response({})]):
            self.assertEqual(m.insight_series(client, '1', 'reach', '2026-11-01', '2026-11-03', 'instagram'), {})

    def test_cycle_fatal_and_bounded(self):
        client = self.client()
        payload = {'data': [], 'paging': {'next': m.BASE_URL + '/1/insights?after=x'}}
        with patch.object(m.requests, 'get', return_value=response(payload)) as call, self.assertRaisesRegex(m.CollectionError, 'pagination_cycle'):
            client.pages('1/insights', {})
        self.assertEqual(call.call_count, 2)

    def test_foreign_host_or_endpoint_rejected(self):
        for url in ('https://example.invalid/v21.0/1/insights?access_token=private', m.BASE_URL + '/2/insights', 'http://graph.facebook.com/v21.0/1/insights'):
            with self.subTest(url=url):
                client = self.client()
                with patch.object(m.requests, 'get', return_value=response({'data': [], 'paging': {'next': url}})) as call, self.assertRaises(m.CollectionError):
                    client.pages('1/insights', {})
                self.assertEqual(call.call_count, 1)

    def test_invalid_pagination_fatal(self):
        for paging in (None, [], 'next', {'next': None}, {'next': ''}, {'next': 4}):
            with self.subTest(paging=paging), patch.object(m.requests, 'get', return_value=response({'data': [], 'paging': paging})), self.assertRaises(m.CollectionError):
                self.client().pages('1/insights', {})

    def test_request_budget_counts_failed_attempts(self):
        client = self.client(max_requests=2)
        with patch.object(m.requests, 'get', return_value=response({'error': {'code': 2}}, 503)) as call:
            for _ in range(2):
                with self.assertRaisesRegex(m.CollectionError, 'service'):
                    client.get(m.BASE_URL + '/1', {})
            with self.assertRaisesRegex(m.CollectionError, 'request_limit'):
                client.get(m.BASE_URL + '/1', {})
        self.assertEqual(call.call_count, 2)

    def test_general_errors_classified_without_retry(self):
        for status, code, category in ((401, 190, 'authentication'), (400, 190, 'authentication'), (403, 10, 'permission'), (503, 2, 'service'), (429, 4, 'rate_limit')):
            with self.subTest(category=category), patch.object(m.requests, 'get', return_value=response({'error': {'code': code, 'error_subcode': 123, 'message': 'SECRET'}}, status)) as call:
                with self.assertRaises(m.CollectionError) as caught:
                    self.client().get(m.BASE_URL + '/1', {})
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(str(caught.exception), category)
                self.assertEqual(call.call_count, 1)

    def test_authentication_never_retried(self):
        with patch.object(m.requests, 'get', return_value=response({'error': {'code': 190}}, 400)) as call, self.assertRaises(m.CollectionError):
            self.client().get(m.BASE_URL + '/1', {})
        self.assertEqual(call.call_count, 1)

    def test_redirect_not_followed(self):
        with patch.object(m.requests, 'get', return_value=response({}, 302)) as call, self.assertRaisesRegex(m.CollectionError, 'redirect'):
            self.client().get(m.BASE_URL + '/1', {})
        self.assertFalse(call.call_args.kwargs['allow_redirects'])
        self.assertEqual(call.call_args.kwargs['timeout'], 20)

    def test_connection_errors_are_sanitized(self):
        with patch.object(m.requests, 'get', side_effect=m.requests.ConnectionError('https://private/?access_token=SECRET')), self.assertRaisesRegex(m.CollectionError, '^connection$'):
            self.client().get(m.BASE_URL + '/1', {})

    def test_malformed_json_not_success(self):
        obj = response({})
        obj.iter_content.return_value = [b'{SECRET']
        with patch.object(m.requests, 'get', return_value=obj), self.assertRaisesRegex(m.CollectionError, 'invalid_response'):
            self.client().get(m.BASE_URL + '/1', {})



    def test_unexpected_internal_error_not_silenced(self):
        client = Mock()
        client.pages.side_effect = TypeError('internal bug')
        with self.assertRaises(TypeError):
            m.insight_series(client, '1', 'reach', '2026-11-01', '2026-11-02', 'instagram')

    def test_listing_deduplicates_and_filters_local_dates(self):
        client = Mock()
        client.pages.return_value = [{'id': 'a', 'timestamp': '2026-11-02T01:00:00Z', 'media_type': 'VIDEO'}, {'id': 'a', 'timestamp': '2026-11-02T01:00:00Z', 'media_type': 'VIDEO'}, {'id': 'b', 'timestamp': '2026-11-03T01:00:00Z', 'media_type': 'IMAGE'}]
        self.assertEqual(m.publication_counts(client, '1', '2026-11-01', '2026-11-02', 'instagram'), {'2026-11-01': 1})

    def test_empty_listing_never_invents_zero(self):
        client = Mock()
        client.pages.return_value = []
        self.assertEqual(m.publication_counts(client, '1', '2026-11-01', '2026-11-02', 'facebook'), {})

    def test_invalid_listing_dates_types_and_ids_fatal(self):
        client = Mock()
        for entry in ({'id': 'a'}, {'id': 'a', 'created_time': 'bad'}, {'created_time': '2026-11-01T12:00:00Z'}):
            client.pages.return_value = [entry]
            with self.subTest(entry=entry), self.assertRaises(m.CollectionError):
                m.publication_counts(client, '1', '2026-11-01', '2026-11-02', 'facebook')

    def test_conflicting_duplicate_publication_dates_fatal(self):
        client = Mock()
        client.pages.return_value = [{'id': 'a', 'created_time': '2026-11-01T12:00:00Z'}, {'id': 'a', 'created_time': '2026-11-02T12:00:00Z'}]
        with self.assertRaisesRegex(m.CollectionError, 'conflicting_publication_date'):
            m.publication_counts(client, '1', '2026-11-01', '2026-11-03', 'facebook')

    def test_account_identification_required(self):
        client = Mock()
        for result in ({}, {'instagram_business_account': {}}, {'instagram_business_account': {'id': 123}}):
            client.get.return_value = result
            with self.assertRaisesRegex(m.CollectionError, 'invalid_account'):
                m.instagram_owner(client, '1')

    def test_preserve_historical_negative_unknown_and_conteudo(self):
        original = document()
        candidate, confirmed = m.apply_results(original, {'facebook': {'visualizacoes': {'2026-11-02': 0}}, 'instagram': {'interacoes': {}}}, True)
        self.assertEqual(confirmed, 1)
        self.assertEqual(candidate['series']['instagram'], original['series']['instagram'])
        self.assertEqual(candidate['unknown'], original['unknown'])
        self.assertEqual(candidate['conteudo'], original['conteudo'])
        self.assertNotIn('2026-11-02', original['series']['facebook']['visualizacoes'])

    def test_new_invalid_value_cannot_overwrite_valid_history(self):
        for value in (None, True, -1, 1.5):
            existing = {'2026-11-01': 8}
            with self.subTest(value=value), self.assertRaises(m.CollectionError):
                m.merge_series(existing, {'2026-11-01': value})
            self.assertEqual(existing, {'2026-11-01': 8})


    def test_no_confirmations_no_global_advance(self):
        original = document()
        candidate, confirmed = m.apply_results(original, {'facebook': {}, 'instagram': {}}, True)
        self.assertEqual(candidate, original)
        self.assertEqual(confirmed, 0)

    def test_old_backfill_does_not_advance_or_regress_global_date(self):
        original = document()
        for advance in (False, True):
            candidate, _ = m.apply_results(original, {'facebook': {'interacoes': {'2025-01-01': 0}}}, advance)
            self.assertEqual(candidate['atualizado'], original['atualizado'])

    def test_document_structure_and_duplicates_validated(self):
        for raw in (b'{}', b'{"atualizado":"","series":{"facebook":{},"instagram":{}},"series":{}}', b'{"x":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(m.CollectionError):
                m.validate_document(m.decode(raw))
        self.assertEqual(m.validate_document(document()), document())

    def test_invalid_existing_dates_or_types_block(self):
        for day, value in (('2026-02-30', 1), ('2026-11-01', None), ('2026-11-01', True), ('2026-11-01', 1.5)):
            doc = document();doc['series']['facebook']['visualizacoes'] = {day: value}
            with self.subTest(day=day, value=value), self.assertRaises(m.CollectionError):
                m.validate_document(doc)

    def test_logs_do_not_disclose_body_url_token_or_exception(self):
        m.warn('facebook', 'insights', 'page_media_view', 'permission', m.CollectionError('permission', 400, 10, 42))
        m.warn('https://private/?token=SECRET', 'SECRET BODY', 'bad/url', 'bad/category')
        out = self.sink.getvalue()
        self.assertIn('http=400 meta_code=10 meta_subcode=42', out)
        self.assertNotIn('SECRET', out)
        self.assertNotIn('https', out)

    def test_partial_http_and_empty_error_object_not_success(self):
        for payload, status in (({'data': [series()]}, 206), ({'data': [], 'error': {}}, 200)):
            with patch.object(m.requests, 'get', return_value=response(payload, status)), self.assertRaises(m.CollectionError):
                self.client().get(m.BASE_URL + '/1', {})

    def test_pagination_cannot_change_metric_or_window(self):
        for query, category in (('metric=other', 'pagination_filter_changed'), ('since=2026-01-01', 'pagination_window_changed'), ('after=x&after=y', 'invalid_pagination')):
            with self.subTest(query=query), patch.object(m.requests, 'get', return_value=response({'data': [], 'paging': {'next': m.BASE_URL + '/1/insights?' + query}})), self.assertRaisesRegex(m.CollectionError, category):
                self.client().pages('1/insights', {'metric': 'reach', 'since': '2026-11-01'})

    def test_total_value_requires_one_day_window(self):
        with self.assertRaisesRegex(m.CollectionError, 'date_interval'):
            m.insight_series(Mock(), '1', 'accounts_engaged', '2026-11-01', '2026-11-03', 'instagram', True)

    def test_retry_configuration_is_rejected(self):
        with self.assertRaisesRegex(m.CollectionError, 'limits'):
            self.client(attempts=3)

    def test_zero_or_negative_limits_refused(self):
        for kwargs in ({'max_requests': 0}, {'max_requests': -1}, {'max_pages': 0}, {'max_pages': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(m.CollectionError, 'limits'):
                self.client(**kwargs)




class StoreTests(unittest.TestCase):
    def setUp(self):
        gate = patch.object(m, 'require_publication_disabled')
        gate.start(); self.addCleanup(gate.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'metrics.json'
        self.raw = json.dumps(document()).encode()
        self.path.write_bytes(self.raw)

    def candidate(self, store):
        return m.apply_results(store.document, {'facebook': {'visualizacoes': {'2026-11-02': 4}}}, True)[0]

    def test_atomic_success_and_lock_cleanup(self):
        with m.Store(self.path) as store:
            candidate = self.candidate(store)
            self.assertTrue(store.save(candidate))
        self.assertEqual(json.loads(self.path.read_bytes()), candidate)
        self.assertFalse(store.lock.exists())
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_dry_run_no_file_writes_or_lock(self):
        with patch.object(m.os, 'open', side_effect=AssertionError('no lock write')), patch.object(m.tempfile, 'NamedTemporaryFile', side_effect=AssertionError('no tempfile')), patch.object(m.os, 'replace', side_effect=AssertionError('no replace')):
            with m.Store(self.path, True) as store:
                self.assertFalse(store.save(self.candidate(store)))
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_lock_contention_never_removes_active_lock(self):
        with m.Store(self.path) as first:
            nonce = first.lock.read_bytes()
            with self.assertRaisesRegex(m.CollectionError, 'locked'):
                with m.Store(self.path):
                    self.fail('second lock acquired')
            self.assertEqual(first.lock.read_bytes(), nonce)
        self.assertEqual(self.path.read_bytes(), self.raw)


    def test_foreign_replaced_lock_never_removed(self):
        with m.Store(self.path) as store:
            store.lock.write_bytes(b'foreign-lock')
            with self.assertRaisesRegex(m.CollectionError, 'lock_changed'):
                store.save(self.candidate(store))
        self.assertEqual(store.lock.read_bytes(), b'foreign-lock')
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_conflict_before_save_blocks_write(self):
        with m.Store(self.path) as store:
            self.path.write_bytes(b'concurrent')
            with self.assertRaisesRegex(m.CollectionError, 'concurrent_change'):
                store.save(self.candidate(store))
        self.assertEqual(self.path.read_bytes(), b'concurrent')

    def test_conflict_during_fsync_blocks_replace(self):
        with m.Store(self.path) as store:
            def conflict(_):self.path.write_bytes(b'concurrent')
            with patch.object(m.os, 'fsync', side_effect=conflict), self.assertRaisesRegex(m.CollectionError, 'concurrent_change'):
                store.save(self.candidate(store))
        self.assertEqual(self.path.read_bytes(), b'concurrent')
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_serialization_error_preserves_original(self):
        with m.Store(self.path) as store:
            candidate = self.candidate(store);candidate['unknown']['bad'] = object()
            with self.assertRaisesRegex(m.CollectionError, 'serialization'):
                store.save(candidate)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_replace_failure_preserves_original_and_cleans_temp(self):
        with m.Store(self.path) as store:
            with patch.object(m.os, 'replace', side_effect=OSError('secret path')), self.assertRaisesRegex(m.CollectionError, 'storage'):
                store.save(self.candidate(store))
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_fsync_failure_preserves_original(self):
        with m.Store(self.path) as store:
            with patch.object(m.os, 'fsync', side_effect=OSError('secret')), self.assertRaisesRegex(m.CollectionError, 'storage'):
                store.save(self.candidate(store))
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_invalid_document_releases_owned_lock(self):
        self.path.write_bytes(b'{}')
        with self.assertRaises(m.CollectionError):
            with m.Store(self.path):pass
        self.assertFalse(self.path.with_suffix('.json.lock').exists())
        self.assertEqual(self.path.read_bytes(), b'{}')





    def test_temp_creation_failure_preserves_original(self):
        with m.Store(self.path) as store:
            with patch.object(m.tempfile, 'NamedTemporaryFile', side_effect=OSError('secret')), self.assertRaisesRegex(m.CollectionError, 'storage'):
                store.save(self.candidate(store))
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_write_and_flush_failures_preserve_original(self):
        original = m.tempfile.NamedTemporaryFile
        for stage in ('write', 'flush'):
            def failing(*args, **kwargs):
                handle = original(*args, **kwargs)
                proxy = Mock(wraps=handle)
                proxy.name = handle.name
                proxy.__enter__ = Mock(return_value=proxy)
                proxy.__exit__ = Mock(side_effect=lambda *args: handle.close())
                getattr(proxy, stage).side_effect = OSError('secret')
                return proxy
            with self.subTest(stage=stage), m.Store(self.path) as store:
                with patch.object(m.tempfile, 'NamedTemporaryFile', side_effect=failing), self.assertRaisesRegex(m.CollectionError, 'storage'):
                    store.save(self.candidate(store))
            self.assertEqual(self.path.read_bytes(), self.raw)
            self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_lock_permission_failure_preserves_original(self):
        with patch.object(m.os, 'open', side_effect=PermissionError('secret')), self.assertRaises(PermissionError):
            with m.Store(self.path):pass
        self.assertEqual(self.path.read_bytes(), self.raw)



    def test_lock_cleanup_failure_is_reported_without_false_write_failure(self):
        sink=io.StringIO()
        with contextlib.redirect_stderr(sink):
            with m.Store(self.path) as store:
                candidate=self.candidate(store)
                self.assertTrue(store.save(candidate))
                original=Path.unlink
                def fail_lock(path,*args,**kwargs):
                    if path==store.lock:raise PermissionError('private path')
                    return original(path,*args,**kwargs)
                with patch.object(Path,'unlink',fail_lock):store.release()
        self.assertEqual(json.loads(self.path.read_bytes()),candidate)
        self.assertTrue(store.lock.exists())
        self.assertIn('lock_cleanup_failed',sink.getvalue())
        self.assertNotIn('private path',sink.getvalue())








class ContractRegressionTests(unittest.TestCase):
    def setUp(self):
        self.sink=io.StringIO()
        self.redirects=[contextlib.redirect_stdout(self.sink),contextlib.redirect_stderr(self.sink)]
        for redirect in self.redirects:redirect.__enter__()
        self.addCleanup(lambda:[redirect.__exit__(None,None,None) for redirect in reversed(self.redirects)])


    def test_reordered_fields_and_query_order_are_equivalent(self):
        next_url=m.BASE_URL+'/1/published_posts?after=x&fields=created_time,id'
        with patch.object(m.requests,'get',side_effect=[response({'data':[{'id':'a'}],'paging':{'next':next_url}}),response({'data':[{'id':'b'}]})]) as call:
            result=m.Client('synthetic').pages('1/published_posts',{'fields':'id,created_time'})
        self.assertEqual(result,[{'id':'a'},{'id':'b'}]);self.assertEqual(call.call_count,2)

    def test_reordered_metric_set_and_percent_encoding_equivalent(self):
        next_url=m.BASE_URL+'/1/insights?after=x&metric=reach%2Caccounts_engaged'
        with patch.object(m.requests,'get',side_effect=[response({'data':[{'id':'a'}],'paging':{'next':next_url}}),response({'data':[{'id':'b'}]})]):
            self.assertEqual(len(m.Client('synthetic').pages('1/insights',{'metric':'accounts_engaged,reach'})),2)

    def test_explicit_iso_instants_and_epoch_are_equivalent(self):
        from urllib.parse import urlencode
        start='2026-11-01T00:00:00Z';end='2026-11-02T00:00:00Z'
        query=urlencode({'since':int(m.stamp(start).timestamp()),'until':int(m.stamp(end).timestamp()),'after':'x'})
        with patch.object(m.requests,'get',side_effect=[response({'data':[{'id':'a'}],'paging':{'next':m.BASE_URL+'/1/insights?'+query}}),response({'data':[]})]) as call:
            result=m.Client('synthetic').pages('1/insights',{'since':start,'until':end})
        self.assertEqual(result,[{'id':'a'}]);self.assertEqual(call.call_count,2)

    def test_equivalent_offsets_in_query_boundaries(self):
        self.assertEqual(m.query_value('since','2026-11-02T01:00:00Z'),m.query_value('since','2026-11-01T22:00:00-0300'))

    def test_bare_date_to_epoch_not_guessed(self):
        url=m.BASE_URL+'/1/insights?since=1793491200&until=1793577600&after=x'
        with patch.object(m.requests,'get',return_value=response({'data':[series()],'paging':{'next':url}})),self.assertRaisesRegex(m.CollectionError,'window_equivalence_unverified'):
            m.Client('synthetic').pages('1/insights',{'since':'2026-11-01','until':'2026-11-02'})

    def test_omitted_filters_restored_on_next_cursor_request(self):
        with patch.object(m.requests,'get',side_effect=[response({'data':[{'id':'a'}],'paging':{'next':m.BASE_URL+'/1/media?after=x'}}),response({'data':[]})]) as call:
            m.Client('synthetic').pages('1/media',{'fields':'id,timestamp','since':'2026-11-01','until':'2026-11-02','limit':100})
        query=call.call_args_list[1].kwargs['params']
        self.assertEqual(query['since'],'2026-11-01');self.assertEqual(query['until'],'2026-11-02')
        self.assertEqual(query['fields'],'id,timestamp');self.assertNotIn('access_token',query)
        self.assertEqual(call.call_args_list[1].kwargs['headers'], {'Authorization': 'Bearer synthetic'})

    def test_new_filter_or_changed_fields_are_fatal(self):
        for query in ('fields=id,message','unrecognized_filter=x'):
            with self.subTest(query=query),patch.object(m.requests,'get',return_value=response({'data':[],'paging':{'next':m.BASE_URL+'/1/media?after=x&'+query}})),self.assertRaisesRegex(m.CollectionError,'pagination_filter_changed'):
                m.Client('synthetic').pages('1/media',{'fields':'id,timestamp'})

    def test_duplicate_page_even_with_different_cursor_and_order_rejected(self):
        first={'data':[{'id':'a'},{'id':'b'}],'paging':{'next':m.BASE_URL+'/1/media?after=x'}}
        second={'data':[{'id':'b'},{'id':'a'}]}
        with patch.object(m.requests,'get',side_effect=[response(first),response(second)]),self.assertRaisesRegex(m.CollectionError,'duplicate_page'):
            m.Client('synthetic').pages('1/media',{})

    def test_equivalent_representations_cannot_hide_url_cycle(self):
        first={'data':[],'paging':{'next':m.BASE_URL+'/1/media?fields=timestamp,id'}}
        with patch.object(m.requests,'get',return_value=response(first)) as call,self.assertRaisesRegex(m.CollectionError,'pagination_cycle'):
            m.Client('synthetic').pages('1/media',{'fields':'id,timestamp'})
        self.assertEqual(call.call_count,1)

    def test_total_value_time_navigation_stops_after_complete_window(self):
        first={'data':[total(value=0)],'paging':{'next':m.BASE_URL+'/1/insights?metric=accounts_engaged&period=day&metric_type=total_value&since=2026-11-02&until=2026-11-03'}}
        with patch.object(m.requests,'get',return_value=response(first)) as call:
            result=m.insight_series(m.Client('synthetic'),'1','accounts_engaged','2026-11-01','2026-11-02','instagram',True)
        self.assertEqual(result,{'2026-11-01':0});self.assertEqual(call.call_count,1)

    def test_native_time_navigation_requires_all_dates_and_audited_contract(self):
        first={'data':[series(),series(end='2026-11-03T08:00:00Z',value=8)],'paging':{'next':m.BASE_URL+'/1/insights?metric=reach&period=day&since=2026-11-03&until=2026-11-04'}}
        with patch.dict(m.REPORTING_CONTRACTS,synthetic_contracts(m)),patch.object(m.requests,'get',return_value=response(first)) as call:
            result=m.insight_series(m.Client('synthetic'),'1','reach','2026-11-01','2026-11-03','instagram')
        self.assertEqual(result,{'2026-11-01':7,'2026-11-02':8});self.assertEqual(call.call_count,1)
        first['data']=[series()]
        with patch.dict(m.REPORTING_CONTRACTS,synthetic_contracts(m)),patch.object(m.requests,'get',return_value=response(first)):
            self.assertEqual(m.insight_series(m.Client('synthetic'),'1','reach','2026-11-01','2026-11-03','instagram'),{})
        self.assertIn('insights_navigation_unresolved',self.sink.getvalue())

    def test_navigation_without_date_contract_preserves_history(self):
        first={'data':[series()],'paging':{'next':m.BASE_URL+'/1/insights?metric=reach&since=2026-11-02&until=2026-11-03'}}
        with patch.object(m.requests,'get',return_value=response(first)):
            self.assertEqual(m.insight_series(m.Client('synthetic'),'1','reach','2026-11-01','2026-11-02','instagram'),{})
        self.assertIn('date_contract_unverified',self.sink.getvalue())

    def test_window_change_with_cursor_is_not_time_navigation(self):
        first={'data':[total()],'paging':{'next':m.BASE_URL+'/1/insights?since=2026-11-02&until=2026-11-03&after=x'}}
        with patch.object(m.requests,'get',return_value=response(first)),self.assertRaisesRegex(m.CollectionError,'pagination_window_changed'):
            m.insight_series(m.Client('synthetic'),'1','accounts_engaged','2026-11-01','2026-11-02','instagram',True)

    def test_overlapping_window_cannot_be_followed(self):
        first={'data':[series()],'paging':{'next':m.BASE_URL+'/1/insights?since=2026-11-02&until=2026-11-04'}}
        with patch.object(m.requests,'get',return_value=response(first)),self.assertRaisesRegex(m.CollectionError,'pagination_window_changed'):
            m.insight_series(m.Client('synthetic'),'1','reach','2026-11-01','2026-11-03','instagram')

    def test_original_equivalent_instants_now_have_same_day_with_contract(self):
        contract=dict(SYNTHETIC_CONTRACT,timezone='Etc/GMT+1')
        self.assertEqual(m.reporting_day('2026-11-02T01:00:00Z',contract),'2026-11-01')
        self.assertEqual(m.reporting_day('2026-11-01T22:00:00-0300',contract),'2026-11-01')

    def test_original_equivalent_instants_rejected_without_contract(self):
        for value in ('2026-11-02T01:00:00Z','2026-11-01T22:00:00-0300'):
            with self.subTest(value=value),self.assertRaisesRegex(m.CollectionError,'date_contract_unverified'):
                m.reporting_day(value)

    def test_brasilia_midnight_month_year_and_leap_boundaries(self):
        contract=dict(SYNTHETIC_CONTRACT,timezone='America/Sao_Paulo')
        for utc,brt,expected in [('2027-01-01T03:00:00Z','2027-01-01T00:00:00-0300','2026-12-31'),('2026-03-01T03:00:00Z','2026-03-01T00:00:00-0300','2026-02-28'),('2024-03-01T03:00:00Z','2024-03-01T00:00:00-0300','2024-02-29')]:
            with self.subTest(expected=expected):
                self.assertEqual(m.reporting_day(utc,contract),expected)
                self.assertEqual(m.reporting_day(brt,contract),expected)

    def test_nonboundary_instant_never_subtracts_a_day(self):
        with self.assertRaisesRegex(m.CollectionError,'invalid_date'):
            m.reporting_day('2026-11-02T01:00:00Z',dict(SYNTHETIC_CONTRACT,timezone='America/Sao_Paulo'))

    def test_invalid_contract_never_maps_native_day(self):
        for contract in ({},{'kind':'exclusive_end','timezone':'UTC'},dict(SYNTHETIC_CONTRACT,timezone='Not/AZone'),dict(SYNTHETIC_CONTRACT,kind='unknown')):
            with self.subTest(contract=contract),self.assertRaisesRegex(m.CollectionError,'date_contract_unverified'):
                m.reporting_day('2026-11-02T08:00:00Z',contract)

    def test_observed_counts_lower_higher_equal_and_absent_preserve_total(self):
        for value in (1,9,12):
            previous={'2026-11-01':9}
            incoming=m.PublicationObservations({'2026-11-01':value},network='facebook',account='synthetic')
            with self.subTest(value=value):
                self.assertEqual(m.merge_series(previous,incoming,metric='publicacoes',network='facebook'),0)
                self.assertEqual(previous,{'2026-11-01':9})
        previous={'2026-11-01':9}
        self.assertEqual(m.merge_series(previous,{},metric='publicacoes',network='facebook'),0)
        candidate,confirmed=m.apply_results(document(),{'facebook':{'publicacoes':{'2026-11-01':1}}})
        self.assertNotIn('publicacoes',candidate['series']['facebook']);self.assertEqual(confirmed,0)

    def test_apply_results_preserves_divergent_historical_count(self):
        doc=document();doc['series']['facebook']['publicacoes']={'2026-11-01':9}
        for value in (1,12):
            candidate,confirmed=m.apply_results(doc,{'facebook':{'publicacoes':{'2026-11-01':value}}},True)
            self.assertEqual(candidate,doc);self.assertEqual(confirmed,0)
        self.assertIn('observed_count_divergence_preserved',self.sink.getvalue())


    def test_expired_or_wrong_scope_inventory_proof_rejected(self):
        account=m.hashlib.sha256(b'1').hexdigest()
        policy={'policy':'complete_daily_inventory_v1','reference':'synthetic','start':'2026-11-02','end':'2026-11-03'}
        observed=m.PublicationObservations({'2026-11-01':1},network='facebook',account=account,evidence={'2026-11-01':('facebook',account,'synthetic','complete_daily_inventory_v1')})
        with patch.dict(m.PUBLICATION_COVERAGE_CONTRACTS,{('facebook',account):policy}):
            previous={'2026-11-01':9};self.assertEqual(m.merge_series(previous,observed,metric='publicacoes',network='facebook'),0)
        self.assertEqual(previous,{'2026-11-01':9})



    def test_incomplete_listing_cannot_issue_coverage_proof(self):
        client=Mock();client.pages.side_effect=m.CollectionError('page_limit')
        with self.assertRaisesRegex(m.CollectionError,'page_limit'):
            m.publication_counts(client,'1','2026-11-01','2026-11-02','facebook')









if __name__ == '__main__':
    unittest.main()
