import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('diagnostic', ROOT / 'scripts/diagnose_facebook_errors.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
SECRET = 'TOKEN_SECRET id_123 cursor_456 https://example.invalid/?access_token=SECRET'
SINCE, UNTIL = '2026-11-12', '2026-11-13'


def response(raw, status=200):
    r = Mock(status=status)
    r.read.return_value = raw
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    return r


class Protections(unittest.TestCase):
    def client(self):
        c = d.Client('TOKEN_SECRET')
        c.opener = Mock()
        return c

    def test_two_fixed_gets_only_change_metric_representation(self):
        c = self.client()
        seen = []
        def answer(req, timeout):
            seen.append(urllib.parse.urlsplit(req.full_url))
            self.assertEqual(req.get_method(), 'GET')
            self.assertEqual(seen[-1].netloc, 'graph.facebook.com')
            self.assertEqual(seen[-1].scheme, 'https')
            self.assertNotIn('TOKEN_SECRET', req.full_url)
            self.assertEqual(req.get_header('Authorization'), 'Bearer TOKEN_SECRET')
            self.assertEqual(timeout, 20)
            return response(json.dumps({'data': [{'value': SECRET}], 'paging': {'next': SECRET}}).encode())
        c.opener.open.side_effect = answer
        report = d.diagnose(c, '123', SINCE, UNTIL)
        self.assertEqual((report['requests'], report['limit'], len(report['results'])), (2, 2, 2))
        self.assertEqual(seen[0].path, '/v21.0/123/insights/page_media_view/day')
        self.assertEqual(seen[1].path, '/v21.0/123/insights')
        a, b = [urllib.parse.parse_qs(x.query) for x in seen]
        self.assertEqual(b.pop('metric'), ['page_media_view'])
        self.assertEqual(a, b)
        self.assertEqual(a, {'period': ['day'], 'since': [SINCE], 'until': [UNTIL]})
        for sensitive in (SECRET, 'TOKEN_SECRET', '123', '456', 'paging', 'next'):
            self.assertNotIn(sensitive, json.dumps(report))

    def test_budget_including_failed_attempts(self):
        c = self.client()
        c.opener.open.side_effect = RuntimeError(SECRET)
        d.diagnose(c, '123', SINCE, UNTIL)
        with self.assertRaisesRegex(ValueError, 'request_budget_exhausted'):
            c.get('123', 'legacy_path', SINCE, UNTIL)
        self.assertEqual(c.opener.open.call_count, 2)

    def test_http_status_and_only_numeric_error_fields(self):
        for status in (400, 401, 403, 404, 429, 500):
            c = self.client()
            body = io.BytesIO(json.dumps({'error': {'code': 100, 'error_subcode': 1504028,
                'message': SECRET, 'type': SECRET, 'fbtrace_id': SECRET, 'error_data': SECRET},
                'id': SECRET}).encode())
            c.opener.open.side_effect = urllib.error.HTTPError(SECRET, status, SECRET, {}, body)
            out = c.get('123', 'legacy_path', SINCE, UNTIL)
            self.assertEqual(out, {'probe': 'legacy_path', 'status': 'http_error',
                'http_status': status, 'meta_code': 100, 'meta_subcode': 1504028})
            self.assertTrue(body.closed)
            self.assertEqual(c.opener.open.call_count, 1)

    def test_reject_nonnumeric_error_fields(self):
        for value in (True, False, '190', SECRET, 190.0, None, [], {}, -1, 2147483648):
            out = d.summarize('legacy_path', 403, json.dumps({'error': {
                'code': value, 'error_subcode': value}}).encode())
            self.assertIsNone(out['meta_code'])
            self.assertIsNone(out['meta_subcode'])
        self.assertEqual(d.number(190), 190)

    def test_missing_and_malformed_errors(self):
        for data in ({}, {'error': None}, {'error': []}, {'error': SECRET}, [], 123):
            out = d.summarize('legacy_path', 400, json.dumps(data).encode())
            self.assertEqual(out['http_status'], 400)
            self.assertIsNone(out['meta_code'])
            self.assertIsNone(out['meta_subcode'])
        for raw in (b'not JSON' + SECRET.encode(), b'\xff', b'[' * 2000):
            out = d.summarize('legacy_path', 400, raw)
            self.assertEqual(out['status'], 'http_error')
            self.assertIsNone(out['meta_code'])

    def test_size_bound_errors_and_success(self):
        for status in (200, 400):
            c = self.client()
            r = response(b'x' * (d.MAX_BODY + 1), status)
            c.opener.open.return_value = r
            self.assertEqual(c.get('123', 'legacy_path', SINCE, UNTIL)['status'], 'oversize')
            r.read.assert_called_once_with(d.MAX_BODY + 1)
        c = self.client()
        body = Mock()
        body.read.return_value = b'x' * (d.MAX_BODY + 1)
        c.opener.open.side_effect = urllib.error.HTTPError(SECRET, 403, SECRET, {}, body)
        self.assertEqual(c.get('123', 'legacy_path', SINCE, UNTIL)['status'], 'oversize')
        body.read.assert_called_once_with(d.MAX_BODY + 1)

    def test_redirects_never_followed_or_bodies_read(self):
        for code in (301, 302, 303, 307, 308):
            handler = d.NoRedirect()
            self.assertIsNone(handler.redirect_request(urllib.request.Request(d.HOST), None,
                code, SECRET, {'Location': SECRET}, 'https://example.invalid/'))
            c = self.client()
            body = Mock()
            c.opener.open.side_effect = urllib.error.HTTPError(SECRET, code, SECRET, {'Location': SECRET}, body)
            out = c.get('123', 'legacy_path', SINCE, UNTIL)
            self.assertEqual(out['status'], 'redirect_blocked')
            body.read.assert_not_called()
            self.assertEqual(c.opener.open.call_count, 1)
        redirects = [h for h in d.Client('TOKEN_SECRET').opener.handlers
                     if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(redirects), 1)
        self.assertIsInstance(redirects[0], d.NoRedirect)

    def test_bad_inputs_do_not_use_budget_or_network(self):
        c = self.client()
        for page, probe, start in [('123/evil', 'legacy_path', SINCE),
                ('123', 'arbitrary', SINCE), ('123', 'legacy_path', '2026-11-12\n')]:
            with self.assertRaises(ValueError):
                c.get(page, probe, start, UNTIL)
        self.assertEqual(c.count, 0)
        c.opener.open.assert_not_called()

    def test_safe_transport_and_body_read_failures(self):
        c = self.client()
        c.opener.open.side_effect = RuntimeError(SECRET)
        out = c.get('123', 'legacy_path', SINCE, UNTIL)
        self.assertEqual(out['status'], 'transport_or_response_error')
        c = self.client()
        body = Mock()
        body.read.side_effect = RuntimeError(SECRET)
        c.opener.open.side_effect = urllib.error.HTTPError(SECRET, 403, SECRET, {}, body)
        out = c.get('123', 'legacy_path', SINCE, UNTIL)
        self.assertEqual(out['status'], 'body_read_error')
        self.assertEqual(out['http_status'], 403)
        self.assertNotIn(SECRET, str(out))

    def test_200_api_errors_and_empty_success(self):
        for raw, expected in [(b'{"error":{"code":190,"error_subcode":463}}', 'api_error'),
                (b'{"data":[]}', 'empty_or_missing'), (b'{"data":[{}]}', 'data_present'),
                (b'not JSON', 'invalid_response'), (b'[]', 'invalid_response')]:
            self.assertEqual(d.summarize('metric_parameter', 200, raw)['status'], expected)

    def test_configuration_is_parsed_never_imported(self):
        self.assertRegex(d.page_id_from_source(), r'^[0-9]{1,32}$')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.py'
            path.write_text('raise RuntimeError("must not execute")\nPAGE_ID = os.environ.get("FB_PAGE_ID", "123")')
            self.assertEqual(d.page_id_from_source(path), '123')
            path.write_text('PAGE_ID = "invented"')
            with self.assertRaises(ValueError):
                d.page_id_from_source(path)

    def test_preflight_has_zero_network(self):
        out = io.StringIO()
        with patch('sys.argv', ['diagnostic']), patch.object(socket.socket, 'connect', side_effect=AssertionError('network')) as connect, contextlib.redirect_stdout(out):
            self.assertEqual(d.main(), 0)
            connect.assert_not_called()
        self.assertIn('no network used', out.getvalue())

    def test_no_secret_fails_before_network(self):
        with patch('sys.argv', ['diagnostic', '--execute-read-only']), patch.dict('os.environ', {}, clear=True), patch.object(d.Client, 'get') as get, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(d.main(), 1)
            get.assert_not_called()
        self.assertEqual(out.getvalue(), 'diagnostic_failed_safely\n')

    def test_main_safe_output_and_unchanged_files(self):
        # Only mocked transport; no API calls even with the execute flag.
        before = {p: p.read_bytes() for p in ROOT.rglob('*') if p.is_file()}
        with patch('sys.argv', ['diagnostic', '--execute-read-only']), patch.dict('os.environ', {'FB_PAGE_ACCESS_TOKEN': 'TOKEN_SECRET'}), patch.object(urllib.request.OpenerDirector, 'open', side_effect=RuntimeError(SECRET)), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(d.main(), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data['requests'], 2)
        self.assertNotIn(SECRET, out.getvalue())
        self.assertEqual(before, {p: p.read_bytes() for p in ROOT.rglob('*') if p.is_file()})


if __name__ == '__main__':
    unittest.main()
