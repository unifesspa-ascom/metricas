import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('diagnostic', ROOT / 'scripts/diagnose_account_metrics.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
SENSITIVE = 'TOKEN_SECRET account_123 cursor_456 https://example.invalid/?access_token=SECRET'


def response(data, status=200):
    obj = Mock()
    obj.status = status
    obj.read.return_value = json.dumps(data).encode()
    obj.__enter__ = Mock(return_value=obj)
    obj.__exit__ = Mock(return_value=False)
    return obj


class Protections(unittest.TestCase):
    def client(self):
        client = d.Client('TOKEN_SECRET')
        client.opener = Mock()
        return client

    def test_hard_budget_counts_failures(self):
        c = self.client()
        c.opener.open.side_effect = RuntimeError(SENSITIVE)
        for _ in range(16):
            self.assertEqual(c.get('123', d.FB[0], '2026-11-12', '2026-11-13')[0],
                             'transport_or_response_error')
        with self.assertRaisesRegex(ValueError, 'request_budget_exhausted'):
            c.get('123', d.FB[0], '2026-11-12', '2026-11-13')
        self.assertEqual(c.opener.open.call_count, 16)

    def test_plan_12_gets_fixed_host_header_auth_no_pagination(self):
        c = self.client()
        def answer(req, timeout):
            self.assertEqual(req.get_method(), 'GET')
            self.assertTrue(req.full_url.startswith(d.HOST))
            self.assertNotIn('TOKEN_SECRET', req.full_url)
            self.assertEqual(req.get_header('Authorization'), 'Bearer TOKEN_SECRET')
            self.assertEqual(timeout, 20)
            return response({'instagram_business_account': {'id': '456'},
                             'data': [{'value': SENSITIVE}],
                             'paging': {'next': 'https://evil.invalid/' + SENSITIVE}})
        c.opener.open.side_effect = answer
        output = json.dumps(d.diagnose(c, '123', '2026-11-12', '2026-11-13'))
        self.assertEqual(c.count, 12)
        for secret in (SENSITIVE, '456', '123', 'TOKEN_SECRET', 'evil.invalid'):
            self.assertNotIn(secret, output)

    def test_missing_ig_skips_three_requests(self):
        c = self.client()
        c.opener.open.return_value = response({'data': []})
        result = d.diagnose(c, '123', 'a', 'b')
        self.assertEqual(c.count, 9)
        self.assertEqual([r['status'] for r in result['results'][-3:]], ['skipped'] * 3)

    def test_redirect_handler_never_builds_followup(self):
        handler = d.NoRedirect()
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(handler.redirect_request(
                urllib.request.Request(d.HOST + '123'), None, code, SENSITIVE,
                {'Location': 'https://evil.invalid/'}, 'https://evil.invalid/'))
        opener = d.Client('TOKEN_SECRET').opener
        self.assertEqual(sum(isinstance(h, urllib.request.HTTPRedirectHandler)
                             for h in opener.handlers), 1)
        self.assertTrue(any(isinstance(h, d.NoRedirect) for h in opener.handlers))

    def test_http_errors_no_retries_no_body_read(self):
        for code in (302, 401, 429, 500):
            c = self.client()
            body = Mock()
            error = urllib.error.HTTPError(d.HOST + '123', code, SENSITIVE, {}, body)
            c.opener.open.side_effect = error
            result = c.get('123', d.FB[0], 'a', 'b')
            self.assertEqual(result[0], 'redirect_blocked' if code == 302 else 'http_error')
            self.assertEqual(c.opener.open.call_count, 1)
            body.read.assert_not_called()
            self.assertNotIn(SENSITIVE, str(result))

    def test_body_size_and_malformed_responses(self):
        for raw, expected in [(b'x' * (d.MAX_BODY + 1), 'oversize'),
                              (b'not JSON', 'transport_or_response_error'),
                              (b'[]', 'invalid_response'),
                              (json.dumps({'error': SENSITIVE}).encode(), 'api_error')]:
            c = self.client()
            r = response({})
            r.read.return_value = raw
            c.opener.open.return_value = r
            self.assertEqual(c.get('123', d.FB[0], 'a', 'b'), (expected, None))
            r.read.assert_called_once_with(d.MAX_BODY + 1)

    def test_disallow_arbitrary_paths_and_metrics(self):
        c = self.client()
        for account, metric in [('https://evil.invalid', d.FB[0]), ('123/../../', d.FB[0]),
                                ('123', 'arbitrary'), ('123\n', d.FB[0])]:
            with self.assertRaises(ValueError):
                c.get(account, metric, 'a', 'b')
        c.opener.open.assert_not_called()

    def test_config_read_without_import_or_token(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertRegex(d.page_id_from_source(), r'^[0-9]{1,32}$')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.py'
            path.write_text('raise RuntimeError("must not execute")\n'
                            'PAGE_ID = os.environ.get("FB_PAGE_ID", "123")\n')
            self.assertEqual(d.page_id_from_source(path), '123')
            path.write_text('PAGE_ID = "invented"')
            with self.assertRaises(ValueError):
                d.page_id_from_source(path)

    def test_default_preflight_has_zero_network(self):
        out = io.StringIO()
        with patch('sys.argv', ['diagnostic']), patch.object(d.Client, 'get') as get:
            with contextlib.redirect_stdout(out):
                self.assertEqual(d.main(), 0)
            get.assert_not_called()
        self.assertIn('no network used', out.getvalue())

    def test_main_suppresses_sensitive_exception(self):
        out = io.StringIO()
        with patch('sys.argv', ['diagnostic', '--execute-read-only']), \
                patch.object(d, 'diagnose', side_effect=RuntimeError(SENSITIVE)), \
                patch.dict('os.environ', {'FB_PAGE_ACCESS_TOKEN': 'TOKEN_SECRET'}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(d.main(), 1)
        self.assertEqual(out.getvalue(), 'diagnostic_failed_safely\n')

    def test_workflow_security_contract(self):
        workflow = (ROOT / '.github/workflows/diagnose-account-metrics.yml').read_text()
        self.assertIn('permissions: {}', workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('github.run_attempt == 1', workflow)
        self.assertIn("refs/heads/diagnostic/account-metrics-online", workflow)
        self.assertIn('default: false', workflow)
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('ref: ${{ github.sha }}', workflow)
        for forbidden in ('schedule:', 'cron:', 'write', 'git push', 'git commit',
                          'pull_request', 'upload-artifact', 'pip install'):
            self.assertNotIn(forbidden, workflow)
        self.assertEqual(workflow.count('secrets.FB_PAGE_ACCESS_TOKEN'), 1)
        self.assertLess(workflow.index('unittest discover'), workflow.index('secrets.FB_PAGE_ACCESS_TOKEN'))


if __name__ == '__main__':
    unittest.main()
