"""Offline contracts for Facebook Insights. Network access is forbidden in all tests."""
import contextlib
from copy import deepcopy
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import facebook_account_metrics as fb

START, END = '2026-11-12', '2026-11-13'
METRIC = 'page_media_view'
SECRET = 'FAKE_TOKEN https://example.invalid/?access_token=SECRET raw_id cursor'


def payload(value=0, stamp='2026-11-13T00:00:00-0300', metric=METRIC):
    return {'data': [{'name': metric, 'period': 'day',
                      'values': [{'value': value, 'end_time': stamp}]}]}


def load_collector(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {'FB_PAGE_ACCESS_TOKEN': 'FAKE_TOKEN'}):
        spec.loader.exec_module(module)
    return module


class FacebookContracts(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden')))

    def test_zero_and_positive_values_survive(self):
        for value in (0, 1, 12345):
            obs = fb.parse_observations(payload(value), METRIC)
            self.assertEqual(fb.map_days(obs, START, END, 'America/Sao_Paulo'), {START: value})

    def test_bad_values_reject_entire_response(self):
        for bad in (True, False, None, -1, 0.0, 1.5, '0', {}, [], 9007199254740992):
            data = payload()
            data['data'][0]['values'].append({'value': bad, 'end_time': '2026-11-14T00:00:00-0300'})
            with self.subTest(value=bad), self.assertRaises(fb.MetricError):
                fb.parse_observations(data, METRIC)
        data = payload()
        del data['data'][0]['values'][0]['value']
        with self.assertRaises(fb.MetricError):
            fb.parse_observations(data, METRIC)

    def test_absent_is_not_zero(self):
        self.assertEqual(fb.parse_observations({'data': []}, METRIC), [])
        data = payload(); data['data'][0]['values'] = []
        self.assertEqual(fb.parse_observations(data, METRIC), [])
        for bad in ({}, {'data': None}, {'data': {}}, {'error': {}}):
            with self.assertRaises(fb.MetricError):
                fb.parse_observations(bad, METRIC)

    def test_wrong_name_period_duplicate_metrics_and_pagination_rejected(self):
        variants = []
        data=payload(); data['data'][0]['name']='page_impressions'; variants.append(data)
        data=payload(); data['data'][0]['period']='week'; variants.append(data)
        data=payload(); data['data'].append(deepcopy(data['data'][0])); variants.append(data)
        data=payload(); data['paging']={'next': SECRET}; variants.append(data)
        for data in variants:
            with self.assertRaises(fb.MetricError): fb.parse_observations(data, METRIC)

    def test_timestamp_must_be_real_aware_and_unique(self):
        for stamp in ('2026-11-13', '2026-11-13T00:00:00', '2026-02-30T00:00:00Z',
                      '2026-11-13T00:00:00Z\n', None, SECRET):
            with self.assertRaises(fb.MetricError): fb.parse_observations(payload(stamp=stamp), METRIC)
        data=payload(); data['data'][0]['values'].append({'value':0,'end_time':'2026-11-13T03:00:00Z'})
        with self.assertRaisesRegex(fb.MetricError, 'duplicate_end_time'):
            fb.parse_observations(data, METRIC)

    def test_no_implicit_date_policy_or_last_value_fallback(self):
        obs=fb.parse_observations(payload(), METRIC)
        with self.assertRaisesRegex(fb.MetricError, 'date_policy_unreviewed'):
            fb.map_days(obs, START, END, None)
        for stamp in ('2026-11-12T00:00:00-0300','2026-11-14T00:00:00-0300'):
            with self.assertRaisesRegex(fb.MetricError, 'out_of_window'):
                fb.map_days(fb.parse_observations(payload(stamp=stamp),METRIC), START,END,'America/Sao_Paulo')
        with self.assertRaisesRegex(fb.MetricError, 'unexpected_day_boundary'):
            fb.map_days(obs, START, END, 'UTC')

    def test_date_uses_calendar_day_end_not_end_date_or_fixed_utc_offset(self):
        # Candidate policies tested synthetically; neither zone is production-approved.
        for stamp, start, end in [('2026-03-09T00:00:00-0700','2026-03-08','2026-03-09'),
                                  ('2026-11-02T00:00:00-0800','2026-11-01','2026-11-02'),
                                  ('2026-01-01T00:00:00-0800','2025-12-31','2026-01-01')]:
            series=fb.map_days(fb.parse_observations(payload(stamp=stamp),METRIC), start,end,'America/Los_Angeles')
            self.assertEqual(series,{start:0})

    def test_transport_fixed_host_query_header_and_six_attempt_budget(self):
        client=fb.Client('FAKE_TOKEN'); client.opener=Mock()
        response=Mock(status=200)
        response.read.return_value=json.dumps(payload()).encode()
        response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        client.opener.open.return_value=response
        for metric in fb.METRICS.values():
            client.get('123',metric,START,END)
            req=client.opener.open.call_args.args[0]
            parts=urllib.parse.urlsplit(req.full_url)
            self.assertEqual((parts.scheme,parts.netloc,parts.path),('https','graph.facebook.com','/v21.0/123/insights'))
            self.assertEqual(urllib.parse.parse_qs(parts.query),{'metric':[metric],'period':['day'],'since':[START],'until':[END]})
            self.assertEqual(req.get_method(),'GET')
            self.assertEqual(req.get_header('Authorization'),'Bearer FAKE_TOKEN')
            self.assertNotIn('FAKE_TOKEN',req.full_url)
            self.assertEqual(client.opener.open.call_args.kwargs,{'timeout':20})
        self.assertEqual(client.count,6)
        with self.assertRaisesRegex(fb.MetricError,'request_budget_exhausted'):
            client.get('123',METRIC,START,END)
        self.assertEqual(client.opener.open.call_count,6)
        response.read.assert_called_with(fb.MAX_BODY+1)

    def test_failures_also_consume_budget_and_no_retry_or_raw_error(self):
        client=fb.Client('FAKE_TOKEN'); client.opener=Mock()
        client.opener.open.side_effect=RuntimeError(SECRET)
        for _ in range(6):
            with self.assertRaisesRegex(fb.MetricError,'^transport_or_response_error$'):
                client.get('123',METRIC,START,END)
        self.assertEqual(client.opener.open.call_count,6)
        with self.assertRaisesRegex(fb.MetricError,'request_budget_exhausted'):
            client.get('123',METRIC,START,END)

    def test_http_errors_never_read_or_echo_raw_bodies(self):
        for code in (301,302,303,307,308,400,403,429,500):
            client=fb.Client('FAKE_TOKEN'); client.opener=Mock(); body=Mock()
            client.opener.open.side_effect=urllib.error.HTTPError(SECRET,code,SECRET,{},body)
            expected='redirect_blocked' if code<400 else 'http_error'
            with self.assertRaisesRegex(fb.MetricError,'^'+expected+'$'):
                client.get('123',METRIC,START,END)
            body.read.assert_not_called()
            self.assertEqual(client.opener.open.call_count,1)
        self.assertIsNone(fb.NoRedirect().redirect_request(None,None,302,SECRET,{},SECRET))

    def test_transport_bounds_malformed_json_and_error_envelopes(self):
        for raw,category in [(b'x'*(fb.MAX_BODY+1),'response_too_large'),(b'{','transport_or_response_error'),
                             (b'[]','invalid_response'),(json.dumps({'error':SECRET}).encode(),'api_error')]:
            client=fb.Client('FAKE_TOKEN'); client.opener=Mock()
            r=Mock(status=200); r.read.return_value=raw
            r.__enter__=Mock(return_value=r); r.__exit__=Mock(return_value=False)
            client.opener.open.return_value=r
            with self.assertRaisesRegex(fb.MetricError,'^'+category+'$'):
                client.get('123',METRIC,START,END)

    def test_unknown_paths_metrics_and_large_window_make_no_requests(self):
        c=fb.Client('FAKE_TOKEN'); c.opener=Mock()
        for page,metric,start,end in [('https://evil.invalid',METRIC,START,END),('123','page_impressions',START,END),
                                      ('123',METRIC,'2026-01-01',END),('123',METRIC,END,START)]:
            with self.assertRaises(fb.MetricError): c.get(page,metric,start,end)
        c.opener.open.assert_not_called()

    def test_metric_failures_do_not_switch_to_fallback_or_zero(self):
        c=Mock(); c.count=0
        def get(page,metric,since,until):
            if metric!=METRIC: raise fb.MetricError('http_error')
            return payload(0)
        c.get.side_effect=get
        series,statuses=fb.collect_series(c,'123',START,END,day_zone='America/Sao_Paulo')
        self.assertEqual(series['visualizacoes'],{START:0})
        self.assertTrue(all(not values for key,values in series.items() if key!='visualizacoes'))
        self.assertEqual(c.get.call_count,6)
        self.assertEqual({call.args[1] for call in c.get.call_args_list},set(fb.METRICS.values()))
        series,statuses=fb.collect_series(c,'123',START,END)
        self.assertEqual(series['visualizacoes'],{})
        self.assertEqual(statuses['visualizacoes'],'date_policy_unreviewed')

    def test_append_preserves_unknown_history_and_zero_without_mutation(self):
        old={'visualizacoes':{'2020-01-01':10},'custom':{'keep':[1,None]},'cliques':{START:0}}
        before=deepcopy(old)
        candidate=fb.append_candidate(old,{'visualizacoes':{START:0},'cliques':{}},since=START,until=END)
        self.assertEqual(old,before)
        self.assertEqual(candidate['visualizacoes'],{'2020-01-01':10,START:0})
        self.assertEqual(candidate['cliques'],{START:0})
        self.assertEqual(candidate['custom'],old['custom'])
        self.assertEqual(fb.append_candidate(old,{},since=START,until=END),old)
        with self.assertRaisesRegex(fb.MetricError,'history_conflict'):
            fb.append_candidate(old,{'cliques':{START:1}},since=START,until=END)
        self.assertEqual(old,before)

    def test_append_is_atomic_and_rejects_bad_values_and_historical_dates(self):
        old={}; snapshot=deepcopy(old)
        for bad in ({'visualizacoes':{START:0},'cliques':{START:False}},
                    {'visualizacoes':{'2020-01-01':1}}, {'unknown':{START:2}}):
            with self.assertRaises(fb.MetricError): fb.append_candidate(old,bad,since=START,until=END)
            self.assertEqual(old,snapshot)

    def test_both_collectors_use_shared_contract_without_post_story_collection(self):
        for name in ('update_metrics','backfill_metrics'):
            m=load_collector(name)
            with patch.object(m,'FacebookClient') as factory, contextlib.redirect_stdout(io.StringIO()):
                factory.return_value.get.side_effect=lambda page,metric,start,end: payload(0,metric=metric)
                if name=='update_metrics':
                    m.since,m.until_,m.yesterday=START,END,START
                    self.assertEqual(m.fetch_facebook_metrics(day_zone='America/Sao_Paulo'),{k:0 for k in fb.METRICS})
                else:
                    self.assertEqual(m.fetch_fb_insights_month(START,END,day_zone='America/Sao_Paulo'),{k:{START:0} for k in fb.METRICS})
                self.assertEqual(factory.return_value.get.call_count,6)

    def test_account_writes_and_backfill_are_unconditionally_blocked(self):
        update=load_collector('update_metrics'); backfill=load_collector('backfill_metrics')
        with patch('builtins.open') as opened:
            with self.assertRaisesRegex(fb.MetricError,'account_publication_suspended'):
                update.update_json({'visualizacoes':0},{})
            opened.assert_not_called()
        for module,args in ((update,[]),(backfill,['--dry-run','--year','2025'])):
            with patch.object(module,'FacebookClient') as client, patch.object(module,'api_get') as get, \
                 patch('sys.argv',['collector']+args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc: module.main()
                self.assertEqual(exc.exception.code,2)
                client.assert_not_called(); get.assert_not_called()

    def test_no_legacy_route_or_fallback_remains_in_account_sources(self):
        for name in ('update_metrics.py','backfill_metrics.py'):
            text=(ROOT/'scripts'/name).read_text()
            self.assertNotIn('/insights/{name}/day',text)
            self.assertNotIn('FB_METRICS_FALLBACK',text)
        self.assertIsNone(fb.APPROVED_DAY_ZONE)
        self.assertFalse(fb.PUBLICATION_ENABLED)


if __name__=='__main__': unittest.main()
