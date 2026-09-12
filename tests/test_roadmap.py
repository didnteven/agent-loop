"""Acceptance tests for unattended execution; providers are simulated."""
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from loop.adapters import Result, command, parse, usage_fraction
from loop.engine import Engine, git, validate_plan
from loop.planner import validate_generated_plan
from loop import health, improve, learning
from loop.registry import CapReached, CapWait, Registry
from loop.service import Service, launchd_plist
from tests.support import isolate_registry


class RoadmapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'verify.py').write_text('from answer import value\nassert value == 42\n')
        (self.repo / '.gitignore').write_text('__pycache__/\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        self.plan = dict(id='roadmap', tasks=[dict(id='answer', provider='codex',
            files=['answer.py'], prompt='Set value to 42', check=['python3', 'verify.py'])])

    def good(self, task, prompt, workspace):
        (workspace / task['files'][0]).write_text('value = 42\n')
        return Result('ok', 'Implemented')

    def test_fresh_target_initializes_without_ignore_edit(self):
        self.engine.initialize(self.plan)
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        self.assertEqual((self.repo / '.gitignore').read_text(), '__pycache__/\n')

    def test_validate_plan_uses_target_root(self):
        (self.repo / 'link').symlink_to(self.repo.parent)
        self.plan['tasks'][0]['files'] = ['link/outside.py']
        with self.assertRaises(ValueError):
            self.engine.initialize(self.plan)

    def test_linked_worktree_exclude(self):
        linked = self.repo.parent / 'linked'
        git(self.repo, 'worktree', 'add', '-b', 'linked', str(linked))
        engine = Engine(linked)
        self.addCleanup(engine.db.close)
        engine.initialize(self.plan)
        self.assertEqual(git(linked, 'status', '--porcelain'), '')
        exclude = self.repo / '.git/info/exclude'
        self.assertEqual(exclude.read_text().count('/.agent-loop/'), 1)

    def test_interrupted_init_adopts_worktree(self):
        original = git
        def interrupted(repo, *args):
            result = original(repo, *args)
            if args[:2] == ('worktree', 'add'):
                raise RuntimeError('injected after worktree creation')
            return result
        with patch('loop.engine.git', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.engine.initialize(self.plan)
        intent = self.engine.home / 'intents/roadmap.json'
        recorded = json.loads(intent.read_text())
        run = self.engine.initialize(self.plan)
        self.assertEqual(run['workspace'], recorded['workspace'])
        self.assertFalse(intent.exists())
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_conflicting_workspace_is_preserved(self):
        path = self.engine.home / 'worktrees/roadmap'
        path.mkdir(parents=True)
        (path / 'user-work').write_text('preserve me')
        run = self.engine.initialize(self.plan)
        self.assertNotEqual(run['workspace'], str(path))
        self.assertEqual((path / 'user-work').read_text(), 'preserve me')

    def independent(self):
        self.plan['tasks'].append(dict(id='independent', provider='claude', files=['other.py'],
            prompt='Set value to 42', check=['python3', '-c', 'from other import value; assert value == 42'], depends_on=[]))

    def test_parked_task_allows_independent_work(self):
        self.independent()
        self.engine.initialize(self.plan)
        self.engine.park('roadmap', 'answer', 'no useful strategy')
        self.assertEqual(self.engine.tick(self.plan, self.good)[0], 'progress')
        self.assertEqual(self.engine.tick(self.plan, self.good)[0], 'parked')
        self.assertFalse((self.engine.home / 'roadmap-pr.md').exists())

    def test_parked_task_keeps_implicit_dependents_pending(self):
        self.independent()
        del self.plan['tasks'][1]['depends_on']
        self.engine.initialize(self.plan)
        self.engine.park('roadmap', 'answer', 'no useful strategy')
        self.assertEqual(self.engine.tick(self.plan, self.good)[0], 'parked')
        self.assertEqual(self.engine.db.execute("SELECT status FROM tasks WHERE id='independent'").fetchone()[0], 'pending')

    def test_wait_releases_independent_work(self):
        self.independent()
        self.engine.hold('codex', 1000, 'quota')
        self.assertEqual(self.engine.tick(self.plan, self.good, now=100)[0], 'progress')
        self.assertEqual(self.engine.tick(self.plan, self.good, now=100), ('waiting', 1000))

    def test_dependency_cycles_and_unknown_ids_rejected(self):
        self.plan['tasks'][0]['depends_on'] = ['answer']
        with self.assertRaisesRegex(ValueError, 'cycle'):
            validate_plan(self.plan, self.repo)
        self.plan['tasks'][0]['depends_on'] = ['missing']
        with self.assertRaisesRegex(ValueError, 'dependency'):
            validate_plan(self.plan, self.repo)

    def test_policy_rejects_protected_and_outside_root(self):
        self.plan['policy'] = {
            'allowed_roots': ['src'],
            'protected_paths': ['src/gate.py'],
            'trusted_checks': [self.plan['tasks'][0]['check']],
        }
        self.plan['tasks'][0]['files'] = ['src/gate.py']
        with self.assertRaisesRegex(ValueError, 'outside allowed roots or protected'):
            validate_plan(self.plan, self.repo)
        self.plan['tasks'][0]['files'] = ['other.py']
        with self.assertRaisesRegex(ValueError, 'outside allowed roots or protected'):
            validate_plan(self.plan, self.repo)

    def test_new_file_allowed_under_root(self):
        self.plan['tasks'][0]['files'] = ['generated/new.py']
        self.plan['policy'] = {
            'allowed_roots': ['generated'],
            'trusted_checks': [self.plan['tasks'][0]['check']],
        }
        validate_generated_plan(self.plan, self.repo)

    def test_decision_recorded_and_in_pr_body(self):
        run = self.engine.initialize(self.plan)
        decision = self.engine.decide('roadmap', 'answer', 'Which layout?',
                                      ['flat', 'nested'], 'flat', 'Repository convention')
        self.assertTrue(decision)
        self.engine.set_task('roadmap', 'answer', status='done', sha=run['base'])
        self.engine.prepare_pr(self.plan, run)
        body = (self.engine.home / 'roadmap-pr.md').read_text()
        self.assertIn('Decisions', body)
        self.assertIn('Which layout?', body)
        self.assertIn('flat', body)

    def test_question_answered_once_then_escalates(self):
        self.plan['tasks'][0]['model'] = 'gpt-cheap'
        self.plan['policy'] = {
            'models': [
                {'provider': 'codex', 'model': 'gpt-cheap', 'effort': 'low'},
                {'provider': 'codex', 'model': 'gpt-strong', 'effort': 'high'},
            ],
            'trusted_checks': [self.plan['tasks'][0]['check']],
        }
        def question(*_):
            return Result('ok', 'Which option do you want?')
        self.assertEqual(self.engine.tick(self.plan, question)[0], 'progress')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE id='answer'").fetchone()
        self.assertEqual(row['question_count'], 1)
        self.assertIn('Decision rule', row['error'])
        self.assertEqual(self.engine.tick(self.plan, question)[0], 'progress')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE id='answer'").fetchone()
        self.assertEqual(row['selected_model'], 'gpt-strong')
        self.assertEqual(row['selected_effort'], 'high')
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0], 2)

    def test_no_change_with_passing_check_is_already_satisfied(self):
        (self.repo / 'answer.py').write_text('value = 42\n')
        git(self.repo, 'add', 'answer.py')
        git(self.repo, 'commit', '-qm', 'already complete')
        def no_change(*_):
            return Result('ok', 'Already satisfied')
        self.assertEqual(self.engine.tick(self.plan, no_change)[0], 'progress')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE id='answer'").fetchone()
        workspace = Path(self.engine.db.execute(
            "SELECT workspace FROM runs WHERE id='roadmap'").fetchone()[0])
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['sha'], git(workspace, 'rev-parse', 'HEAD'))

    def test_successor_contains_parent_commit_and_reuses_verified_task(self):
        self.assertEqual(self.engine.tick(self.plan, self.good)[0], 'progress')
        parent_sha = self.engine.db.execute(
            "SELECT sha FROM tasks WHERE run_id='roadmap' AND id='answer'").fetchone()[0]
        successor = self.engine.succeed_plan(self.plan, 'new evidence', {})
        child = self.engine.db.execute("SELECT * FROM runs WHERE id=?", (successor['id'],)).fetchone()
        task = self.engine.db.execute("SELECT * FROM tasks WHERE run_id=?", (successor['id'],)).fetchone()
        self.assertEqual(child['parent_id'], 'roadmap')
        self.assertEqual(child['base'], parent_sha)
        self.assertEqual(task['status'], 'done')
        self.assertEqual(task['sha'], parent_sha)
        self.assertEqual(git(Path(child['workspace']), 'rev-parse', 'HEAD'), parent_sha)
        self.assertEqual(self.engine.db.execute(
            "SELECT superseded_by FROM runs WHERE id='roadmap'").fetchone()[0], successor['id'])


class AccountingTests(unittest.TestCase):
    """Step 3: every model call is attributed and capped allowance is shared."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry_dir = isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'verify.py').write_text('from answer import value\nassert value == 42\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        self.plan = dict(id='acct', tasks=[dict(id='answer', provider='codex',
            files=['answer.py'], prompt='Set value to 42', check=['python3', 'verify.py'])])

    def good(self, task, prompt, workspace):
        (workspace / task['files'][0]).write_text('value = 42\n')
        return Result('ok', 'Implemented', {'input_tokens': 100, 'output_tokens': 20})

    def capped(self, **limits):
        plan = copy.deepcopy(self.plan)
        plan['policy'] = {'limits': limits}
        return plan

    def test_every_invocation_is_attributed_and_reconciled_once(self):
        self.engine.tick(self.plan, self.good)
        row = self.engine.db.execute('SELECT * FROM invocations').fetchone()
        self.assertEqual(row['kind'], 'implement')
        self.assertEqual(row['input_tokens'], 100)
        self.assertEqual(row['output_tokens'], 20)
        self.assertIsNotNone(row['reservation_id'])
        registry = Registry(self.registry_dir / 'registry.sqlite')
        self.addCleanup(registry.close)
        reservation = registry.db.execute('SELECT * FROM reservations').fetchone()
        self.assertEqual(reservation['state'], 'reconciled')
        self.assertEqual(reservation['actual'], 120)
        self.assertEqual(reservation['objective_id'], 'acct')
        # Reconciliation is idempotent: a replayed settle cannot double-count.
        self.assertFalse(registry.reconcile(reservation['id'], 999))

    def test_unknown_usage_stays_unreconciled_and_holds_allowance(self):
        def silent(task, prompt, workspace):
            (workspace / task['files'][0]).write_text('value = 42\n')
            return Result('ok', 'Implemented')
        self.engine.tick(self.plan, silent)
        registry = Registry(self.registry_dir / 'registry.sqlite')
        self.addCleanup(registry.close)
        reservation = registry.db.execute('SELECT * FROM reservations').fetchone()
        self.assertEqual(reservation['state'], 'reserved')
        self.assertIsNone(reservation['actual'])
        row = self.engine.db.execute('SELECT * FROM invocations').fetchone()
        self.assertIsNone(row['input_tokens'])

    def test_planning_before_a_run_is_accounted(self):
        from loop import planner
        with patch.object(planner, 'run_process', return_value=(0, json.dumps(
                {'type': 'turn.completed', 'usage': {'input_tokens': 7, 'output_tokens': 3}})
                + '\n' + json.dumps({'item': {'type': 'agent_message', 'text': 'notes'}}), '')):
            planner.ask('codex', None, 'scout this repository', self.repo, engine=self.engine,
                        run_id='planning', task_id='scout', kind='plan')
        row = self.engine.db.execute("SELECT * FROM invocations WHERE kind='plan'").fetchone()
        self.assertEqual(row['task_id'], 'scout')
        self.assertEqual(row['input_tokens'], 7)
        self.assertIsNotNone(row['reservation_id'])

    def test_simultaneous_targets_cannot_reserve_the_same_allowance(self):
        registry = Registry(self.registry_dir / 'registry.sqlite')
        self.addCleanup(registry.close)
        limits = {'provider:codex': {'lifetime_tokens': 100}}
        common = dict(provider='codex', account='shared', bucket='codex', run_id='r',
                      task_id='t', kind='implement', limits=limits, tokens=60)
        first = registry.reserve(objective_id='one', **common)
        self.assertTrue(first)
        with self.assertRaises(CapReached):
            registry.reserve(objective_id='two', **common)
        # Reconciling the first at its true, smaller usage reopens the allowance.
        registry.reconcile(first, 10)
        self.assertTrue(registry.reserve(objective_id='two', **common))

    def test_rolling_limit_waits_and_lifetime_limit_parks(self):
        plan = self.capped(**{'provider:codex': {'rolling': {'tokens': 10, 'window_seconds': 600}}})
        state, deadline = self.engine.tick(plan, self.good)
        self.assertEqual(state, 'waiting')
        self.assertGreater(deadline, time.time())
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='acct'").fetchone()
        self.assertEqual(row['status'], 'waiting')
        # A rolling wait is not a coding attempt.
        self.assertEqual(row['attempts'], 0)

        engine = Engine(self.repo)
        self.addCleanup(engine.db.close)
        lifetime = self.capped(**{'provider:codex': {'lifetime_tokens': 10}})
        lifetime['id'] = 'acct-life'
        self.assertEqual(engine.tick(lifetime, self.good)[0], 'parked')
        row = engine.db.execute("SELECT * FROM tasks WHERE run_id='acct-life'").fetchone()
        self.assertEqual(row['status'], 'parked')
        self.assertEqual(row['wake_kind'], 'policy')

    def test_capped_admission_refuses_when_the_registry_is_unavailable(self):
        self.engine.caps = {'account': {'lifetime_tokens': 1000}}
        with patch.object(Registry, 'reserve', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(CapWait):
                self.engine.reserve('codex', 'codex', 'acct', 'acct', 'answer', 'implement')

    def test_monitoring_mode_degrades_without_stopping_work(self):
        with patch.object(Registry, 'reserve', side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(self.engine.tick(self.plan, self.good)[0], 'progress')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='acct'").fetchone()
        self.assertEqual(row['status'], 'done')
        self.assertTrue(self.engine.db.execute(
            "SELECT 1 FROM events WHERE kind='registry_degraded'").fetchone())

    def test_outbox_replays_accounting_the_registry_could_not_take(self):
        registry = Registry(self.registry_dir / 'registry.sqlite')
        self.addCleanup(registry.close)
        reservation = registry.reserve(provider='codex', account='a', bucket='codex',
                                       objective_id='acct', run_id='acct', task_id='answer',
                                       kind='implement', limits={}, tokens=60)
        with patch.object(Registry, 'reconcile', side_effect=sqlite3.OperationalError('locked')):
            self.engine.settle(reservation, 42)
        self.assertTrue(list((self.engine.home / 'outbox').glob('*.json')))
        self.engine.flush_outbox()
        self.assertFalse(list((self.engine.home / 'outbox').glob('*.json')))
        self.assertEqual(registry.db.execute(
            'SELECT actual FROM reservations WHERE id=?', (reservation,)).fetchone()[0], 42)

    def test_unchanged_transient_failures_terminate(self):
        plan = copy.deepcopy(self.plan)
        plan['transient_attempts'] = 2
        def flaky(*_):
            return Result('transient', '', {}, error='connection reset by peer')
        moment = time.time()
        for index in range(2):
            self.engine.tick(plan, flaky, now=moment + index * 3600)
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='acct'").fetchone()
        self.assertEqual(row['status'], 'parked')
        self.assertEqual(row['wake_kind'], 'environment')

    def test_replan_is_attempted_once_then_the_objective_parks(self):
        plan = copy.deepcopy(self.plan)
        plan['auto_replan'] = True
        def wrong(task, prompt, workspace):
            (workspace / task['files'][0]).write_text('value = 1\n')
            return Result('ok', 'Implemented', {'input_tokens': 1, 'output_tokens': 1})
        self.assertEqual(self.engine.tick(plan, wrong)[0], 'progress')
        repaired = copy.deepcopy(plan)
        repaired['tasks'][0]['files'] = ['answer.py', 'helper.py']
        with patch('loop.planner.repair_plan', return_value=repaired) as repair:
            self.assertEqual(self.engine.tick(plan, wrong)[0], 'superseded')
        self.assertEqual(repair.call_count, 1)
        successor = self.engine.db.execute(
            "SELECT id FROM runs WHERE parent_id='acct'").fetchone()['id']
        self.assertTrue(successor)
        # The successor inherits the objective, so the same evidence cannot buy
        # a second replan: it parks instead of reauthoring forever.
        successor_plan = json.loads(self.engine.db.execute(
            'SELECT plan FROM runs WHERE id=?', (successor,)).fetchone()['plan'])
        successor_plan['auto_replan'] = True
        with patch('loop.planner.repair_plan', return_value=repaired) as repair:
            for _ in range(2):
                state = self.engine.tick(successor_plan, wrong)[0]
        self.assertEqual(state, 'parked')
        self.assertEqual(repair.call_count, 0)


class DetachedWorkerTests(unittest.TestCase):
    """Step 4: ownership, durable results, and crash boundaries, with real children."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'verify.py').write_text('from answer import value\nassert value == 42\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        self.plan = dict(id='detached', worker_lease_seconds=60,
                         tasks=[dict(id='answer', provider='codex', files=['answer.py'],
                                     prompt='Set value to 42', check=['python3', 'verify.py'])])
        # These tests spawn real detached children. Reap them before the
        # temporary directory is removed, or a still-running worker races the
        # cleanup and fails the teardown rather than the assertion.
        self.spawned = []
        original_spawn = Engine.spawn

        def recording_spawn(engine, argv):
            process = original_spawn(engine, argv)
            self.spawned.append(process)
            return process

        spawn_patch = patch.object(Engine, 'spawn', recording_spawn)
        spawn_patch.start()
        self.addCleanup(spawn_patch.stop)
        self.addCleanup(self.reap_workers)
        # A real shim on PATH: the detached child is a separate process, so a
        # patched function in this one would not reach it.
        binaries = Path(self.temp.name) / 'bin'
        binaries.mkdir()
        shim = binaries / 'codex'
        shim.write_text('#!/bin/sh\n'
                        'if [ "$1" = "app-server" ]; then exit 1; fi\n'
                        'exec %s %s\n' % (sys.executable,
                                           Path(__file__).resolve().parent / 'fake_provider.py'))
        shim.chmod(0o755)
        environment = patch.dict(os.environ, {'PATH': str(binaries) + os.pathsep + os.environ['PATH']})
        environment.start()
        self.addCleanup(environment.stop)

    def reap_workers(self):
        for process in self.spawned:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def run_detached(self, plan=None):
        return self.engine.tick(plan or self.plan)

    def test_detached_worker_commits_through_the_supervisor(self):
        state, _ = self.run_detached()
        self.assertEqual(state, 'progress')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='detached'").fetchone()
        self.assertEqual(row['status'], 'done')
        workspace = Path(self.engine.db.execute(
            "SELECT workspace FROM runs WHERE id='detached'").fetchone()[0])
        self.assertEqual(git(workspace, 'log', '-1', '--format=%s'), 'feat: answer')
        # The worker's usage was attributed to the supervisor's invocation record.
        invocation = self.engine.db.execute('SELECT * FROM invocations').fetchone()
        self.assertEqual(invocation['input_tokens'], 11)
        self.assertEqual(invocation['status'], 'ok')
        # No attempt or result file survives a completed reap.
        self.assertFalse(list((self.engine.home / 'attempts').glob('*.json')))

    def test_crash_before_spawn_leaves_no_owner_and_retries(self):
        run = self.engine.initialize(self.plan)
        workspace = Path(run['workspace'])
        with patch.object(Engine, 'spawn') as spawn:
            self.engine.launch_worker(self.plan, run, self.plan['tasks'][0], 1, workspace)
            self.assertTrue(spawn.called)
        self.engine.set_task('detached', 'answer', status='running')
        self.assertFalse(self.engine.workspace_owner_present(workspace))
        self.assertEqual(self.run_detached()[0], 'progress')
        self.assertTrue(self.engine.db.execute(
            "SELECT 1 FROM events WHERE kind='worker_lost'").fetchone())

    def test_result_written_before_a_supervisor_crash_is_reaped_not_repeated(self):
        run = self.engine.initialize(self.plan)
        workspace = Path(run['workspace'])
        record = self.engine.launch_worker(self.plan, run, self.plan['tasks'][0], 1, workspace,
                                           snapshot={'answer.py': None})
        # Simulate the supervisor dying after the worker's result landed.
        deadline = time.time() + 30
        while time.time() < deadline and not Path(record['result_path']).exists():
            time.sleep(0.05)
        self.assertTrue(Path(record['result_path']).exists())
        self.engine.set_task('detached', 'answer', status='running', attempts=1)
        state, _ = self.run_detached()
        self.assertEqual(state, 'progress')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='detached'").fetchone()
        self.assertEqual(row['status'], 'done')
        # The reaped result was accepted; it did not consume a second attempt.
        self.assertEqual(row['attempts'], 1)
        self.assertTrue(self.engine.db.execute(
            "SELECT 1 FROM events WHERE kind='reaped'").fetchone())

    def test_stale_result_from_another_attempt_is_rejected(self):
        run = self.engine.initialize(self.plan)
        workspace = Path(run['workspace'])
        record = self.engine.launch_worker(self.plan, run, self.plan['tasks'][0], 1, workspace)
        deadline = time.time() + 30
        while time.time() < deadline and not Path(record['result_path']).exists():
            time.sleep(0.05)
        payload = json.loads(Path(record['result_path']).read_text())
        payload['nonce'] = 'a-different-attempt'
        Path(record['result_path']).write_text(json.dumps(payload))
        self.engine.set_task('detached', 'answer', status='running')
        disposition, _ = self.engine.reconcile_attempt(run, self.plan['tasks'][0], workspace)
        self.assertEqual(disposition, 'free')
        self.assertTrue(self.engine.db.execute(
            "SELECT 1 FROM events WHERE kind='stale_result_rejected'").fetchone())

    def test_a_live_owner_blocks_replacement_dispatch(self):
        run = self.engine.initialize(self.plan)
        workspace = Path(run['workspace'])
        from loop.worker import workspace_lock_path
        import fcntl
        path = workspace_lock_path(self.engine.home, workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            record = self.engine.launch_worker(self.plan, run, self.plan['tasks'][0], 1, workspace)
            self.engine.set_task('detached', 'answer', status='running')
            disposition, held = self.engine.reconcile_attempt(run, self.plan['tasks'][0], workspace)
            self.assertEqual(disposition, 'recovering')
            self.assertEqual(held['attempt_id'], record['attempt_id'])
            state, deadline = self.run_detached()
        self.assertEqual(state, 'waiting')
        self.assertGreater(deadline, 0)
        # The blocked worker touched nothing in the shared worktree.
        self.assertEqual(git(workspace, 'status', '--porcelain'), '')

    def test_worker_refuses_a_workspace_it_does_not_own(self):
        run = self.engine.initialize(self.plan)
        workspace = Path(run['workspace'])
        record = self.engine.launch_worker(self.plan, run, self.plan['tasks'][0], 1, workspace)
        deadline = time.time() + 30
        while time.time() < deadline and not Path(record['result_path']).exists():
            time.sleep(0.05)
        from loop.worker import run_attempt, workspace_lock_path
        import fcntl
        with workspace_lock_path(self.engine.home, workspace).open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertEqual(run_attempt(self.engine.attempt_path(record['attempt_id'])), 3)


class SetupAndRegressionTests(unittest.TestCase):
    """Step 5: authorized setup recipes and successor regression repair."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'verify.py').write_text(
            "import pathlib\n"
            "assert pathlib.Path('.deps-ready').exists(), \"ModuleNotFoundError: No module named 'widgets'\"\n"
            "from answer import value\nassert value == 42\n")
        # A real recipe installs into an ignored directory; the worktree must
        # stay clean afterwards.
        (self.repo / '.gitignore').write_text('.deps-ready\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        self.plan = dict(id='setup', policy={'setup_recipes': [
            {'match': "no module named 'widgets'",
             'argv': ['python3', '-c', "open('.deps-ready','w').write('ready')"]}]},
            tasks=[dict(id='answer', provider='codex', files=['answer.py'],
                        prompt='Set value to 42', check=['python3', 'verify.py'])])

    def good(self, task, prompt, workspace):
        (workspace / task['files'][0]).write_text('value = 42\n')
        return Result('ok', 'Implemented', {'input_tokens': 1, 'output_tokens': 1})

    def test_authorized_recipe_resolves_a_missing_dependency(self):
        self.assertEqual(self.engine.tick(self.plan, self.good)[0], 'progress')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='setup'").fetchone()
        self.assertEqual(row['status'], 'pending')
        setup = self.engine.db.execute('SELECT * FROM setup_runs').fetchone()
        self.assertEqual(setup['status'], 'done')
        # With the dependency present the same task now completes.
        self.assertEqual(self.engine.tick(self.plan, self.good)[0], 'progress')
        self.assertEqual(self.engine.db.execute(
            "SELECT status FROM tasks WHERE run_id='setup'").fetchone()[0], 'done')

    def test_unauthorized_dependency_parks_with_an_environment_condition(self):
        plan = copy.deepcopy(self.plan)
        plan['policy'] = {'setup_recipes': []}
        for _ in range(2):
            state = self.engine.tick(plan, self.good)[0]
        self.assertEqual(state, 'parked')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='setup'").fetchone()
        self.assertEqual(row['wake_kind'], 'environment')
        self.assertFalse(self.engine.db.execute('SELECT 1 FROM setup_runs').fetchone())

    def test_an_unchanged_failed_recipe_is_not_retried(self):
        plan = copy.deepcopy(self.plan)
        plan['policy']['setup_recipes'] = [{'match': "no module named 'widgets'",
                                            'argv': ['python3', '-c', 'raise SystemExit(1)']}]
        self.engine.tick(plan, self.good)
        self.assertEqual(self.engine.db.execute(
            "SELECT status FROM setup_runs").fetchone()[0], 'failed')
        self.engine.tick(plan, self.good)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM setup_runs').fetchone()[0], 1)
        self.assertTrue(self.engine.db.execute(
            "SELECT 1 FROM events WHERE kind='setup_recipe_exhausted'").fetchone())

    def test_a_recipe_that_dirties_the_worktree_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan['policy']['setup_recipes'] = [{'match': "no module named 'widgets'",
                                            'argv': ['python3', '-c', "open('junk.py','w').write('x')"]}]
        self.engine.tick(plan, self.good)
        setup = self.engine.db.execute('SELECT * FROM setup_runs').fetchone()
        self.assertEqual(setup['status'], 'failed')
        workspace = Path(self.engine.db.execute(
            "SELECT workspace FROM runs WHERE id='setup'").fetchone()[0])
        self.assertEqual(git(workspace, 'status', '--porcelain'), '')


class RegressionRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'verify.py').write_text(
            'from answer import value\nfrom other import flag\n'
            'assert value == 42 and flag is True\n')
        # check_first passes when the milestone starts and is broken later by a
        # different task's accepted change.
        (self.repo / 'check_first.py').write_text(
            'from answer import value\nfrom other import flag\n'
            'assert value == 42 and flag is False\n')
        (self.repo / 'other.py').write_text('flag = False\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        self.plan = dict(id='regress', auto_replan=True, tasks=[
            dict(id='first', provider='codex', files=['answer.py'],
                 prompt='Set value to 42', check=['python3', 'check_first.py']),
            dict(id='second', provider='codex', files=['other.py'],
                 prompt='Set flag to True', check=['python3', 'verify.py'])])

    def test_regression_creates_a_successor_repair_task(self):
        def worker(task, prompt, workspace):
            if task['id'] == 'first':
                (workspace / 'answer.py').write_text('value = 42\n')
            else:
                (workspace / 'other.py').write_text('flag = True\n')
            return Result('ok', 'done', {'input_tokens': 1, 'output_tokens': 1})

        self.assertEqual(self.engine.tick(self.plan, worker)[0], 'progress')
        self.assertEqual(self.engine.tick(self.plan, worker)[0], 'progress')
        # Both tasks are accepted, but the milestone check for the first now
        # fails because of the second task's accepted change.
        state, _ = self.engine.tick(self.plan, worker)
        self.assertEqual(state, 'superseded')
        successor = self.engine.db.execute(
            "SELECT * FROM runs WHERE parent_id='regress'").fetchone()
        successor_plan = json.loads(successor['plan'])
        repair = successor_plan['tasks'][-1]
        self.assertTrue(repair['id'].startswith('repair-'))
        self.assertIn('other.py', repair['files'])
        # The trusted check is carried over, never rewritten by the repair task.
        self.assertEqual(repair['check'], ['python3', 'check_first.py'])
        self.assertNotIn('check_first.py', repair['files'])
        self.assertEqual(repair['depends_on'], ['first', 'second'])
        # Accepted work is preserved, not reopened.
        self.assertEqual(self.engine.db.execute(
            "SELECT status FROM tasks WHERE run_id=? AND id='second'",
            (successor['id'],)).fetchone()[0], 'done')

    def test_regression_repair_is_bounded_by_objective_history(self):
        run = self.engine.initialize(self.plan)
        workspace = Path(run['workspace'])
        task = self.plan['tasks'][0]
        with patch.object(Engine, 'regression_candidates', return_value=[]):
            self.assertIsNone(self.engine.repair_regression(
                self.plan, run, task, workspace, 'AssertionError: boom'))
        self.assertTrue(self.engine.db.execute(
            "SELECT 1 FROM events WHERE kind='regression_no_candidates'").fetchone())


class ContextBoundTests(unittest.TestCase):
    """Step 6: one bound over the whole prompt, not one per context file."""

    def test_sections_share_one_budget_and_trimming_is_visible(self):
        from loop.context import fit, model_budget
        assembled = fit([('instructions', 'A' * 100), ('context big.py', 'B' * 10000)], 4000)
        self.assertLessEqual(len(assembled), 4000)
        self.assertIn('A' * 100, assembled)
        self.assertIn('trimmed to fit the context budget', assembled)

    def test_output_space_is_reserved(self):
        from loop.context import fit
        self.assertLessEqual(len(fit([('one', 'x' * 100000)], 10000)), 7500 + 64)

    def test_budget_comes_from_policy_then_default(self):
        from loop.context import DEFAULT_CONTEXT_CHARACTERS, model_budget
        self.assertEqual(model_budget({}, None, None), DEFAULT_CONTEXT_CHARACTERS)
        self.assertEqual(model_budget({'context_characters': {'default': 50}}, None, None), 50)
        self.assertEqual(model_budget({'context_characters': {'m': 70, 'default': 50}}, 'm', None), 70)
        self.assertEqual(model_budget({'context_characters': {'default': 50}}, None, 999), 999)

    def test_many_context_files_cannot_exceed_the_bound(self):
        from loop.worker import build_prompt
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            names = []
            for index in range(20):
                name = 'file%d.py' % index
                (workspace / name).write_text('y' * 30000)
                names.append(name)
            record = {'workspace': str(workspace), 'context_characters': 20000,
                      'previous_error': 'boom',
                      'task': {'files': ['answer.py'], 'prompt': 'do it',
                               'context_files': names}}
            prompt = build_prompt(record)
        self.assertLessEqual(len(prompt), 20000)
        self.assertIn('do it', prompt)
        self.assertIn('omitted for context budget', prompt)


class HealthAndRoutingTests(unittest.TestCase):
    """Step 6: measured routing, censored timeouts, and bounded tuning."""

    def setUp(self):
        self.directory = isolate_registry(self)
        self.registry = Registry(self.directory / 'registry.sqlite')
        self.addCleanup(self.registry.close)
        self.db = self.registry.db
        health.ensure_schema(self.db)

    def sample(self, model, outcome, tokens, **extra):
        health.record_sample(self.db, provider='codex', model=model, effort='low',
                             task_class='c', outcome=outcome, tokens=tokens, **extra)

    def test_routing_prefers_measured_tokens_to_acceptance(self):
        for _ in range(6):
            self.sample('cheap-but-failing', 'error', 4000)
            self.sample('cheap-but-failing', 'ok', 4000)
            self.sample('reliable', 'ok', 3000)
        models = [{'provider': 'codex', 'model': 'cheap-but-failing', 'effort': 'low'},
                  {'provider': 'codex', 'model': 'reliable', 'effort': 'low'}]
        choice, reason = health.choose_route(self.db, models, 'c')
        self.assertEqual(choice['model'], 'reliable')
        self.assertIn('tokens per acceptance', reason)

    def test_unmeasured_routes_are_unknown_not_preferred(self):
        for _ in range(6):
            self.sample('measured', 'ok', 1000)
        models = [{'provider': 'codex', 'model': 'measured', 'effort': 'low'},
                  {'provider': 'codex', 'model': 'never-tried', 'effort': 'low'}]
        choice, _ = health.choose_route(self.db, models, 'c')
        self.assertEqual(choice['model'], 'measured')

    def test_a_route_below_the_success_floor_is_excluded(self):
        for _ in range(10):
            self.sample('broken', 'error', 1000)
        for _ in range(6):
            self.sample('working', 'ok', 9000)
        models = [{'provider': 'codex', 'model': 'broken', 'effort': 'low'},
                  {'provider': 'codex', 'model': 'working', 'effort': 'low'}]
        choice, _ = health.choose_route(self.db, models, 'c')
        self.assertEqual(choice['model'], 'working')

    def test_censored_timeouts_cannot_train_the_timeout_downward(self):
        for _ in range(10):
            health.record_sample(self.db, provider='codex', model='m', effort='low',
                                 task_class='c', outcome='transient', duration=180,
                                 censored=True)
        self.assertGreaterEqual(health.learned_timeout(self.db, 'codex', 'm', 'low', 'c', 180), 180)

    def test_learned_probe_interval_respects_floor_and_ceiling(self):
        for _ in range(5):
            health.record_recovery(self.db, 'codex', 'a', 'quota', 1)
        self.assertEqual(health.learned_interval(self.db, 'codex', 'a', 'quota'),
                         health.MIN_PROBE_SECONDS)
        for _ in range(5):
            health.record_recovery(self.db, 'claude', 'a', 'quota', 10 ** 9)
        self.assertEqual(health.learned_interval(self.db, 'claude', 'a', 'quota'),
                         health.MAX_PROBE_SECONDS)

    def test_tuning_is_bounded_per_period_and_revertible(self):
        first = health.apply_setting(self.db, 'worker_timeout', 10000, 100)
        self.assertEqual(first, 125)
        second = health.apply_setting(self.db, 'worker_timeout', 10000, 100)
        self.assertEqual(second, 150)
        self.assertEqual(health.revert_setting(self.db, 'worker_timeout'), 100)
        self.assertEqual(health.setting(self.db, 'worker_timeout', 0), 100)


class ProviderRecoveryTests(unittest.TestCase):
    """Step 6: waiting ends through probes, not through another task succeeding."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'verify.py').write_text('from answer import value\nassert value == 42\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        self.plan = dict(id='recover', probe_interval_seconds=60,
                         tasks=[dict(id='answer', provider='codex', files=['answer.py'],
                                     prompt='Set value to 42', check=['python3', 'verify.py'])])

    def good(self, task, prompt, workspace):
        (workspace / task['files'][0]).write_text('value = 42\n')
        return Result('ok', 'Implemented', {'input_tokens': 1, 'output_tokens': 1})

    def test_every_credential_failing_waits_and_recovers_without_another_task(self):
        def unauthenticated(*_):
            return Result('auth_required', '', {}, error='Not logged in')
        state, deadline = self.engine.tick(self.plan, unauthenticated)
        self.assertEqual(state, 'waiting')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='recover'").fetchone()
        self.assertEqual(row['status'], 'waiting')
        self.assertEqual(row['wake_kind'], 'credentials')
        provider = self.engine.db.execute("SELECT * FROM providers WHERE name='codex'").fetchone()
        self.assertTrue(provider['reason'].startswith('auth:'))
        self.assertGreater(provider['held_since'], 0)
        # The probe, not another successful task, is what ends the wait.
        with patch('loop.engine.auth_probe', return_value=True):
            self.engine.recover_providers(self.plan, time.time() + 120)
        provider = self.engine.db.execute("SELECT * FROM providers WHERE name='codex'").fetchone()
        self.assertEqual(provider['retry_at'], 0)
        self.assertTrue(self.engine.db.execute(
            "SELECT 1 FROM events WHERE kind='provider_recovered'").fetchone())
        self.assertEqual(self.engine.tick(self.plan, self.good)[0], 'progress')

    def test_an_unknown_probe_reschedules_rather_than_assuming_recovery(self):
        self.engine.hold('codex', time.time() + 1, 'auth: not logged in')
        with patch('loop.engine.auth_probe', return_value=None):
            self.engine.recover_providers(self.plan, time.time() + 120)
        provider = self.engine.db.execute("SELECT * FROM providers WHERE name='codex'").fetchone()
        self.assertNotEqual(provider['retry_at'], 0)
        self.assertGreater(provider['probe_at'], time.time())

    def test_routing_records_its_reason_and_selection(self):
        plan = copy.deepcopy(self.plan)
        plan['auto_route'] = True
        plan['policy'] = {'models': [{'provider': 'codex', 'model': 'small', 'effort': 'low'},
                                     {'provider': 'codex', 'model': 'large', 'effort': 'high'}]}
        self.assertEqual(self.engine.tick(plan, self.good)[0], 'progress')
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='recover'").fetchone()
        self.assertEqual(row['selected_model'], 'small')
        decision = self.engine.db.execute(
            "SELECT * FROM decisions WHERE chosen='small'").fetchone()
        self.assertIn('cold start', decision['reason'])


class ServiceTests(unittest.TestCase):
    """Step 7: durable queue, deterministic ticks, and release reconciliation."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'verify.py').write_text('from answer import value\nassert value == 42\n')
        (self.repo / 'verify_two.py').write_text('from second import value\nassert value == 7\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        self.service = Service(self.engine)
        self.plans = Path(self.temp.name) / 'plans'
        self.plans.mkdir()

    def write_plan(self, name, file_name, check):
        plan = dict(id=name, tasks=[dict(id='task', provider='codex', files=[file_name],
                                         prompt='implement', check=check)])
        path = self.plans / (name + '.json')
        path.write_text(json.dumps(plan))
        return path

    def test_queue_is_durable_and_fair(self):
        first = self.write_plan('alpha', 'answer.py', ['python3', 'verify.py'])
        second = self.write_plan('beta', 'second.py', ['python3', 'verify_two.py'])
        self.service.enqueue(second, priority=50)
        self.service.enqueue(first, priority=10)
        # A fresh Service over a reopened engine sees the same queue.
        reopened = Engine(self.repo)
        self.addCleanup(reopened.db.close)
        self.assertEqual([row['id'] for row in Service(reopened).objectives()],
                         ['alpha', 'beta'])
        self.assertEqual(Service(reopened).eligible(time.time())['id'], 'alpha')

    def test_a_parked_objective_does_not_stop_the_queue(self):
        broken = self.write_plan('broken', 'answer.py', ['python3', '-c', 'raise SystemExit(1)'])
        working = self.write_plan('working', 'second.py', ['python3', 'verify_two.py'])
        self.service.enqueue(broken, priority=10)
        self.service.enqueue(working, priority=20)

        def worker(task, prompt, workspace):
            (workspace / task['files'][0]).write_text(
                'value = 42\n' if task['files'][0] == 'answer.py' else 'value = 7\n')
            return Result('ok', 'done', {'input_tokens': 1, 'output_tokens': 1})

        with patch.object(Engine, 'tick', autospec=True) as tick:
            tick.side_effect = lambda engine, plan, *a, **k: (
                ('parked', 0) if plan['id'] == 'broken' else ('ready_for_pr', 0))
            self.assertEqual(self.service.tick()[0], 'parked')
            self.assertEqual(self.service.tick()[0], 'locally_complete')
        rows = {row['id']: row['status'] for row in self.service.objectives()}
        self.assertEqual(rows, {'broken': 'parked', 'working': 'locally_complete'})
        # With nothing eligible left, the service sleeps rather than inventing work.
        state, deadline = self.service.tick()
        self.assertEqual(state, 'idle')
        self.assertGreater(deadline, time.time())

    def test_local_completion_published_and_merged_are_distinct(self):
        plan = self.write_plan('release', 'answer.py', ['python3', 'verify.py'])
        self.service.enqueue(plan, release={'repository': 'owner/name', 'base': 'main'})
        with patch.object(Engine, 'tick', return_value=('ready_for_pr', 0)), \
                patch('loop.release.publish', return_value={'state': 'pr_open',
                                                            'url': 'https://example.invalid/1',
                                                            'number': 1}):
            self.assertEqual(self.service.tick()[0], 'published')
        self.assertEqual(self.service.release_state('release')['state'], 'pr_open')
        with patch.object(Engine, 'tick', return_value=('ready_for_pr', 0)), \
                patch('loop.release.publish', return_value={'state': 'merged',
                                                            'url': 'https://example.invalid/1',
                                                            'number': 1}):
            self.service.set_objective('release', status='running')
            self.assertEqual(self.service.tick()[0], 'merged')
        self.assertEqual(self.service.release_state('release')['state'], 'merged')

    def test_a_changed_head_invalidates_recorded_check_evidence(self):
        self.service.record_release('run', 'owner/name', 'main', 'waiting_for_checks',
                                    {'number': 3, 'url': 'u', 'headRefOid': 'aaa'})
        self.assertFalse(self.service.invalidate_on_head_change('run', 'aaa'))
        self.assertTrue(self.service.invalidate_on_head_change('run', 'bbb'))
        self.assertEqual(self.service.release_state('run')['state'], 'pending')

    def test_a_busy_milestone_backs_off_instead_of_spinning(self):
        plan = self.write_plan('busy', 'answer.py', ['python3', 'verify.py'])
        self.service.enqueue(plan)
        with patch.object(Engine, 'tick', side_effect=RuntimeError('Another supervisor is active')):
            state, deadline = self.service.tick()
        self.assertEqual(state, 'waiting')
        self.assertGreater(deadline, time.time())
        row = self.service.objectives()[0]
        self.assertEqual(row['attempts'], 1)
        self.assertIn('Another supervisor', row['reason'])

    def test_the_loop_heartbeats_and_stops_on_request(self):
        ticks = []
        with patch.object(Service, 'tick', side_effect=lambda now=None: (
                ticks.append(now) or ('idle', (now or 0) + 1))):
            self.service.run_forever(stop=lambda: len(ticks) >= 3, sleep=lambda _: None,
                                     now=lambda: 1000.0)
        self.assertEqual(len(ticks), 3)
        heartbeat = json.loads((self.engine.home / 'service-heartbeat.json').read_text())
        self.assertEqual(heartbeat['at'], 1000.0)

    def test_launchd_definition_keeps_the_service_alive(self):
        plist = launchd_plist(self.repo, 'com.example.loop')
        self.assertIn('<key>KeepAlive</key>', plist)
        self.assertIn('com.example.loop', plist)
        self.assertIn(str(self.repo), plist)


class LearningTests(unittest.TestCase):
    """Step 8: incremental, deduplicated learning with measured promotion."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'verify.py').write_text('from answer import value\nassert value == 42\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        self.db = self.engine.registry.db
        learning.ensure_schema(self.db)

    def observe(self, event_id, objective, fingerprint='fp'):
        return learning.observe(self.db, signal_kind='failed_strategy', scope='s',
                                fingerprint=fingerprint, event_id=event_id,
                                objective_id=objective)

    def test_reprocessing_an_event_cannot_count_as_another_occurrence(self):
        self.observe('e1', 'one')
        row = self.observe('e1', 'one')
        self.assertEqual(row['occurrences'], 1)
        self.assertEqual(self.observe('e2', 'one')['occurrences'], 2)

    def test_successor_retries_of_one_failure_do_not_corroborate(self):
        for index in range(5):
            self.observe('e%d' % index, 'same-objective')
        self.assertEqual(learning.promote_candidates(self.db), [])
        self.observe('other-1', 'second-objective')
        self.assertEqual(len(learning.promote_candidates(self.db)), 1)

    def test_learning_is_incremental_across_calls(self):
        self.engine.event('run', 'task', 'parked_no_progress', 'boom')
        first = learning.learn(self.engine)
        self.assertEqual(first['events'], 1)
        second = learning.learn(self.engine)
        self.assertEqual(second['events'], 0)
        self.engine.event('run', 'task', 'parked_no_progress', 'boom')
        self.assertEqual(learning.learn(self.engine)['events'], 1)
        lesson = self.db.execute('SELECT * FROM lessons').fetchone()
        self.assertEqual(lesson['occurrences'], 2)
        self.assertEqual(lesson['signal_kind'], 'failed_strategy')

    def start_trial(self, baseline):
        for index in range(3):
            self.observe('e%d' % index, 'objective-%d' % (index % 2))
        identifier = learning.promote_candidates(self.db)[0]
        self.assertTrue(learning.start_trial(self.db, identifier, baseline))
        return identifier

    def apply(self, identifier, outcomes, tokens=100):
        for index, outcome in enumerate(outcomes):
            learning.record_application(self.db, identifier, 'run', 't%d' % index, outcome, tokens)

    def test_a_trial_needs_ten_terminal_tasks_with_known_accounting(self):
        identifier = self.start_trial({'tokens_per_acceptance': 200, 'completion_rate': 1.0})
        self.apply(identifier, ['ok'] * 9)
        self.assertIsNone(learning.evaluate_trial(self.db, identifier))
        self.apply(identifier, ['ok'])
        self.assertEqual(learning.evaluate_trial(self.db, identifier), learning.ACTIVE)

    def test_unknown_usage_prevents_promotion(self):
        identifier = self.start_trial({'tokens_per_acceptance': 200, 'completion_rate': 1.0})
        self.apply(identifier, ['ok'] * 10)
        learning.record_application(self.db, identifier, 'run', 'unknown', 'ok', None)
        self.assertIsNone(learning.evaluate_trial(self.db, identifier))
        row = self.db.execute('SELECT status FROM lessons WHERE id=?', (identifier,)).fetchone()
        self.assertEqual(row['status'], learning.TRIAL)

    def test_a_trial_that_spends_more_is_rejected(self):
        identifier = self.start_trial({'tokens_per_acceptance': 50, 'completion_rate': 1.0})
        self.apply(identifier, ['ok'] * 10, tokens=100)
        self.assertEqual(learning.evaluate_trial(self.db, identifier), learning.REJECTED)
        self.assertFalse(learning.retrieve(self.db))

    def test_failures_and_parks_count_inside_the_trial_window(self):
        identifier = self.start_trial({'tokens_per_acceptance': 1000, 'completion_rate': 1.0})
        self.apply(identifier, ['ok'] * 10 + ['parked'] * 4)
        self.assertEqual(learning.evaluate_trial(self.db, identifier), learning.REJECTED)

    def test_only_one_trial_runs_at_a_time(self):
        first = self.start_trial({'tokens_per_acceptance': 100, 'completion_rate': 1.0})
        for index in range(3):
            self.observe('other-%d' % index, 'objective-%d' % (index % 2), fingerprint='other')
        others = [row for row in learning.promote_candidates(self.db) if row != first]
        self.assertTrue(others)
        self.assertFalse(learning.start_trial(self.db, others[0], {}))

    def test_active_lessons_are_retrievable_and_disableable(self):
        identifier = self.start_trial({'tokens_per_acceptance': 1000, 'completion_rate': 0.5})
        self.apply(identifier, ['ok'] * 10)
        self.assertEqual(learning.evaluate_trial(self.db, identifier), learning.ACTIVE)
        self.assertEqual(len(learning.retrieve(self.db, signal_kind='failed_strategy')), 1)
        learning.disable(self.db, identifier, 'increased failures')
        self.assertFalse(learning.retrieve(self.db))


class ImprovementTests(unittest.TestCase):
    """Step 9: fenced surfaces, pinned evaluation, canary and rollback."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'seed.txt').write_text('seed\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        self.db = self.engine.registry.db
        improve.ensure_schema(self.db)
        learning.ensure_schema(self.db)
        self.baseline = Path(self.temp.name) / 'baseline'
        self.candidate = Path(self.temp.name) / 'candidate'
        source = Path(__file__).resolve().parent.parent
        for root in (self.baseline, self.candidate):
            (root / 'loop').mkdir(parents=True)
            (root / 'tests' / 'fixtures').mkdir(parents=True)
            for name in improve.FENCED_PATHS + tuple(
                    path for path, _ in improve.FENCED_FUNCTIONS):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((source / name).read_bytes())
            (root / 'loop' / 'adapters.py').write_text('# adapter\n')

    def corroborated(self, kind='adapter_defect', fingerprint='fp'):
        for index in range(3):
            learning.observe(self.db, signal_kind=kind, scope='s', fingerprint=fingerprint,
                             event_id='%s-%d' % (fingerprint, index),
                             objective_id='objective-%d' % (index % 2),
                             proposed_action='fix the adapter')
        learning.promote_candidates(self.db)

    def test_only_one_improvement_trial_is_in_flight(self):
        self.corroborated()
        first, _ = improve.propose(self.engine)
        self.assertTrue(first)
        second, reason = improve.propose(self.engine)
        self.assertIsNone(second)
        self.assertIn('already in flight', reason)

    def test_a_cheaper_validated_lesson_is_preferred_over_a_code_change(self):
        self.corroborated(fingerprint='shared')
        for index in range(3):
            learning.observe(self.db, signal_kind='routing', scope='s', fingerprint='shared',
                             event_id='routing-%d' % index,
                             objective_id='objective-%d' % (index % 2))
        self.db.execute("UPDATE lessons SET status='active' WHERE signal_kind='routing'")
        identifier, reason = improve.propose(self.engine)
        self.assertIsNone(identifier)
        self.assertIn('no corroborated evidence', reason)

    def test_a_candidate_that_edits_a_fenced_surface_is_ineligible(self):
        (self.candidate / 'loop' / 'policy.py').write_text('# weakened\n')
        report = improve.evaluate(self.engine, self.candidate, self.baseline)
        self.assertFalse(report['eligible'])
        self.assertIn('loop/policy.py', report['reason'])

    def test_a_candidate_that_rewrites_an_enforcement_function_is_ineligible(self):
        text = (self.candidate / 'loop' / 'engine.py').read_text()
        weakened = text.replace('def safe_path(root, name):',
                                'def safe_path(root, name):\n    return root / name')
        self.assertNotEqual(text, weakened)
        (self.candidate / 'loop' / 'engine.py').write_text(weakened)
        report = improve.evaluate(self.engine, self.candidate, self.baseline)
        self.assertFalse(report['eligible'])
        self.assertIn('safe_path', report['reason'])

    def test_missing_independent_fixtures_are_ineligible_not_successful(self):
        (self.baseline / 'tests' / 'fixtures' / 'manifest.json').unlink()
        report = improve.evaluate(self.engine, self.candidate, self.baseline)
        self.assertFalse(report['eligible'])
        self.assertIn('no pinned independent fixture manifest', report['reason'])

    def test_a_regressed_fixture_blocks_promotion(self):
        manifest = {'thresholds': {'regressed_fixtures': 0},
                    'fixtures': [{'id': 'always', 'argv': ['python3', '-c', 'pass']},
                                 {'id': 'candidate-only',
                                  'argv': ['python3', '-c',
                                           "import pathlib,sys;"
                                           "sys.exit(1 if pathlib.Path('broken').exists() else 0)"]}]}
        for root in (self.baseline, self.candidate):
            # The manifest is pinned: both installations carry the identical file.
            (root / 'tests' / 'fixtures' / 'manifest.json').write_text(json.dumps(manifest))
        (self.candidate / 'broken').write_text('x')
        report = improve.evaluate(self.engine, self.candidate, self.baseline)
        self.assertFalse(report['eligible'])
        self.assertIn('candidate-only', report['reason'])

    def test_an_eligible_candidate_passes_the_pinned_fixtures(self):
        manifest = {'thresholds': {'regressed_fixtures': 0},
                    'fixtures': [{'id': 'always', 'argv': ['python3', '-c', 'pass']}]}
        for root in (self.baseline, self.candidate):
            (root / 'tests' / 'fixtures' / 'manifest.json').write_text(json.dumps(manifest))
        report = improve.evaluate(self.engine, self.candidate, self.baseline)
        self.assertTrue(report['eligible'], report['reason'])

    def test_runs_keep_their_assigned_version_across_a_rollback(self):
        self.db.execute("INSERT INTO installations(version,path,sha,role,created_at) "
                        "VALUES ('baseline','/b','aaa','active',1)")
        self.db.execute("INSERT INTO installations(version,path,sha,role,created_at) "
                        "VALUES ('candidate','/c','bbb','canary',2)")
        self.corroborated()
        trial, lesson = improve.propose(self.engine)
        self.db.execute("UPDATE improvement_trials SET version='candidate' WHERE id=?", (trial,))
        self.assertEqual(improve.assign_version(self.engine, 'run-1', 'candidate'), 'candidate')
        # A second assignment never moves a run that is already executing.
        self.assertEqual(improve.assign_version(self.engine, 'run-1', 'baseline'), 'candidate')
        improve.promote(self.engine, 'candidate')
        self.assertEqual(improve.active_version(self.engine), 'candidate')
        report = improve.rollback(self.engine, 'candidate', 'canary regression')
        self.assertEqual(report['active'], 'baseline')
        self.assertEqual(report['runs_still_owned_by_candidate'], ['run-1'])
        self.assertEqual(self.db.execute(
            'SELECT status FROM lessons WHERE id=?', (lesson,)).fetchone()['status'],
            learning.ROLLED_BACK)

    def test_the_improvement_allowlist_excludes_every_fenced_file(self):
        allowed = improve.improvement_allowlist(Path(__file__).resolve().parent.parent)
        self.assertIn('loop/adapters.py', allowed)
        for name in improve.FENCED_PATHS:
            self.assertNotIn(name, allowed)

    def test_the_shipped_manifest_is_valid_and_pinned(self):
        manifest = improve.load_manifest(Path(__file__).resolve().parent.parent)
        self.assertIsNotNone(manifest)
        self.assertIn('tests/fixtures/manifest.json', improve.FENCED_PATHS)


class ProviderDenialTests(unittest.TestCase):
    """Regression: a denied worker action is not a successful turn.

    Found by a live three-provider run, not by a fixture: antigravity reported
    status SUCCESS with an empty response while its sandbox had denied every
    action, so the supervisor spent a second attempt and an advisory review
    call rediscovering that no file had been written.
    """

    def denied(self, **extra):
        payload = {'status': 'SUCCESS', 'response': '', 'num_turns': 1,
                   'usage': {'input_tokens': 15284, 'output_tokens': 70},
                   'denied_actions': [{'action': 'command', 'display_name': 'RunCommand'}]}
        payload.update(extra)
        return json.dumps(payload)

    def test_denied_actions_are_not_reported_as_success(self):
        result = parse('antigravity', 0, self.denied(), '')
        self.assertEqual(result.status, 'error')
        self.assertIn('RunCommand', result.error)
        self.assertIn('denied', result.error.lower())

    def test_a_denial_is_classified_as_a_permission_fault(self):
        from loop.diagnosis import PERMISSION, classify
        self.assertEqual(classify(parse('antigravity', 0, self.denied(), '').error), PERMISSION)

    def test_an_empty_denial_list_still_succeeds(self):
        result = parse('antigravity', 0, self.denied(denied_actions=[], response='done'), '')
        self.assertEqual(result.status, 'ok')

    def test_usage_is_retained_for_a_denied_run(self):
        # The invocation happened and was billed; only its outcome was a fault.
        self.assertEqual(parse('antigravity', 0, self.denied(), '').usage['input_tokens'], 15284)

    def test_the_sandbox_posture_is_explicit_in_the_command(self):
        self.assertIn('--sandbox', command('antigravity', 'p'))
        self.assertNotIn('--sandbox', command('antigravity', 'p', sandbox=False))


class HeadroomRoutingTests(unittest.TestCase):
    """Quota gates eligibility, but a nearly-exhausted provider is also a stall."""

    def setUp(self):
        self.directory = isolate_registry(self)
        self.registry = Registry(self.directory / 'registry.sqlite')
        self.addCleanup(self.registry.close)
        self.db = self.registry.db
        health.ensure_schema(self.db)
        self.models = [{'provider': 'claude', 'model': 'haiku', 'effort': 'low'},
                       {'provider': 'antigravity', 'model': 'flash', 'effort': 'low'}]

    def measure(self, provider, model, tokens, count=6, outcome='ok'):
        for _ in range(count):
            health.record_sample(self.db, provider=provider, model=model, effort='low',
                                 task_class='c', outcome=outcome, tokens=tokens)

    def test_usage_fraction_reads_the_binding_window(self):
        self.assertEqual(usage_fraction(
            {'rate_limits': {'session': {'usedPercent': 18}, 'week': {'usedPercent': 51}}}), 0.51)
        self.assertIsNone(usage_fraction({'rate_limits': {}}))

    def test_an_exhausted_provider_loses_even_when_it_is_more_efficient(self):
        self.measure('claude', 'haiku', 1000)          # cheaper per acceptance
        self.measure('antigravity', 'flash', 9000)     # dearer, but has room
        choice, reason = health.choose_route(
            self.db, self.models, 'c', headroom={'claude': 0.02, 'antigravity': 0.97})
        self.assertEqual(choice['provider'], 'antigravity')
        self.assertIn('headroom', reason)

    def test_efficiency_still_ranks_when_both_have_room(self):
        self.measure('claude', 'haiku', 1000)
        self.measure('antigravity', 'flash', 9000)
        choice, _ = health.choose_route(
            self.db, self.models, 'c', headroom={'claude': 0.8, 'antigravity': 0.9})
        self.assertEqual(choice['provider'], 'claude')

    def test_unknown_headroom_is_usable_but_not_evidence_of_capacity(self):
        self.measure('claude', 'haiku', 1000)
        self.measure('antigravity', 'flash', 9000)
        # antigravity has measured room; claude's headroom is unknown. Unknown
        # must not be excluded, and must not win on the strength of not knowing.
        choice, _ = health.choose_route(
            self.db, self.models, 'c', headroom={'antigravity': 0.9})
        self.assertIn(choice['provider'], ('claude', 'antigravity'))
        # With every provider low, the run still proceeds rather than stalling.
        choice, _ = health.choose_route(
            self.db, self.models, 'c', headroom={'claude': 0.01, 'antigravity': 0.01})
        self.assertIsNotNone(choice)


class FailoverTests(unittest.TestCase):
    """A long wait is not free: finish the same task on another provider."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.repo = Path(self.temp.name).resolve() / 'target'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'verify.py').write_text('from answer import value\nassert value == 42\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')
        self.engine = Engine(self.repo)
        self.addCleanup(self.engine.db.close)
        # Headroom polling spawns provider CLIs; this suite is about scheduling.
        headroom = patch.object(Engine, 'refresh_headroom', return_value=None)
        headroom.start()
        self.addCleanup(headroom.stop)
        self.plan = dict(id='failover', failover_after_seconds=3600, tasks=[
            dict(id='answer', provider='claude', model='haiku', effort='low',
                 files=['answer.py'], prompt='Set value to 42',
                 check=['python3', 'verify.py'])])
        self.plan['policy'] = {'models': [
            {'provider': 'claude', 'model': 'haiku', 'effort': 'low'},
            {'provider': 'antigravity', 'model': 'flash', 'effort': 'low'}]}

    def good(self, task, prompt, workspace):
        self.used.append(task['provider'])
        (workspace / task['files'][0]).write_text('value = 42\n')
        return Result('ok', 'done', {'input_tokens': 1, 'output_tokens': 1})

    def test_a_long_wait_switches_provider_and_continues_the_same_task(self):
        self.used = []
        now = time.time()
        self.engine.initialize(self.plan)
        # Bank one failed attempt so we can prove history is carried, not reset.
        self.engine.set_task('failover', 'answer', attempts=2, error='previous diagnostic')
        self.engine.hold('claude', now + 4 * 3600, 'session limit', now)
        state, _ = self.engine.tick(self.plan, self.good, now=now)
        self.assertEqual(state, 'progress')
        self.assertEqual(self.used, ['antigravity'])
        row = self.engine.db.execute("SELECT * FROM tasks WHERE run_id='failover'").fetchone()
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['selected_provider'], 'antigravity')
        # Continued, not restarted: the attempt history survived the switch.
        self.assertEqual(row['attempts'], 3)
        decision = self.engine.db.execute(
            "SELECT * FROM decisions WHERE chosen='antigravity'").fetchone()
        self.assertIn('unavailable', decision['question'])
        self.assertTrue(self.engine.db.execute(
            "SELECT 1 FROM events WHERE kind='failover'").fetchone())

    def test_a_short_wait_still_waits(self):
        self.used = []
        now = time.time()
        self.engine.initialize(self.plan)
        self.engine.hold('claude', now + 120, 'brief backoff', now)
        state, deadline = self.engine.tick(self.plan, self.good, now=now)
        self.assertEqual(state, 'waiting')
        self.assertEqual(self.used, [])
        self.assertAlmostEqual(deadline, now + 120, delta=1)

    def test_failover_never_uses_a_provider_that_is_also_held(self):
        self.used = []
        now = time.time()
        self.engine.initialize(self.plan)
        self.engine.hold('claude', now + 4 * 3600, 'session limit', now)
        self.engine.hold('antigravity', now + 5 * 3600, 'also exhausted', now)
        state, deadline = self.engine.tick(self.plan, self.good, now=now)
        self.assertEqual(state, 'waiting')
        self.assertEqual(self.used, [])
        self.assertTrue(self.engine.db.execute(
            "SELECT 1 FROM events WHERE kind='failover_unavailable'").fetchone())

    def test_with_no_configured_alternative_it_waits(self):
        self.used = []
        now = time.time()
        plan = copy.deepcopy(self.plan)
        plan['policy']['models'] = [{'provider': 'claude', 'model': 'haiku', 'effort': 'low'}]
        self.engine.initialize(plan)
        self.engine.hold('claude', now + 4 * 3600, 'session limit', now)
        self.assertEqual(self.engine.tick(plan, self.good, now=now)[0], 'waiting')
        self.assertEqual(self.used, [])

    def test_the_threshold_is_configurable(self):
        self.used = []
        now = time.time()
        plan = copy.deepcopy(self.plan)
        plan['failover_after_seconds'] = 60
        self.engine.initialize(plan)
        self.engine.hold('claude', now + 120, 'short by default, long for this plan', now)
        self.assertEqual(self.engine.tick(plan, self.good, now=now)[0], 'progress')
        self.assertEqual(self.used, ['antigravity'])
