"""Offline policy, mutation and real sparse-checkout tests. Requires PyYAML."""
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/meta-account-diagnostic-pinned.yml'
FIXTURE = Path(os.environ.get('META_DIAGNOSTIC_FIXTURE', ROOT.parent / 'metricas-diagnostic'))
SHA = 'de9d2cb3df1ed85641d30c26aeed2f83485311c7'
FILES = {
 'scripts/diagnose_account_metrics.py': 'b9699b9f059d58b6b2c4f706f877ce3fc7b95dcd8b80927c769efc66c2ca84ba',
 'scripts/update_metrics.py': 'c3c64677b0f4f52f24c5d7060032ce4dc8f112c035eb43329f4d4eb9777729bf',
 'tests/test_diagnose_account_metrics.py': 'd174789f911ac8f425e08475975c606b76f84121465441938596b8a299c6d6b1',
 '.github/workflows/diagnose-account-metrics.yml': '3c134d814a26df4d69c10de0c00542a53e1cce20e633abc35351d69c38d45f4e',
}


def validate(w):
    assert set(w['on']) == {'workflow_dispatch'}
    assert w['on']['workflow_dispatch']['inputs'] == {'confirm_read_only': {
        'description': 'Confirmo executar ate 16 GETs Meta, sem publicar dados',
        'required': True, 'default': False, 'type': 'boolean'}}
    assert w['permissions'] == {}
    assert set(w['jobs']) == {'diagnostic'}
    job = w['jobs']['diagnostic']
    assert job['permissions'] == {'contents': 'read'}
    assert job['runs-on'] == 'ubuntu-24.04'
    assert job['timeout-minutes'] == 8
    assert job['if'] == "${{ github.repository == 'unifesspa-ascom/metricas' && github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' && github.run_attempt == 1 && inputs.confirm_read_only == true }}"
    assert job['defaults']['run'] == {'shell': 'bash --noprofile --norc -euo pipefail {0}', 'working-directory': 'diagnostic'}
    assert not any(k in job for k in ('env', 'container', 'services'))
    steps = job['steps']
    assert len(steps) == 6
    assert steps[0]['uses'] == 'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683'
    assert steps[0]['with'] == {
        'repository': 'unifesspa-ascom/metricas', 'ref': SHA, 'path': 'diagnostic',
        'fetch-depth': 1, 'persist-credentials': False, 'submodules': False,
        'lfs': False, 'sparse-checkout-cone-mode': False,
        'sparse-checkout': ''.join('/' + p + '\n' for p in FILES)}
    for step in steps[1:]:
        assert set(step) <= {'name', 'run', 'env'}  # no continue-on-error or always()
    assert 'git rev-parse HEAD' in steps[1]['run'] and SHA in steps[1]['run']
    assert 'sha256sum --check --status' in steps[1]['run']
    for path, digest in FILES.items():
        assert digest + '  ' + path in steps[1]['run']
    assert steps[2]['run'] == '/usr/bin/python3 -I -S -B -m unittest discover -s tests -p test_diagnose_account_metrics.py -v'
    assert steps[3]['run'] == '/usr/bin/python3 -I -S -B scripts/diagnose_account_metrics.py'
    assert steps[4]['run'] == 'git diff --quiet HEAD --\ntest -z "$(git ls-files --others --exclude-standard)"\n'
    assert steps[5]['env'] == {'FB_PAGE_ACCESS_TOKEN': '${{ secrets.FB_PAGE_ACCESS_TOKEN }}'}
    assert steps[5]['run'] == 'exec /usr/bin/python3 -I -S -B scripts/diagnose_account_metrics.py --execute-read-only'
    assert all('env' not in s for s in steps[:5])
    assert yaml.safe_dump(w).count('secrets.') == 1
    assert not any(k in w for k in ('env', 'defaults'))


class PinnedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.w = yaml.safe_load(WORKFLOW.read_text())

    def test_workflow_policy(self):
        validate(self.w)

    def test_reject_mutations(self):
        mutations = [
            lambda w: w['on'].update({'push': None}),
            lambda w: w['on']['workflow_dispatch']['inputs'].update({'sha': {'type': 'string'}}),
            lambda w: w['permissions'].update({'contents': 'write'}),
            lambda w: w['jobs']['diagnostic'].update({'if': '${{ always() }}'}),
            lambda w: w['jobs']['diagnostic']['steps'][0]['with'].update({'ref': 'main'}),
            lambda w: w['jobs']['diagnostic']['steps'][0]['with'].update({'persist-credentials': True}),
            lambda w: w['jobs']['diagnostic']['steps'][0]['with'].update({'sparse-checkout-cone-mode': True}),
            lambda w: w['jobs']['diagnostic']['steps'][2].update({'continue-on-error': True}),
            lambda w: w['jobs']['diagnostic'].update({'env': {'TOKEN': '${{ secrets.FB_PAGE_ACCESS_TOKEN }}'}}),
            lambda w: w['jobs']['diagnostic']['steps'][3].update({'run': 'python scripts/update_metrics.py'}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(self.w)
                mutation(changed)
                with self.assertRaises(AssertionError):
                    validate(changed)

    def test_real_sparse_checkout_hashes_tests_preflight_and_tamper(self):
        if not FIXTURE.exists():
            self.skipTest('Set FIXTURE to a local checkout of the reviewed SHA to run integration')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def run(*args, ok=True):
                result = subprocess.run(args, cwd=root, text=True, capture_output=True)
                if ok:
                    self.assertEqual(result.returncode, 0, result.stderr)
                return result
            run('git', 'init', '-q')
            for path, digest in FILES.items():
                source = (FIXTURE / path).read_bytes()
                self.assertEqual(hashlib.sha256(source).hexdigest(), digest)
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source)
            for path in ('dados/metricas.json', 'scripts/sitecustomize.py', 'tests/test_decoy.py', 'README.md'):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('raise RuntimeError("must not execute")')
            run('git', 'add', '.')
            run('git', '-c', 'user.name=Offline test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
            actual_sha = run('git', 'rev-parse', 'HEAD').stdout.strip()
            run('git', 'sparse-checkout', 'set', '--no-cone', *('/' + p for p in FILES))
            materialized = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts}
            self.assertEqual(materialized, set(FILES))
            steps = self.w['jobs']['diagnostic']['steps']
            # Fixture commit differs only for this offline integration test.
            verify = steps[1]['run'].replace(SHA, actual_sha)
            run('bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', verify)
            for step in steps[2:5]:
                result = run('bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', step['run'])
                if step is steps[3]:
                    self.assertIn('no network used', result.stdout)
            target = root / 'scripts/diagnose_account_metrics.py'
            target.write_text(target.read_text() + '\n# tampered\n')
            self.assertNotEqual(run('bash', '-eu', '-c', verify, ok=False).returncode, 0)
            self.assertNotEqual(run('bash', '-eu', '-c', steps[4]['run'], ok=False).returncode, 0)
            # The secret-bearing final step is intentionally never executed.


if __name__ == '__main__':
    unittest.main()
