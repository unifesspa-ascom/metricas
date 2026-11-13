"""Offline contracts for the proposed launcher; never dispatch an Action."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_posts_workflow import parsed_workflows

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/facebook-six-diagnostic-pinned.yml'
PIN = '8900c92ec8580cf2b917e5a3c896e56f68c41f26'
FILES = {
    'scripts/diagnose_facebook_six.py', 'scripts/facebook_account_metrics.py',
    'scripts/account_metrics_safety.py', 'scripts/update_metrics.py', 'scripts/backfill_metrics.py',
    'tests/test_diagnose_facebook_six.py', 'tests/test_facebook_account_metrics.py',
    'tests/test_account_metrics_safety.py',
}


def parsed():
    text = WORKFLOW.read_text(encoding='utf-8')
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path in (ROOT / '.github/workflows/update-metrics.yml', ROOT / '.github/workflows/backfill-posts.yml'):
            return text
        return original(path, *args, **kwargs)
    # Reuse the installed parser / verified cached JS parser without downloads.
    with patch.object(Path, 'read_text', read):
        return parsed_workflows()['update-metrics.yml']


class FacebookWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = parsed()
        cls.job = cls.workflow['jobs']['diagnostic']
        cls.steps = cls.job['steps']

    def test_manual_only_default_false_and_no_rerun(self):
        self.assertEqual(set(self.workflow['on']), {'workflow_dispatch'})
        inputs = self.workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(str(inputs['confirm_read_only']['default']).lower(), 'false')
        self.assertEqual(inputs['confirm_read_only']['type'], 'boolean')
        self.assertNotIn('default', inputs['day'])
        condition = self.job['if']
        for guard in ("github.repository == 'unifesspa-ascom/metricas'", "github.ref == 'refs/heads/main'",
                      "github.event_name == 'workflow_dispatch'", 'github.run_attempt == 1',
                      'inputs.confirm_read_only == true'):
            self.assertIn(guard, condition)
        self.assertEqual(self.workflow['concurrency']['group'], 'meta-account-diagnostic')
        self.assertEqual(str(self.workflow['concurrency']['cancel-in-progress']).lower(), 'false')

    def test_minimal_permissions_pinned_checkout_and_exact_inventory(self):
        self.assertEqual(self.workflow['permissions'], {})
        self.assertEqual(self.job['permissions'], {'contents': 'read'})
        self.assertEqual(set(self.workflow['jobs']), {'diagnostic'})
        checkout = self.steps[0]
        self.assertRegex(checkout['uses'], r'^actions/checkout@[a-f0-9]{40}$')
        self.assertEqual(checkout['with']['ref'], PIN)
        self.assertEqual(str(checkout['with']['persist-credentials']).lower(), 'false')
        inventory = {line.strip().lstrip('/') for line in checkout['with']['sparse-checkout'].splitlines()}
        self.assertEqual(inventory, FILES)
        self.assertFalse(any('dados/' in path for path in inventory))

    def test_hashes_match_git_blobs_and_are_rechecked_before_secret(self):
        self.assertEqual(self.steps[1]['run'], self.steps[-2]['run'])
        script = self.steps[1]['run']
        self.assertIn(PIN, script)
        hashes = dict((path, digest) for digest, path in re.findall(r'^([a-f0-9]{64})  (\S+)$', script, re.M))
        self.assertEqual(set(hashes), FILES)
        for path, digest in hashes.items():
            raw = subprocess.check_output(['git', 'show', PIN + ':' + path], cwd=ROOT)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest, path)
        self.assertIn('git diff --quiet HEAD --', script)
        self.assertIn('git ls-files --others --exclude-standard', script)

    def test_secrets_only_in_last_step_no_publication_or_retry_steps(self):
        self.assertNotIn('secrets.', str(self.job.get('env', {})))
        for step in self.steps[:-1]:
            self.assertNotIn('secrets.', str(step))
        final = self.steps[-1]
        self.assertEqual(final['env'], {'FB_PAGE_ACCESS_TOKEN': '${{ secrets.FB_PAGE_ACCESS_TOKEN }}',
                                       'FB_PAGE_ID': '${{ secrets.FB_PAGE_ID }}'})
        self.assertEqual(sum('--authorize-six-gets' in step.get('run', '') for step in self.steps), 1)
        self.assertIn('diagnostic.main', final['run'])
        for step in self.steps:
            self.assertNotIn('continue-on-error', step)
            self.assertNotIn('if', step)
            script = step.get('run', '')
            for prohibited in ('git push', 'git commit', 'upload-artifact', 'curl ', 'update_json(',
                               'fetch_instagram_metrics(', 'metricas.json', 'update_posts.py', 'update_stories.py'):
                self.assertNotIn(prohibited, script)
            self.assertNotIn('${{', script)  # Inputs only through environment, never shell interpolation.

    def test_exact_preflight_runs_offline_with_synthetic_closed_day(self):
        script = self.steps[3]['run']
        self.assertIn('socket.socket.connect = denied', script)
        self.assertIn('socket.getaddrinfo = denied', script)
        self.assertIn('explicit_authorization_required', script)
        # Execute the actual embedded Python only, never the credentialed step.
        code = script.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        env = dict(os.environ, DIAGNOSTIC_DAY='2026-01-01', PYTHONDONTWRITEBYTECODE='1')
        env.pop('FB_PAGE_ACCESS_TOKEN', None); env.pop('FB_PAGE_ID', None)
        # The unchanged launcher tests its pinned commit, not this working tree.
        # Recreate that checkout so current diagnostic changes cannot leak into it.
        with tempfile.TemporaryDirectory(prefix='facebook-pinned-preflight-') as folder:
            for name in FILES:
                target = Path(folder, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(subprocess.check_output(['git', 'show', PIN + ':' + name], cwd=ROOT))
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code], cwd=folder, env=env,
                                    capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertIn('Ran 115 tests', result.stderr.decode(errors='replace'))

    def test_bash_syntax_of_every_step(self):
        bash = shutil.which('bash')
        if bash is None and Path('C:/Program Files/Git/bin/bash.exe').exists():
            bash = 'C:/Program Files/Git/bin/bash.exe'
        if bash is None:
            self.skipTest('Bash is not installed')
        for step in self.steps:
            if 'run' in step:
                result = subprocess.run([bash, '-n'], input=step['run'], text=True,
                                        capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, step['name'])


if __name__ == '__main__':
    unittest.main()
