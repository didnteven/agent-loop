import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from loop.adapters import Result, command, parse, quota_deadline, run_process, token_total
from loop.engine import Engine, git, safe_path
from loop.release import checks_pass, validate_remote


class AdapterTests(unittest.TestCase):
    def test_codex_usage_does_not_double_count_cache(self):
        result = parse("codex", 0, '\n'.join([
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":80,"output_tokens":5}}']), "")
        self.assertEqual(result.status, "ok")
        self.assertEqual(token_total("codex", result.usage), 105)

    def test_false_success_is_not_completion(self):
        result = parse("claude", 0, '{"type":"result","subtype":"success","is_error":true,"result":"Not logged in"}', "")
        self.assertEqual(result.status, "auth_required")
        self.assertEqual(parse("antigravity", 0, '{"status":"WAITING"}', "").status, "error")
        self.assertEqual(parse("codex", 0, "", "").status, "error")

    def test_weekly_window_controls_wait(self):
        self.assertEqual(quota_deadline({"primary": {"usedPercent":100,"resetsAt":200},
                                        "secondary":{"usedPercent":100,"resetsAt":900}}, 100), 905)
        self.assertEqual(quota_deadline({"used_percentage":99,"resets_at":900}, 100), 0)
        self.assertEqual(quota_deadline({"used_percentage":100,"resets_at":90}, 100), 0)

    def test_unknown_quota_does_not_invent_reset(self):
        result = parse("claude", 1, '{"type":"result","is_error":true,"result":"You hit your weekly limit"}', "", 100)
        self.assertEqual(result.status, "rate_limited")
        self.assertEqual(result.retry_at, 0)

    def test_arguments_are_not_shell_code(self):
        prompt = 'literal $(whoami) `date` "quotes"'
        self.assertIn(prompt, command("claude", prompt))

    def test_timeout_is_bounded(self):
        code, _, _ = run_process(["python3", "-c", "import time; time.sleep(10)"], "/private/tmp", 0.05)
        self.assertEqual(code, 124)

    def test_merge_requires_real_passing_checks(self):
        self.assertFalse(checks_pass([]))
        self.assertFalse(checks_pass([{"bucket":"pass"}, {"bucket":"pending"}]))
        self.assertFalse(checks_pass([{"bucket":"skipping"}]))
        self.assertFalse(checks_pass([{"bucket":"fail"}]))
        self.assertTrue(checks_pass([{"bucket":"pass"}]))

    def test_release_remote_must_match(self):
        validate_remote("git@github.com:todd/test.git", "todd/test")
        validate_remote("https://github.com/todd/test.git", "todd/test")
        with self.assertRaises(ValueError):
            validate_remote("https://github.com/todd/production.git", "todd/test")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / ".gitignore").write_text(".agent-loop/\n__pycache__/\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-qm", "initial")
        self.engine = Engine(self.repo)
        self.plan = {"id":"test", "tasks":[{"id":"one", "provider":"codex", "files":["answer.py"],
                     "prompt":"write answer", "check":["python3","-c","from answer import add; assert add(2,3)==5"]}]}
        self.calls = 0

    def tearDown(self):
        self.engine.db.close()
        self.temp.cleanup()

    def good_worker(self, *_):
        self.calls += 1
        return Result("ok", json.dumps({"files":[{"path":"answer.py","content":"def add(a,b): return a+b\n"}]}), {"input_tokens":50,"output_tokens":10})

    def test_commit_restart_and_pr_body(self):
        with self.engine.lock():
            self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "progress")
        self.engine.db.close()
        self.engine = Engine(self.repo)
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "ready_for_pr")
        self.assertEqual(self.calls, 1)
        self.assertFalse((self.repo / "answer.py").exists())
        self.assertTrue((self.engine.home / "test-pr.md").exists())
        self.assertEqual(self.engine.status()["tasks"][0]["status"], "done")

    def test_quota_wait_survives_restart_without_worker_calls(self):
        def limited(*_):
            self.calls += 1
            return Result("rate_limited", retry_at=500)
        self.assertEqual(self.engine.tick(self.plan, limited, now=100), ("waiting",500))
        self.engine.db.close()
        self.engine = Engine(self.repo)
        for now in (101,200,499):
            self.assertEqual(self.engine.tick(self.plan, self.good_worker, now), ("waiting",500))
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.engine.tick(self.plan, self.good_worker, now=501)[0], "progress")
        self.assertEqual(self.calls, 2)

    def test_commit_reconciles_database_crash(self):
        self.engine.tick(self.plan, self.good_worker)
        self.engine.set_task("test", "one", status="running", sha=None)
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "ready_for_pr")
        self.assertEqual(self.calls, 1)

    def test_bad_code_is_not_committed_and_retry_is_bounded(self):
        def bad(*_):
            return Result("ok", '{"files":[{"path":"answer.py","content":"def add(a,b): return 0"}]}')
        for _ in range(3):
            self.engine.tick(self.plan, bad)
        self.assertEqual(self.engine.tick(self.plan, bad)[0], "blocked")
        workspace = self.engine.home / "worktrees" / "test"
        self.assertEqual(git(workspace, "rev-list", "--count", "HEAD"), "1")

    def test_single_supervisor_lock(self):
        other = Engine(self.repo)
        try:
            with self.engine.lock():
                with self.assertRaises(RuntimeError):
                    with other.lock():
                        pass
        finally:
            other.db.close()

    def test_path_and_response_boundary(self):
        for name in ("../outside", ".git/config", "/tmp/outside", ".agent-loop/state.sqlite"):
            with self.assertRaises(ValueError):
                safe_path(self.repo, name)
        (self.repo / "link").symlink_to("/tmp")
        with self.assertRaises(ValueError):
            safe_path(self.repo, "link/outside")
        for response in ('[]', '{"files":[{}]}', '{"files":[{"path":"answer.py","content":null}]}'):
            with self.assertRaises(ValueError):
                self.engine.apply_result(self.plan["tasks"][0], response, self.repo)

    def test_budget_is_admission_gate(self):
        self.plan["provider_token_budgets"] = {"codex":0}
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "blocked")
        self.assertEqual(self.calls, 0)

    def test_changed_plan_is_rejected(self):
        self.engine.initialize(self.plan)
        self.plan["tasks"][0]["prompt"] = "different"
        with self.assertRaises(ValueError):
            self.engine.initialize(self.plan)


if __name__ == "__main__":
    unittest.main()
