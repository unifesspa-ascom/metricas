"""Offline workflow tests: every Git mutation targets temporary local repos only."""
import contextlib
import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("posts_guard", ROOT / "scripts/posts_workflow_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def parsed_workflows():
    """Use an already installed parser, or a verified npm cache, never network/install."""
    files = {name: (ROOT / '.github/workflows' / name).read_text()
             for name in ('update-metrics.yml', 'backfill-posts.yml')}
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml:
        return {name: yaml.load(text, Loader=yaml.BaseLoader) for name, text in files.items()}
    cache = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.cache')) / 'npm-cache' / '_cacache'
    for index in (cache / 'index-v5').rglob('*'):
        if not index.is_file():continue
        for line in index.read_text(errors='replace').splitlines():
            try:entry = json.loads(line.split('\t', 1)[1])
            except (ValueError, IndexError):continue
            if not entry.get('key', '').startswith('make-fetch-happen:request-cache:https://registry.npmjs.org/js-yaml/-/'):
                continue
            integrity = entry.get('integrity', '')
            if not integrity.startswith('sha512-'):continue
            expected = base64.b64decode(integrity[7:])
            hexdigest = expected.hex()
            path = cache / 'content-v2' / 'sha512' / hexdigest[:2] / hexdigest[2:4] / hexdigest[4:]
            if not path.exists():continue
            raw = path.read_bytes()
            import hashlib
            if hashlib.sha512(raw).digest() != expected:continue
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
                member = archive.extractfile('package/dist/js-yaml.mjs')
                if member is None:continue
                module = member.read().decode()
            script = '''import fs from 'node:fs';
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const yaml=await import('data:text/javascript;base64,'+Buffer.from(input.module).toString('base64'));
console.log(JSON.stringify(Object.fromEntries(Object.entries(input.files).map(([name,text])=>[name,yaml.load(text)]))));'''
            result = subprocess.run(['node', '--input-type=module', '-e', script],
                                    input=json.dumps({'module': module, 'files': files}),
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:raise AssertionError('YAML syntax validation failed')
            return json.loads(result.stdout)
    raise unittest.SkipTest('No installed/cached YAML parser; install a validation tool separately')


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "offline", "GIT_COMMITTER_NAME": "offline",
            "GIT_AUTHOR_EMAIL": "offline@example.invalid", "GIT_COMMITTER_EMAIL": "offline@example.invalid",
            "FB_PAGE_ACCESS_TOKEN": "FAKE_OFFLINE_TOKEN", "PYTHONDONTWRITEBYTECODE": "1"}))
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.repo, self.server, self.work = self.temp / "repo", self.temp / "server.git", self.temp / "work"
        self.repo.mkdir()
        self.command(self.repo, "init", "-b", "main")
        self.command(self.temp, "init", "--bare", "-b", "main", str(self.server))
        for name in guard.PROTECTED:
            if name == guard.TARGET:
                continue
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / name).read_bytes())
        (self.repo / "dados").mkdir(exist_ok=True)
        self.before = {"atualizado": "2026-01-01", "facebook": [], "instagram": [
            {"id": "old", "data": "2026-01-01", "views": 12, "vis": 0, "alc": 4,
             "txt": "original", "custom": {"keep": [1, None, True]}}], "unknown": {"keep": True}}
        (self.repo / guard.TARGET).write_bytes(guard.canonical(self.before))
        (self.repo / "dados/metricas.json").write_text('{"legacy":"unchanged"}')
        (self.repo / "scripts/update_metrics.py").write_text("raise SystemExit(0)\n")
        self.command(self.repo, "add", ".")
        self.command(self.repo, "commit", "-m", "initial")
        self.command(self.repo, "remote", "add", "origin", str(self.server))
        self.command(self.repo, "push", "-u", "origin", "main")
        self.base = self.command(self.repo, "rev-parse", "HEAD")

    @staticmethod
    def command(repo, *args):
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
        if result.returncode:
            raise AssertionError("Temporary Git fixture operation failed")
        return result.stdout.decode().strip()

    def prepare(self, mode="update", env=None):
        guard.prepare(self.repo, self.work, "main", mode, env or {})

    def module(self, mode):
        module = guard.load_collector(self.repo, mode)
        module.INSIGHT_SLEEP = 0
        return module

    def collect(self, mode="update", posts=None, general=False):
        self.prepare(mode, {"BACKFILL_START": "2026-01-01"} if mode == "backfill" else {})
        module = self.module(mode)
        module.fetch_fb_posts = lambda *args, **kwargs: []
        module.get_ig_user_id = lambda: "synthetic-account"

        def media(*args, **kwargs):
            if general:
                module.INSIGHT_BLOCKED["instagram"] = general if isinstance(general, str) else "authentication"
            return posts if posts is not None else [{"id": "old", "data": "2026-01-02", "views": 0,
                                                      "txt": "new confirmed text", "curt": 0}]

        module.fetch_ig_posts = media
        with patch("time.sleep"):
            guard.collect(self.repo, self.work, loader=lambda repo, mode: module)
        return module

    def validated(self, **kwargs):
        module = self.collect(**kwargs)
        guard.validate_candidate(self.repo, self.work)
        return module

    def peer(self):
        peer = self.temp / "peer"
        if not peer.exists():
            self.command(self.temp, "clone", str(self.server), str(peer))
        return peer

    def advance(self, path="dados/stories.json", content='{"independent":true}'):
        peer = self.peer()
        file = peer / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        self.command(peer, "add", path)
        self.command(peer, "commit", "-m", "remote change")
        self.command(peer, "push", "origin", "main")
        return self.command(peer, "rev-parse", "HEAD")

    def test_prepare_requires_clean_repository_and_external_work_directory(self):
        with self.assertRaises(guard.GuardError):
            guard.prepare(self.repo, self.repo / "candidate", "main", "update", {})
        (self.repo / "unexpected.txt").write_text("untracked")
        with self.assertRaises(guard.GuardError):
            self.prepare()
        self.assertFalse(self.work.exists())

    def test_prepare_preserves_original_and_refuses_existing_work_or_bad_input(self):
        self.prepare()
        raw = (self.repo / guard.TARGET).read_bytes()
        self.assertEqual((self.work / "before.json").read_bytes(), raw)
        self.assertEqual((self.work / "candidate.json").read_bytes(), raw)
        with self.assertRaises(guard.GuardError):
            self.prepare()
        self.assertEqual(self.command(self.repo, "status", "--porcelain"), "")

    def test_safe_backfill_arguments_and_fixed_budgets(self):
        args = guard.arguments("backfill", {"BACKFILL_START": "2026-01-01", "BACKFILL_END": "2026-01-02",
                                            "SKIP_INSTAGRAM": "true", "SKIP_INSIGHTS": "true"})
        self.assertEqual(args, ["--start", "2026-01-01", "--end", "2026-01-02", "--skip-instagram",
                                "--skip-insights", "--max-requests", "2000", "--max-pages", "50"])
        for env in ({"BACKFILL_START": "$(touch leaked)"}, {"BACKFILL_START": "2026-02-30"},
                    {"BACKFILL_START": "2026-02-01", "BACKFILL_END": "2026-01-01"},
                    {"BACKFILL_START": "2026-01-01", "SKIP_INSIGHTS": "true; echo secret"}):
            with self.assertRaises(guard.GuardError):
                guard.arguments("backfill", env)
        self.assertEqual(guard.arguments("update", {})[-4:], ["--max-requests", "2000", "--max-pages", "50"])

    def test_independent_contract_rejects_invalid_values_ids_dates_and_links(self):
        for bad in (None, True, "0", -1, 0.5, float("nan")):
            doc = copy.deepcopy(self.before); doc["instagram"][0]["views"] = bad
            with self.assertRaises(guard.GuardError):
                guard.validate_document(doc)
        for key, value in (("id", "bad id"), ("data", "2026-02-30"), ("txt", 3),
                           ("url", "https://example.invalid/?access_token=private"),
                           ("url", "https://user:password@example.invalid/")):
            doc = copy.deepcopy(self.before); doc["instagram"][0][key] = value
            with self.assertRaises(guard.GuardError):
                guard.validate_document(doc)
        doc = copy.deepcopy(self.before); doc["instagram"].append(doc["instagram"][0])
        with self.assertRaises(guard.GuardError):guard.validate_document(doc)
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'broken'):
            with self.assertRaises(guard.GuardError):guard.decode(raw)

    def test_collect_receipt_comes_from_real_validated_results_and_hashes_text_and_ids(self):
        self.collect()
        receipt = guard.decode((self.work / "collected.json").read_bytes())
        entry = receipt["observations"]["instagram"][0]
        self.assertEqual(entry["fields"]["views"], 0)
        self.assertEqual(entry["fields"]["data"], "2026-01-02")
        self.assertEqual(len(entry["id_sha256"]), 64)
        text = (self.work / "collected.json").read_text()
        self.assertNotIn('"id":"old"', text)
        self.assertNotIn("new confirmed text", text)
        self.assertNotIn("FAKE_OFFLINE_TOKEN", text)
        self.assertEqual((self.repo / guard.TARGET).read_bytes(), guard.canonical(self.before))

    def test_independent_validation_preserves_unknown_history_and_accepts_zero(self):
        self.validated()
        candidate = guard.decode((self.work / "validated-posts.json").read_bytes())
        self.assertEqual(candidate["unknown"], self.before["unknown"])
        post = candidate["instagram"][0]
        self.assertEqual(post["data"], "2026-01-01")
        self.assertEqual(post["views"], 0)
        self.assertEqual(post["vis"], 0)
        self.assertEqual(post["alc"], 4)
        self.assertEqual(post["custom"], self.before["instagram"][0]["custom"])

    def test_validation_does_not_reconstruct_evidence_from_final_json(self):
        self.collect()
        candidate = guard.decode((self.work / "candidate.json").read_bytes())
        candidate["instagram"][0]["alc"] = 999
        raw = guard.canonical(candidate); (self.work / "candidate.json").write_bytes(raw)
        receipt = guard.decode((self.work / "collected.json").read_bytes())
        receipt["candidate_sha256"] = guard.digest(raw)
        guard.put(self.work / "collected.json", receipt)
        with self.assertRaises(guard.GuardError) as caught:guard.validate_candidate(self.repo, self.work)
        self.assertEqual(str(caught.exception), "history_or_field_integrity")
        self.assertFalse((self.work / "validated-posts.json").exists())

    def test_changed_candidate_or_removed_history_is_rejected(self):
        self.collect()
        candidate = guard.decode((self.work / "candidate.json").read_bytes()); candidate["instagram"] = []
        raw = guard.canonical(candidate); (self.work / "candidate.json").write_bytes(raw)
        with self.assertRaises(guard.GuardError):guard.validate_candidate(self.repo, self.work)
        receipt = guard.decode((self.work / "collected.json").read_bytes())
        receipt["candidate_sha256"] = guard.digest(raw); guard.put(self.work / "collected.json", receipt)
        with self.assertRaises(guard.GuardError):guard.validate_candidate(self.repo, self.work)

    def test_general_insight_failure_has_no_publishable_receipt(self):
        with self.assertRaises(guard.GuardError) as caught:self.collect(general=True)
        self.assertEqual(str(caught.exception), "general_insight_failure")
        self.assertFalse((self.work / "collected.json").exists())
        self.assertFalse((self.work / "validated-posts.json").exists())
        self.assertEqual((self.repo / guard.TARGET).read_bytes(), guard.canonical(self.before))

    def test_lock_and_mandatory_failures_never_validate_or_publish(self):
        self.prepare()
        lock = self.work / "candidate.json.lock"; lock.write_bytes(b"active")
        module = self.module("update")
        with patch.object(module, "fetch_fb_posts") as fetch:
            with self.assertRaises(guard.GuardError):
                guard.collect(self.repo, self.work, loader=lambda *args:module)
            fetch.assert_not_called()
        self.assertEqual(lock.read_bytes(), b"active")
        self.assertFalse((self.work / "collected.json").exists())

    def test_pagination_and_request_limits_block_publication(self):
        self.prepare()
        for category in ("page_limit", "request_limit", "invalid_pagination", "invalid_listing", "connection"):
            module = self.module("update")
            module.fetch_fb_posts = lambda *args, **kwargs: []
            module.get_ig_user_id = lambda:"ig"
            with patch.object(module,"fetch_ig_posts",side_effect=module.CollectionError(category)):
                with self.assertRaises(guard.GuardError):guard.collect(self.repo,self.work,loader=lambda *args:module)
            self.assertFalse((self.work / "collected.json").exists())
            self.assertEqual((self.work / "candidate.json").read_bytes(), guard.canonical(self.before))

    def test_backfill_skip_insights_preserves_views_and_skipped_instagram(self):
        self.prepare("backfill", {"BACKFILL_START":"2026-01-01", "SKIP_INSIGHTS":"true", "SKIP_INSTAGRAM":"true"})
        module = self.module("backfill")
        module.fetch_fb_posts = lambda *args, **kwargs: [{"id":"new-fb","data":"2026-01-02","comp":0}]
        with patch.object(module,"fetch_ig_posts") as instagram:
            guard.collect(self.repo,self.work,loader=lambda *args:module)
            instagram.assert_not_called()
        guard.validate_candidate(self.repo,self.work)
        candidate=guard.decode((self.work/"validated-posts.json").read_bytes())
        self.assertEqual(candidate['instagram'],self.before['instagram'])
        self.assertEqual(candidate['facebook'][0]['comp'],0)

    def test_missing_optional_metrics_are_preserved_and_reported_partial(self):
        self.validated(posts=[{"id":"old", "data":"2026-01-01", "com":0}])
        receipt=guard.decode((self.work/'collected.json').read_bytes())
        candidate=guard.decode((self.work/'validated-posts.json').read_bytes())
        self.assertEqual(receipt['status'],'partial')
        self.assertEqual(candidate['instagram'][0]['views'],12)
        self.assertEqual(candidate['instagram'][0]['com'],0)

    def test_publish_only_posts_without_modifying_real_checkout_index_or_account_data(self):
        self.validated()
        account=(self.repo/'dados/metricas.json').read_bytes()
        commit=guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})
        self.assertEqual(self.command(self.server,'diff-tree','--no-commit-id','--name-only','-r',commit),guard.TARGET)
        self.assertEqual(self.command(self.server,'rev-parse','refs/heads/main'),commit)
        self.assertEqual(self.command(self.repo,'rev-parse','HEAD'),self.base)
        self.assertEqual(self.command(self.repo,'status','--porcelain'),'')
        self.assertEqual((self.repo/'dados/metricas.json').read_bytes(),account)

    def test_unrelated_remote_advance_is_preserved_without_merge_or_rebase(self):
        self.validated(); remote=self.advance()
        commit=guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})
        self.assertEqual(self.command(self.server,'rev-parse',commit+'^'),remote)
        self.assertEqual(self.command(self.server,'show',commit+':dados/stories.json'),'{"independent":true}')
        self.assertEqual(self.command(self.server,'diff-tree','--no-commit-id','--name-only','-r',commit),guard.TARGET)

    def test_remote_posts_and_guard_components_changes_are_blocked(self):
        self.validated(); remote=self.advance('scripts/update_posts.py','print("changed")\n')
        with self.assertRaises(guard.GuardError) as caught:guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})
        self.assertEqual(str(caught.exception),'remote_components_changed')
        self.assertEqual(self.command(self.server,'rev-parse','refs/heads/main'),remote)
        self.assertTrue((self.work/'validated-posts.json').exists())

    def test_remote_json_change_is_not_overwritten(self):
        self.validated(); remote=self.advance(guard.TARGET,guard.canonical({**self.before,'atualizado':'2026-02-01'}).decode())
        with self.assertRaises(guard.GuardError):guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})
        self.assertEqual(self.command(self.server,'rev-parse','refs/heads/main'),remote)

    def test_late_remote_race_rejects_single_ordinary_push_and_keeps_artifact(self):
        self.validated(); original=guard.git; pushes=[]
        def race(repo,*args,**kwargs):
            if args and args[0]=='push':
                pushes.append(args);self.advance('independent.txt','late')
            return original(repo,*args,**kwargs)
        with patch.object(guard,'git',side_effect=race):
            with self.assertRaises(guard.GuardError):guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})
        self.assertEqual(len(pushes),1)
        self.assertNotIn('--force',pushes[0]);self.assertNotIn('-f',pushes[0])
        self.assertEqual(self.command(self.server,'show','refs/heads/main:'+guard.TARGET),guard.canonical(self.before).decode())
        self.assertTrue((self.work/'validated-posts.json').exists())

    def test_tampering_after_validation_blocks_publication(self):
        self.validated();(self.work/'candidate.json').write_text('{}')
        with self.assertRaises(guard.GuardError):guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})
        self.assertEqual(self.command(self.server,'rev-parse','refs/heads/main'),self.base)

    def test_dirty_staging_or_changed_local_head_blocks_publication(self):
        self.validated(); (self.repo/'unrelated.txt').write_text('not authorized')
        self.command(self.repo,'add','unrelated.txt')
        with self.assertRaises(guard.GuardError):guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})
        self.assertEqual(self.command(self.server,'rev-parse','refs/heads/main'),self.base)

    def test_sensitive_exceptions_and_credentials_never_reach_logs(self):
        stream=io.StringIO()
        with patch.object(guard,'collect',side_effect=RuntimeError('FAKE_OFFLINE_TOKEN https://private/?access_token=secret')),contextlib.redirect_stderr(stream):
            self.assertEqual(guard.main(['collect','--repo',str(self.repo),'--work',str(self.work)]),1)
        self.assertNotIn('FAKE_OFFLINE_TOKEN',stream.getvalue());self.assertNotIn('access_token',stream.getvalue())
        env=guard.auth_environment({'GITHUB_TOKEN':'FAKE_OFFLINE_TOKEN','PATH':os.environ['PATH']})
        self.assertNotIn('GITHUB_TOKEN',env)
        self.assertEqual(env['GIT_CONFIG_COUNT'],'1')
        self.assertNotIn('FAKE_OFFLINE_TOKEN',env['GIT_CONFIG_VALUE_0'])

    def test_account_failure_and_success_are_isolated_and_never_publish_or_echo_logs(self):
        account=(self.repo/'dados/metricas.json').read_bytes()
        for code in (0,1):
            work=self.temp/('account-'+str(code))
            with patch.object(guard.subprocess,'run',return_value=SimpleNamespace(returncode=code)) as run:
                if code:
                    with self.assertRaises(guard.GuardError):guard.account(self.repo,work)
                else:guard.account(self.repo,work)
                self.assertEqual(run.call_args.kwargs['stdout'],subprocess.DEVNULL)
                self.assertEqual(run.call_args.kwargs['stderr'],subprocess.DEVNULL)
                self.assertEqual(run.call_args.kwargs['cwd'],work)
            self.assertEqual((self.repo/'dados/metricas.json').read_bytes(),account)

    def test_other_general_failures_block_candidates(self):
        for category in ('permission','service','connection','rate_limit'):
            self.work=self.temp/category
            with self.assertRaises(guard.GuardError):self.collect(general=category)
            self.assertFalse((self.work/'collected.json').exists())

    def test_invalid_document_preparation_and_manifest_changes_fail_closed(self):
        self.collect()
        receipt=guard.decode((self.work/'collected.json').read_bytes())
        receipt['observations'].pop('instagram')
        guard.put(self.work/'collected.json',receipt)
        with self.assertRaises(guard.GuardError):guard.validate_candidate(self.repo,self.work)
        meta=guard.decode((self.work/'prepared.json').read_bytes())
        meta['args']=['--max-requests','9000','--max-pages','500']
        guard.put(self.work/'prepared.json',meta)
        with self.assertRaises(guard.GuardError):guard.read_state(self.work)

    def test_baseline_and_validated_manifest_integrity_are_checked_again(self):
        self.validated()
        receipt=(self.work/'collected.json').read_bytes()
        (self.work/'collected.json').write_bytes(receipt+b' ')
        with self.assertRaises(guard.GuardError):guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})
        (self.work/'collected.json').write_bytes(receipt)
        (self.work/'before.json').write_text('{}')
        with self.assertRaises(guard.GuardError):guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})

    def test_account_timeout_is_safe_and_has_no_relation_to_post_publication(self):
        with patch.object(guard.subprocess,'run',side_effect=subprocess.TimeoutExpired('private',240)):
            with self.assertRaises(guard.GuardError) as caught:guard.account(self.repo,self.temp/'account-timeout')
        self.assertEqual(str(caught.exception),'account_collection_timeout')
        self.validated()
        self.assertIsNotNone(guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''}))

    def test_account_subprocess_writes_only_temporary_candidate_and_discards_raw_output(self):
        script=self.repo/'scripts/update_metrics.py'
        script.write_text("from pathlib import Path\nimport sys\nprint('FAKE_OFFLINE_TOKEN', file=sys.stderr)\nPath('dados/metricas.json').write_text('{\"temporary\":true}')\n")
        original=(self.repo/'dados/metricas.json').read_bytes()
        output=io.StringIO();work=self.temp/'isolated-account'
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):guard.account(self.repo,work)
        self.assertEqual((self.repo/'dados/metricas.json').read_bytes(),original)
        self.assertEqual((work/'dados/metricas.json').read_text(),'{"temporary":true}')
        self.assertNotIn('FAKE_OFFLINE_TOKEN',output.getvalue())

    def test_diverged_remote_history_is_rejected(self):
        self.validated()
        tree=self.command(self.server,'rev-parse','refs/heads/main^{tree}')
        orphan=self.command(self.server,'commit-tree',tree,'-m','unrelated history')
        self.command(self.server,'update-ref','refs/heads/main',orphan)
        with self.assertRaises(guard.GuardError):guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''})
        self.assertEqual(self.command(self.server,'rev-parse','refs/heads/main'),orphan)

    def test_collector_output_cannot_leak_raw_logs(self):
        self.prepare();module=self.module('update')
        def broken(*args,**kwargs):
            print('FAKE_OFFLINE_TOKEN https://private/?access_token=secret')
            raise RuntimeError('private body')
        module.fetch_fb_posts=broken
        output=io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            with self.assertRaises(guard.GuardError):guard.collect(self.repo,self.work,loader=lambda *args:module)
        self.assertEqual(output.getvalue(),'')

    def test_no_change_is_a_success_without_commit_or_push(self):
        self.before['atualizado']=guard.datetime.now(guard.ZoneInfo('America/Sao_Paulo')).date().isoformat()
        (self.repo/guard.TARGET).write_bytes(guard.canonical(self.before))
        self.command(self.repo,'add',guard.TARGET)
        self.command(self.repo,'commit','-m','current baseline')
        self.command(self.repo,'push','origin','main')
        self.base=self.command(self.repo,'rev-parse','HEAD')
        self.validated(posts=[{'id':'old','data':'2026-01-01'}])
        self.assertIsNone(guard.publish(self.repo,self.work,env={**os.environ,'GITHUB_TOKEN':''}))
        self.assertEqual(self.command(self.server,'rev-parse','refs/heads/main'),self.base)

    def test_historical_date_conflicts_have_sanitized_counts_in_the_receipt(self):
        self.collect()
        receipt=guard.decode((self.work/'collected.json').read_bytes())
        self.assertEqual(receipt['warnings'].get('historical_date_conflict'),1)
        self.assertNotIn('FAKE_OFFLINE_TOKEN',(self.work/'collected.json').read_text())
        guard.validate_candidate(self.repo,self.work)


class WorkflowContractTests(unittest.TestCase):
    def test_yaml_syntax_and_parsed_workflow_semantics(self):
        workflows=parsed_workflows()
        update=workflows['update-metrics.yml'];backfill=workflows['backfill-posts.yml']
        self.assertEqual(update['on']['schedule'],[{'cron':'17 12 * * *'}])
        self.assertEqual(set(backfill['on']),{'workflow_dispatch'})
        posts=update['jobs']['publications'];historical=backfill['jobs']['backfill-posts']
        self.assertEqual(posts['concurrency'],historical['concurrency'])
        self.assertIn(posts['concurrency']['cancel-in-progress'],(False,'false'))
        for job in (posts,historical):
            self.assertEqual(job['permissions'],{'contents':'write'})
            self.assertNotIn('needs',job)
            self.assertIn('github.event.repository.default_branch',job['if'])
            steps=job['steps']
            self.assertEqual([step['id'] for step in steps if 'id' in step],['validate','publish'])
            self.assertEqual(next(s for s in steps if s.get('id')=='publish')['env'],{'GITHUB_TOKEN':'${{ github.token }}'})
            artifact=steps[-1]
            self.assertEqual(str(artifact['with']['retention-days']),'3')
            self.assertTrue(artifact['with']['path'].endswith('/validated-posts.json'))
        account=update['jobs']['account-observation']
        self.assertEqual(account['permissions'],{'contents':'read'})
        self.assertNotIn('needs',account)
        self.assertFalse(any(s.get('uses','').startswith('actions/upload-artifact') for s in account['steps']))

    def test_workflows_keep_triggers_shared_concurrency_permissions_and_safe_inputs(self):
        update=(ROOT/'.github/workflows/update-metrics.yml').read_text()
        backfill=(ROOT/'.github/workflows/backfill-posts.yml').read_text()
        self.assertEqual(update.count("cron:"),1);self.assertIn("'17 12 * * *'",update)
        self.assertNotIn('schedule:',backfill)
        for text in (update,backfill):
            self.assertIn('workflow_dispatch:',text)
            self.assertIn('group: publications-${{ github.repository }}',text)
            self.assertIn('cancel-in-progress: false',text)
            self.assertIn('permissions: {}',text)
            self.assertIn('persist-credentials: false',text)
            self.assertIn('timeout-minutes: 20',text)
            self.assertIn('timeout-minutes: 30',text)
            self.assertIn('retention-days: 3',text)
            self.assertIn('steps.validate.outcome == \'success\'',text)
            self.assertIn('steps.publish.outcome == \'failure\'',text)
            self.assertIn('validated-posts.json',text)
            self.assertNotIn('continue-on-error',text)
            self.assertNotIn('git add',text)
            self.assertNotIn('git push',text)
            self.assertNotIn('needs:',text)
            self.assertNotIn('secrets.', '\n'.join(line for line in text.splitlines() if line.strip().startswith('run:')))
        self.assertIn('contents: read',update)
        self.assertIn(' account --work ',update)
        self.assertNotIn('${{ github.event.inputs.',backfill)
        self.assertIn('BACKFILL_START: ${{ inputs.start_date }}',backfill)
        self.assertNotIn('dados/metricas.json',update)

    def test_bash_syntax_of_all_workflow_run_steps(self):
        bash=shutil.which('bash')
        if bash is None and Path('C:/Program Files/Git/bin/bash.exe').exists():
            bash='C:/Program Files/Git/bin/bash.exe'
        if bash is None:self.skipTest('Bash is not installed')
        # These workflows use only run scalars and literal blocks, with no expressions.
        import re
        for name in ('update-metrics.yml','backfill-posts.yml'):
            lines=(ROOT/'.github/workflows'/name).read_text().splitlines()
            for i,line in enumerate(lines):
                match=re.match(r'^(\s*)(?:- )?run: (.*)$',line)
                if not match:continue
                body=match[2]
                if body=='|':
                    parts=[]
                    for following in lines[i+1:]:
                        if following.strip() and len(following)-len(following.lstrip())<=len(match[1]):break
                        parts.append(following)
                    body='\n'.join(parts)
                result=subprocess.run([bash,'-n'],input=body,text=True,capture_output=True,timeout=20)
                self.assertEqual(result.returncode,0,'Invalid Bash in '+name)


if __name__=='__main__':
    unittest.main()
