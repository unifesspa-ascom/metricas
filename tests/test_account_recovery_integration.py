"""New integration contracts for the selectively recovered account layer."""
import contextlib
from copy import deepcopy
import importlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import account_metrics_safety as safe
import facebook_account_metrics as fb
import update_metrics as update
import backfill_metrics as backfill

START, END = '2026-11-12', '2026-11-13'


def document():
    return {'series': {'facebook': {'visualizacoes': {'2026-11-01': 8}},
                       'instagram': {'stories': {'2026-11-01': 5}}},
            'atualizado': '2026-11-01', 'conteudo': {'manual': [99]}, 'custom': {'keep': True}}


def response(body):
    result = Mock(status_code=200)
    result.iter_content.return_value = [json.dumps(body).encode()]
    return result


class RecoveryIntegration(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))

    def test_production_gates_precede_disk_network_and_credentials(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'read_bytes') as read, \
                patch.object(safe.os, 'open') as opened, patch.object(safe.requests, 'get') as get:
            importlib.reload(update)
            importlib.reload(backfill)
            for operation in (lambda: safe.Store('missing.json').__enter__(),
                              lambda: safe.Store('missing.json').save({}),
                              lambda: update.update_json({}, {})):
                with self.assertRaisesRegex(fb.MetricError, 'account_publication_suspended'):
                    operation()
            for module, args in ((update, []), (backfill, []), (backfill, ['--dry-run'])):
                with patch.object(sys, 'argv', ['collector'] + args), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        module.main()
            read.assert_not_called(); opened.assert_not_called(); get.assert_not_called()

    def test_pending_contracts_are_empty_and_facebook_is_unchanged(self):
        self.assertEqual(safe.REPORTING_CONTRACTS, {})
        self.assertEqual(safe.PUBLICATION_COVERAGE_CONTRACTS, {})
        self.assertIsNone(fb.APPROVED_DAY_ZONE)
        self.assertFalse(fb.PUBLICATION_ENABLED)
        self.assertIs(update.FacebookClient, fb.Client)
        self.assertIs(backfill.FacebookClient, fb.Client)
        self.assertIs(safe.FB_METRICS, fb.METRICS)

    def test_instagram_wire_zero_missing_dates_and_no_inventory(self):
        bodies = [
            {'instagram_business_account': {'id': '456'}},
            {'data': [{'name': 'reach', 'period': 'day', 'values': [
                {'value': 9, 'end_time': '2026-11-13T00:00:00-0300'}]}]},
            {'data': [{'name': 'accounts_engaged', 'period': 'day', 'total_value': {'value': 0}}]},
            {'data': []},
        ]
        with patch.dict(os.environ, {'FB_PAGE_ACCESS_TOKEN': 'FAKE_TOKEN'}), \
                patch.multiple(update, PAGE_ID='123', yesterday=START, since=START, until_=END), \
                patch.object(safe.requests, 'get', side_effect=[response(body) for body in bodies]) as get, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(update.fetch_instagram_metrics(), {'interacoes': 0})
        self.assertEqual(get.call_count, 4)
        for call in get.call_args_list:
            self.assertNotIn('FAKE_TOKEN', call.args[0])
            self.assertNotIn('access_token', call.kwargs['params'])
            self.assertEqual(call.kwargs['headers'], {'Authorization': 'Bearer FAKE_TOKEN'})
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertNotIn('/media', call.args[0])
            self.assertNotIn('/stories', call.args[0])

    def test_instagram_total_shape_name_period_and_integer_validation(self):
        baseline = {'name': 'accounts_engaged', 'period': 'day', 'total_value': {'value': 0}}
        variants = [dict(baseline, name='other'), dict(baseline, period='week'),
                    dict(baseline, total_value={}), dict(baseline, total_value={'value': False}),
                    dict(baseline, total_value={'value': 9007199254740992})]
        for entry in variants:
            client = Mock(); client.pages.return_value = [entry]
            with self.subTest(entry=entry), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(safe.insight_series(client, '456', 'accounts_engaged', START, END,
                                                    'instagram', total=True), {})

    def test_duplicate_api_keys_nonfinite_values_and_oversized_bodies_rejected(self):
        for raw in (b'{"data":[],"data":[]}', b'{"custom":NaN}', b'{"custom":1e999}', b'x' * (safe.MAX_BODY + 1)):
            res = response({}); res.iter_content.return_value = [raw]
            with self.subTest(raw_length=len(raw)), patch.object(safe.requests, 'get', return_value=res):
                with self.assertRaises(safe.CollectionError):
                    safe.Client('FAKE_TOKEN').get(safe.BASE_URL + '/123/insights', {})
                res.close.assert_called_once()

    def test_limits_and_bad_destinations_make_no_request(self):
        with patch.object(safe.requests, 'get') as get:
            for limits in ({'max_requests': 101}, {'max_pages': 11}, {'attempts': 2}):
                with self.assertRaises(safe.CollectionError):
                    safe.Client('FAKE_TOKEN', **limits)
            for destination in ('http://graph.facebook.com/v21.0/1',
                                'https://graph.facebook.com.evil.invalid/v21.0/1',
                                safe.BASE_URL + '/1?access_token=foreign', safe.BASE_URL + '/1#fragment'):
                with self.assertRaises(safe.CollectionError):
                    safe.Client('FAKE_TOKEN').get(destination, {})
            get.assert_not_called()

    def test_candidate_conflict_is_atomic_zero_is_valid_and_manual_fields_survive(self):
        original = document(); before = deepcopy(original)
        original['series']['custom_network'] = {'opaque': [1, None]}
        before = deepcopy(original)
        with self.assertRaisesRegex(safe.CollectionError, 'history_conflict'):
            safe.apply_results(original, {'facebook': {'cliques': {START: 0},
                'visualizacoes': {'2026-11-01': 0}}})
        self.assertEqual(original, before)
        candidate, count = safe.apply_results(original, {'instagram': {'interacoes': {START: 0}}})
        self.assertEqual(count, 1)
        self.assertEqual(candidate['series']['instagram']['interacoes'], {START: 0})
        for field in ('atualizado', 'conteudo', 'custom'):
            self.assertEqual(candidate[field], before[field])
        safe.preserve_history(before, candidate)

    def test_dry_run_checks_history_without_writes_or_recalculation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'metricas.json'); raw = json.dumps(document()).encode()
            path.write_bytes(raw)
            real_store = safe.Store
            with patch.multiple(update, yesterday=START), \
                    patch.object(safe, 'Store', side_effect=lambda **kw: real_store(path, **kw)), \
                    contextlib.redirect_stdout(io.StringIO()):
                candidate = update.update_json({}, {'interacoes': 0}, dry_run=True)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            self.assertEqual(candidate['conteudo'], document()['conteudo'])

    def test_storage_independently_rejects_historical_replacement_or_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'metricas.json'); raw = json.dumps(document()).encode()
            path.write_bytes(raw)
            for change in ('value', 'delete', 'manual'):
                with safe.Store(path, dry_run=True) as store:
                    candidate = deepcopy(store.document)
                    if change == 'value':
                        candidate['series']['facebook']['visualizacoes']['2026-11-01'] = 0
                    elif change == 'delete':
                        del candidate['series']['facebook']['visualizacoes']
                    else:
                        candidate['conteudo'] = {}
                    with self.assertRaisesRegex(safe.CollectionError, 'history_conflict'):
                        store.save(candidate)
                self.assertEqual(path.read_bytes(), raw)

    def test_real_archival_document_contract_readonly(self):
        path = Path(__file__).resolve().parents[1] / 'dados/metricas.json'
        raw = path.read_bytes()
        original = safe.validate_document(safe.decode(raw))
        candidate, count = safe.apply_results(original, {'facebook': {}, 'instagram': {}})
        self.assertEqual(candidate, original)
        self.assertEqual(count, 0)
        self.assertEqual(path.read_bytes(), raw)

    def test_even_audited_inventory_cannot_overwrite_history_or_partially_merge(self):
        account = safe.hashlib.sha256(b'123').hexdigest()
        policy = {'policy': 'complete_daily_inventory_v1', 'reference': 'OFFLINE_FIXTURE_ONLY',
                  'start': '2026-11-01', 'end': END}
        old = {'2026-11-01': 9}
        evidence = {day: ('facebook', account, policy['reference'], policy['policy'])
                    for day in (START, '2026-11-01')}
        observed = safe.PublicationObservations({START: 0, '2026-11-01': 1},
            network='facebook', account=account, evidence=evidence)
        with patch.dict(safe.PUBLICATION_COVERAGE_CONTRACTS, {('facebook', account): policy}):
            with self.assertRaisesRegex(safe.CollectionError, 'history_conflict'):
                safe.merge_series(old, observed, metric='publicacoes', network='facebook')
            self.assertEqual(old, {'2026-11-01': 9})

    def test_page_ceiling_never_returns_partial_metric(self):
        body = {'data': [{'name': 'accounts_engaged', 'period': 'day',
                          'total_value': {'value': 0}}],
                'paging': {'next': safe.BASE_URL + '/456/insights?after=x'}}
        with patch.object(safe.requests, 'get', return_value=response(body)) as get:
            with self.assertRaisesRegex(safe.CollectionError, 'page_limit'):
                safe.insight_series(safe.Client('FAKE_TOKEN', max_pages=1), '456',
                                    'accounts_engaged', START, END, 'instagram', total=True)
            self.assertEqual(get.call_count, 1)

    def test_lock_excludes_another_process_with_gate_mocked_only_in_fixture(self):
        code = '''import account_metrics_safety as m, sys
m.require_publication_disabled = lambda: None  # Offline fixture only.
try:
    with m.Store(sys.argv[1]):
        raise SystemExit(9)
except m.CollectionError as error:
    raise SystemExit(0 if error.category == 'locked' else 8)
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'metricas.json'); path.write_text(json.dumps(document()), encoding='utf-8')
            with patch.object(safe, 'require_publication_disabled'), safe.Store(path) as store:
                nonce = store.lock.read_bytes()
                env = dict(os.environ)
                scripts = str(Path(__file__).resolve().parents[1] / 'scripts')
                env['PYTHONPATH'] = scripts + os.pathsep + env.get('PYTHONPATH', '')
                result = subprocess.run([sys.executable, '-B', '-c', code, str(path)],
                                        env=env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                self.assertEqual(store.lock.read_bytes(), nonce)


if __name__ == '__main__':
    unittest.main()
