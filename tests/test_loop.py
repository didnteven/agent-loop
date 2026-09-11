import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loop.adapters import Result, command, parse, quota_deadline, run_process, token_total
from loop.engine import Engine, git, safe_path, validate_plan
from loop.release import checks_pass, publish, validate_remote
from loop.review import review_plan

ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).resolve().parent / "concurrent_worker.py"


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
        self.assertTrue(any(prompt in argument for argument in command("claude", prompt)))

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

    def test_review_parses_declined_verdict(self):
        with patch("loop.review.run_process",
                   return_value=(0, '{"type":"result","subtype":"success","is_error":false,'
                                    '"result":"Task 2 duplicates existing code.\\nREVIEW: DECLINED"}', "")):
            verdict = review_plan("claude", None, {"id": "x", "tasks": []}, "/private/tmp")
        self.assertFalse(verdict["approved"])
        self.assertIn("duplicates", verdict["reasoning"])

    def test_review_parses_approved_verdict(self):
        with patch("loop.review.run_process",
                   return_value=(0, '{"type":"result","subtype":"success","is_error":false,'
                                    '"result":"REVIEW: APPROVED"}', "")):
            verdict = review_plan("claude", None, {"id": "x", "tasks": []}, "/private/tmp")
        self.assertTrue(verdict["approved"])

    def test_review_rejects_response_without_verdict(self):
        with patch("loop.review.run_process",
                   return_value=(0, '{"type":"result","subtype":"success","is_error":false,'
                                    '"result":"looks fine to me"}', "")):
            with self.assertRaises(RuntimeError):
                review_plan("claude", None, {"id": "x", "tasks": []}, "/private/tmp")

    def test_review_rejects_multiple_verdicts(self):
        with patch("loop.review.run_process",
                   return_value=(0, '{"type":"result","subtype":"success","is_error":false,'
                                    '"result":"REVIEW: APPROVED\\nwait, REVIEW: DECLINED"}', "")):
            with self.assertRaises(RuntimeError):
                review_plan("claude", None, {"id": "x", "tasks": []}, "/private/tmp")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "checkout", "-q", "-B", "main")
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
        with self.engine.run_lock("test"):
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

    def test_one_supervisor_per_milestone_but_not_per_repository(self):
        other = Engine(self.repo)
        try:
            with self.engine.run_lock("test"):
                with self.assertRaises(RuntimeError):
                    with other.run_lock("test"):
                        pass
                # A different milestone in the same repository is not excluded.
                with other.run_lock("second"):
                    pass
        finally:
            other.db.close()

    def test_concurrent_milestones_share_one_repository(self):
        plans = []
        for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
            plan = json.loads(json.dumps(self.plan))
            plan["id"] = name
            plan["tasks"][0]["files"] = [name + ".py"]
            plan["tasks"][0]["check"] = ["python3", "-c",
                                         "from " + name + " import add; assert add(2,3)==5"]
            plans.append(plan)
        workers = [subprocess.Popen(
            ["python3", str(WORKER), str(self.repo), json.dumps(plan)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for plan in plans]
        (self.engine.home / "go").write_text("")
        results = [worker.communicate() for worker in workers]
        for plan, (out, err) in zip(plans, results):
            self.assertEqual(out.strip().splitlines()[-1], "ready_for_pr", err)
            workspace = self.engine.home / "worktrees" / plan["id"]
            self.assertEqual(git(workspace, "rev-list", "--count", "HEAD"), "2")
            self.assertTrue((workspace / (plan["id"] + ".py")).exists())
        # Neither milestone leaked into the other's worktree or the shared checkout.
        self.assertFalse((self.engine.home / "worktrees" / "alpha" / "beta.py").exists())
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), len(plans) + 1)
        self.assertFalse((self.repo / "alpha.py").exists())

    def test_token_budgets_are_scoped_to_the_plan_and_the_task(self):
        self.plan["tasks"][0]["token_budget"] = 40
        # A concurrent milestone's spend on the same provider must not gate this one.
        self.engine.db.execute("UPDATE providers SET tokens=1000000000 WHERE name='codex'")
        self.plan["provider_token_budgets"] = {"codex": 1000}
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "progress")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.engine.status()["tasks"][0]["tokens"], 60)

    def test_task_budget_is_its_own_admission_gate(self):
        self.plan["tasks"][0]["token_budget"] = 0
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "blocked")
        self.assertEqual(self.calls, 0)
        self.assertIn("this section", self.engine.status()["tasks"][0]["error"])

    def test_plan_budgets_must_be_sane(self):
        for budget in ({"codex": -1}, {"nowhere": 10}, {"codex": True}, {"codex": 1.5}):
            plan = dict(self.plan, provider_token_budgets=budget)
            with self.assertRaises(ValueError):
                validate_plan(plan)

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

    def test_milestone_rechecks_earlier_sections(self):
        self.plan["tasks"].append({"id":"two", "provider":"claude", "files":["answer.py"],
                                   "prompt":"change answer", "check":["python3","-c","pass"]})
        self.engine.tick(self.plan, self.good_worker)
        def regressing(*_):
            return Result("ok", '{"files":[{"path":"answer.py","content":"def add(a,b): return 0"}]}')
        self.engine.tick(self.plan, regressing)
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "blocked")
        row = self.engine.status()["tasks"][0]
        self.assertIn("Milestone regression", row["error"])
        self.engine.set_task("test", "one", status="pending", attempts=0)
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "progress")
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "ready_for_pr")

    def test_unrelated_worktree_edits_are_preserved(self):
        run = self.engine.initialize(self.plan)
        unrelated = Path(run["workspace"]) / "user-note.txt"
        unrelated.write_text("keep me")
        with self.assertRaises(RuntimeError):
            self.engine.tick(self.plan, self.good_worker)
        self.assertEqual(unrelated.read_text(), "keep me")
        self.assertEqual(self.calls, 0)

    def test_release_checks_and_exact_sha_with_simulated_github(self):
        self.engine.tick(self.plan, self.good_worker)
        self.engine.tick(self.plan, self.good_worker)
        sha = self.engine.status()["tasks"][0]["sha"]
        calls = []
        merged = False
        pr = {"number":1, "state":"OPEN", "headRefOid":sha, "url":"https://github.com/test/demo/pull/1"}
        def fake_git(repo, *args):
            calls.append(("git", *args))
            if args[:2] == ("remote", "get-url"):
                return "https://github.com/test/demo.git"
            if args[0] == "rev-parse":
                return sha
            return ""
        def fake_gh(repo, *args):
            nonlocal merged
            calls.append(("gh", *args))
            if args[:2] == ("pr", "list"):
                return json.dumps([dict(pr, state="MERGED" if merged else "OPEN")])
            if args[:2] == ("pr", "merge"):
                merged = True
                self.assertIn("--match-head-commit", args)
                self.assertIn(sha, args)
                self.assertNotIn("--admin", args)
                return ""
            return json.dumps(dict(pr, state="MERGED" if merged else "OPEN"))
        with patch("loop.release.git", side_effect=fake_git), patch("loop.release.gh", side_effect=fake_gh), \
                patch("loop.release.subprocess.check_output", return_value="true"), \
                patch("loop.release.subprocess.run") as check:
            check.return_value = subprocess.CompletedProcess([], 0, '[]', '')
            self.assertEqual(publish(self.engine,"test","test/demo",merge=True)["state"], "waiting_for_checks")
            self.assertFalse(merged)
            check.return_value = subprocess.CompletedProcess([], 0, '[{"bucket":"pass"}]', '')
            self.assertEqual(publish(self.engine,"test","test/demo",merge=True)["state"], "merged")
            count = len(calls)
            self.assertEqual(publish(self.engine,"test","test/demo",merge=True)["state"], "merged")
            self.assertFalse(any(call[:2] == ("git","push") for call in calls[count:]))


if __name__ == "__main__":
    unittest.main()
