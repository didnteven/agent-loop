import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loop.adapters import (Result, antigravity_limits, claude_limits, command,
                           parse, provider_limits, quota_deadline, run_process, runner_command,
                           token_total)
from loop.engine import Engine, git, safe_path, validate_plan
from loop.planner import create_plan, validate_generated_plan
from loop.release import checks_pass, publish, validate_remote
from loop.review import review_failure, review_plan
from tests.support import isolate_registry

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

    def test_interrupted_provider_stream_is_transient(self):
        result = parse("antigravity", 1, "", "The stream was interrupted. Please continue the task.")
        self.assertEqual(result.status, "transient")

    def test_claude_limits_reads_statusline_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / ".agent-loop"
            snapshot.mkdir()
            (snapshot / "claude-quota.json").write_text(json.dumps({
                "rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": 200}}
            }))
            with patch("loop.adapters.run_process", return_value=(1, "", "offline")):
                self.assertEqual(claude_limits(directory)["rate_limits"]["five_hour"]["resets_at"], 200)

    def test_claude_limits_falls_back_to_official_usage_command(self):
        usage = {
            "result": "Current session: 82% used · resets Sep 12 at 1:50pm (Pacific/Auckland)\n"
                       "Current week (all models): 41% used · resets Sep 16 at 5pm"
        }
        with patch("loop.adapters.run_process",
                   return_value=(0, json.dumps(usage), "")) as run:
            limits = claude_limits(tempfile.gettempdir())
        self.assertEqual(limits["rate_limits"]["primary"]["usedPercent"], 82)
        self.assertEqual(limits["rate_limits"]["secondary"]["usedPercent"], 41)
        self.assertEqual(run.call_args.args[0][:3], ["claude", "-p", "/usage"])

    def test_antigravity_limits_converts_installed_utility_json(self):
        with tempfile.TemporaryDirectory() as directory:
            utility = Path(directory)
            runner = utility / "node_modules" / ".bin"
            runner.mkdir(parents=True)
            (runner / "tsx").write_text("")
            (utility / "src").mkdir()
            (utility / "src" / "index.ts").write_text("")
            payload = {"models": [{"remainingPercentage": 0.25,
                                    "resetTime": "2026-09-12T02:00:00Z"}]}
            pretty = "Antigravity quota follows\n" + json.dumps(payload, indent=2)
            with patch.dict("os.environ", {"AGENT_LOOP_ANTIGRAVITY_USAGE_DIR": directory}), \
                    patch("loop.adapters.run_process", return_value=(0, pretty, "")):
                limits = antigravity_limits(tempfile.gettempdir())
        self.assertEqual(limits["rate_limits"]["models"][0]["usedPercent"], 75)

    def test_antigravity_limits_reads_official_usage_command(self):
        usage = {
            "status": "SUCCESS",
            "command": {"data": {"groups": [{"name": "Gemini Models", "buckets": [
                {"remaining_fraction": 0.96, "reset_time": "2026-09-13T05:11:51Z"}
            ]}]}}
        }
        with patch("loop.adapters.run_process",
                   return_value=(0, json.dumps(usage), "")) as run:
            limits = antigravity_limits(tempfile.gettempdir())
        self.assertEqual(limits["source"], "agy /usage")
        self.assertEqual(limits["rate_limits"]["models"][0]["usedPercent"], 4)
        self.assertEqual(run.call_args.args[0][:3], ["agy", "-p", "/usage"])

    def test_provider_limits_dispatches_every_supported_provider(self):
        with patch("loop.adapters.codex_limits", return_value={"codex": 1}) as codex, \
                patch("loop.adapters.claude_limits", return_value={"claude": 1}) as claude, \
                patch("loop.adapters.antigravity_limits", return_value={"antigravity": 1}) as agy:
            self.assertEqual(provider_limits("codex", "."), {"codex": 1})
            self.assertEqual(provider_limits("claude", "."), {"claude": 1})
            self.assertEqual(provider_limits("antigravity", "."), {"antigravity": 1})
            self.assertTrue(codex.called and claude.called and agy.called)

    def test_two_pass_planner_uses_context_then_strong_planner(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "answer.py").write_text("answer = 1\n")
            plan = {"id": "planned-change", "tasks": [{
                "id": "change", "provider": "codex", "files": ["answer.py"],
                "prompt": "make the requested change", "check": ["python3", "-c", "pass"]
            }]}
            calls = []
            def fake_ask(provider, model, prompt, cwd, timeout, effort):
                calls.append((provider, model, effort, prompt))
                return ("repository notes", {}) if len(calls) == 1 else (json.dumps(plan), {})
            with patch("loop.planner.live_provider_usage", return_value={"codex": {"status": "ok"}, "claude": {"status": "ok"}, "antigravity": {"status": "ok"}}), \
                    patch("loop.planner.ask", side_effect=fake_ask):
                result = create_plan(directory, "fix the button", timeout=1)
        self.assertEqual(result["id"], "planned-change")
        self.assertEqual([(call[0], call[2]) for call in calls],
                         [("antigravity", "low"), ("antigravity", "high")])
        self.assertIn("repository notes", calls[1][3])

    def test_generated_plan_can_create_file_inside_default_root(self):
        plan = {"id": "planned-change", "tasks": [{
            "id": "change", "provider": "codex", "files": ["missing.py"],
            "prompt": "make the requested change", "check": ["python3", "-c", "pass"]
        }]}
        with tempfile.TemporaryDirectory() as directory:
            validate_generated_plan(plan, Path(directory))

    def test_provider_runner_is_the_only_real_worker_launcher(self):
        argv = runner_command("codex", ["codex", "exec", "work"], "/repo", 123, 456,
                              "base_model_inference")
        self.assertIn("scripts/run_provider.py", argv[1])
        self.assertEqual(argv[argv.index("--provider") + 1], "codex")
        self.assertEqual(argv[argv.index("--quota-bucket") + 1], "base_model_inference")

    def test_arguments_are_not_shell_code(self):
        prompt = 'literal $(whoami) `date` "quotes"'
        self.assertTrue(any(prompt in argument for argument in command("claude", prompt)))

    def test_model_and_prompt_order_matches_each_cli(self):
        for provider, model in (("codex", "gpt-5.6-luna"),
                                ("claude", "claude-sonnet-5")):
            argv = command(provider, "Return JSON only", model)
            model_index = argv.index("--model")
            self.assertEqual(argv[model_index + 1], model)
            self.assertTrue(argv[model_index + 2].startswith("Return JSON only"))
        antigravity = command("antigravity", "Return JSON only", "gemini-3.1-pro-high")
        prompt_index = next(i for i, arg in enumerate(antigravity)
                            if arg.startswith("Return JSON only"))
        self.assertEqual(antigravity[prompt_index - 1], "-p")
        self.assertEqual(antigravity[antigravity.index("--model") + 1], "gemini-3.1-pro-high")

    def test_planning_commands_forbid_edits_without_worker_instructions(self):
        prompt = command("codex", "Make a plan", worker=False)[-1]
        self.assertIn("Do not edit files", prompt)
        self.assertNotIn("managed worktree directly", prompt)

    def test_worker_commands_enable_tools_but_disable_delegation(self):
        codex = command("codex", "work")
        self.assertIn("workspace-write", codex)
        self.assertIn("multi_agent", codex)
        self.assertIn("multi_agent_v2", codex)
        claude = command("claude", "work")
        self.assertIn("default", claude)
        self.assertIn("Agent", claude)
        self.assertNotIn("", claude)
        antigravity = command("antigravity", "work")
        self.assertIn("accept-edits", antigravity)
        # A headless agy worker cannot answer a permission prompt, so without
        # auto-approval every command it attempts is denied and it produces
        # nothing. --sandbox forces exactly that and the two flags cannot be
        # combined, so a worker gets auto-approval and restriction comes from
        # the managed worktree and the supervisor's checks instead.
        self.assertIn("--dangerously-skip-permissions", antigravity)
        self.assertNotIn("--sandbox", antigravity)
        self.assertIn("--sandbox", command("antigravity", "work", sandbox=True))

    def test_antigravity_tiered_model_carries_its_own_effort(self):
        # agy refuses "--model gemini-3.1-pro-high --effort high": the tier is in the id.
        argv = command("antigravity", "work", "gemini-3.1-pro-high")
        self.assertIn("gemini-3.1-pro-high", argv)
        self.assertNotIn("--effort", argv)
        untiered = command("antigravity", "work", "gemini-3.1-pro", "medium")
        self.assertEqual(untiered[untiered.index("--effort") + 1], "medium")

    def test_timeout_is_bounded(self):
        code, _, _ = run_process(["python3", "-c", "import time; time.sleep(10)"], tempfile.gettempdir(), 0.05)
        self.assertEqual(code, 124)

    def test_any_provider_turn_limit_is_a_timeout(self):
        out = '{"type":"result","subtype":"error_max_turns","is_error":false,"result":"partial"}'
        result = parse("claude", 0, out, "")
        self.assertEqual(result.status, "transient")
        quoted = '{"type":"result","subtype":"success","is_error":false,"result":"notes on print timeout"}'
        self.assertEqual(parse("claude", 0, quoted, "").status, "ok")

    def test_output_review_lists_media_and_reads_verdict(self):
        from loop.review import review_output
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            git(workspace, "config", "user.name", "T")
            git(workspace, "config", "user.email", "t@example.invalid")
            (workspace / "seed").write_text("x")
            git(workspace, "add", "seed")
            git(workspace, "commit", "-qm", "seed")
            (workspace / "ad.png").write_bytes(b"\x89PNG")
            task = {"id": "ad", "files": ["ad.png"], "prompt": "make an ad",
                    "review": {"provider": "claude", "instructions": "faces visible"}}
            reply = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                "result": "The phone hides the face.\nREVIEW: DECLINED"})
            with patch("loop.review.run_process", return_value=(0, reply, "")) as run:
                verdict = review_output("claude", None, task, workspace)
            prompt = run.call_args.args[0][2]
            self.assertIn(str(workspace / "ad.png"), prompt)
            self.assertIn("faces visible", prompt)
            self.assertEqual(verdict, {"approved": False, "reasoning": "The phone hides the face."})

    def test_antigravity_cut_off_turn_is_a_timeout_not_success(self):
        out = '{"status":"SUCCESS","response":"","usage":{"total_tokens":10}}'
        err = "[agy] print timeout after 2m30s with turn in progress; returning partial output"
        result = parse("antigravity", 0, out, err)
        self.assertEqual(result.status, "transient")
        self.assertIn("timeout", result.error.lower())
        self.assertEqual(parse("antigravity", 0, out, "").status, "ok")

    def test_idle_timeout_stops_a_silent_process(self):
        code, _, err = run_process(["python3", "-c", "import time; time.sleep(10)"],
                                   tempfile.gettempdir(), 30, idle_timeout=0.3)
        self.assertEqual(code, 124)
        self.assertIn("idle timeout", err)

    def test_idle_timeout_lets_a_process_that_keeps_producing_output_finish(self):
        script = ("import sys, time\n"
                  "for i in range(10):\n"
                  "    print(i, flush=True); time.sleep(0.1)\n")
        code, out, _ = run_process(["python3", "-c", script], tempfile.gettempdir(), 30,
                                   idle_timeout=0.5)
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), [str(i) for i in range(10)])

    def test_file_activity_keeps_a_silent_process_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.bin"
            script = ("import time\n"
                      "for i in range(8):\n"
                      "    open(%r, 'ab').write(b'x'); time.sleep(0.15)\n" % str(target))
            signature = lambda: target.stat().st_size if target.exists() else None
            code, _, err = run_process(["python3", "-c", script], tmp, 30,
                                       idle_timeout=0.4, activity=signature)
            self.assertEqual(code, 0, err)
            self.assertEqual(target.read_bytes(), b"x" * 8)

    def test_output_is_written_to_tee_files_while_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_log, err_log = Path(tmp) / "out.log", Path(tmp) / "err.log"
            script = ("import sys, time\n"
                      "print('first', flush=True)\n"
                      "time.sleep(1.5)\n"
                      "print('second', flush=True)\n")
            proc = subprocess.Popen(
                ["python3", "-c",
                 "import sys; sys.path.insert(0, %r)\n"
                 "from loop.adapters import run_process\n"
                 "run_process(['python3', '-c', %r], %r, 30, tee=(%r, %r))\n"
                 % (str(Path(__file__).resolve().parents[1]), script, tmp,
                    str(out_log), str(err_log))])
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not (
                        out_log.exists() and "first" in out_log.read_text()):
                    time.sleep(0.05)
                self.assertIn("first", out_log.read_text())
                self.assertNotIn("second", out_log.read_text())
            finally:
                proc.wait()
            self.assertIn("second", out_log.read_text())

    def test_noninteractive_processes_receive_eof_not_a_live_stdin_pipe(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ("ok", "")
        with patch("loop.adapters.subprocess.Popen", return_value=proc) as popen:
            self.assertEqual(run_process(["tool"], tempfile.gettempdir()), (0, "ok", ""))
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

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

    def test_failure_review_parses_retry_verdict(self):
        task = {"id": "one", "files": ["answer.py"], "prompt": "write answer",
                "check": ["python3", "-c", "pass"]}
        with patch("loop.review.run_process",
                   return_value=(0, '{"type":"result","subtype":"success","is_error":false,'
                                    '"result":"The assertion is fixable.\\nFAILURE: RETRY"}', "")), \
                patch("loop.review.git", return_value="diff"):
            verdict = review_failure("claude", None, task, "assertion failed", "/private/tmp")
        self.assertTrue(verdict["retry"])
        self.assertIn("fixable", verdict["reasoning"])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        isolate_registry(self)
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

    def test_provider_runner_receives_the_task_quota_bucket(self):
        task = dict(self.plan["tasks"][0], quota_bucket="base_model_inference")
        argv = runner_command(task["provider"], ["codex", "exec", "work"], self.repo,
                              180, 1800, task["quota_bucket"])
        self.assertEqual(argv[argv.index("--quota-bucket") + 1], "base_model_inference")

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
        self.assertEqual(self.engine.tick(self.plan, bad)[0], "parked")
        workspace = self.engine.home / "worktrees" / "test"
        self.assertEqual(git(workspace, "rev-list", "--count", "HEAD"), "1")

    def test_tool_worker_edits_managed_worktree_directly(self):
        def tool_worker(_task, _prompt, workspace):
            (workspace / "answer.py").write_text("def add(a,b): return a+b\n")
            return Result("ok", "Implemented and checked the requested file")
        self.assertEqual(self.engine.tick(self.plan, tool_worker)[0], "progress")
        workspace = self.engine.home / "worktrees" / "test"
        self.assertEqual((workspace / "answer.py").read_text(), "def add(a,b): return a+b\n")

    def test_plan_rejects_gitignored_task_files(self):
        plan = json.loads(json.dumps(self.plan))
        plan["tasks"][0]["files"] = ["__pycache__/answer.py"]
        with self.assertRaisesRegex(ValueError, "gitignored"):
            validate_plan(plan, self.repo)
        validate_plan(self.plan, self.repo)

    def test_timeout_keeps_in_scope_progress_and_next_attempt_continues(self):
        calls = 0
        def worker(_task, prompt, workspace):
            nonlocal calls
            calls += 1
            if calls == 1:
                (workspace / "answer.py").write_text("def add(a,b):\n    pass\n")
                (workspace / "stray.txt").write_text("outside the allowlist")
                return Result("transient", error="worker idle timeout: no output for 300s")
            self.assertEqual((workspace / "answer.py").read_text(), "def add(a,b):\n    pass\n")
            self.assertFalse((workspace / "stray.txt").exists())
            self.assertIn("continue from where it left off", prompt)
            (workspace / "answer.py").write_text("def add(a,b): return a+b\n")
            return Result("ok", "finished")
        self.engine.tick(self.plan, worker)
        row = self.engine.status()["tasks"][0]
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))
        self.assertEqual(self.engine.tick(self.plan, worker)[0], "progress")
        self.assertEqual(calls, 2)
        self.assertEqual(self.engine.status()["tasks"][0]["status"], "done")

    def test_failed_check_saves_rejected_files_for_the_next_attempt(self):
        calls = 0
        def worker(_task, prompt, workspace):
            nonlocal calls
            calls += 1
            if calls == 1:
                (workspace / "answer.py").write_text("def add(a,b): return 0\n")
                return Result("ok", "done")
            self.assertFalse((workspace / "answer.py").exists())
            saved = self.engine.home / "rejected" / "test" / "one" / "answer.py"
            self.assertEqual(saved.read_text(), "def add(a,b): return 0\n")
            self.assertIn(str(saved.parent), prompt)
            (workspace / "answer.py").write_text("def add(a,b): return a+b\n")
            return Result("ok", "done")
        self.engine.tick(self.plan, worker)
        self.assertEqual(self.engine.tick(self.plan, worker)[0], "progress")
        self.assertEqual(self.engine.status()["tasks"][0]["status"], "done")

    def test_timeout_without_new_progress_is_not_resumed_forever(self):
        def stuck(_task, _prompt, workspace):
            (workspace / "answer.py").write_text("def add(a,b):\n    pass\n")
            return Result("transient", error="worker timeout")
        workspace = self.engine.home / "worktrees" / "test"
        self.engine.tick(self.plan, stuck)
        self.assertTrue((workspace / "answer.py").exists())
        self.engine.tick(self.plan, stuck)
        self.assertFalse((workspace / "answer.py").exists())
        self.assertEqual(self.engine.status()["tasks"][0]["status"], "waiting")

    def commit_file(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        git(self.repo, "add", name)
        git(self.repo, "commit", "-qm", "add " + name)

    def test_command_task_runs_without_a_model(self):
        self.commit_file("tools/gen.py", "open('answer.py','w').write('def add(a,b): return a+b\\n')\n")
        plan = json.loads(json.dumps(self.plan))
        plan["tasks"][0].update(provider="command", run=["python3", "tools/gen.py"])
        self.assertEqual(self.engine.tick(plan, self.good_worker)[0], "progress")
        self.assertEqual(self.calls, 0)
        row = self.engine.status()["tasks"][0]
        self.assertEqual((row["status"], row["tokens"]), ("done", 0))
        self.assertTrue(list((self.engine.home / "logs" / "test").glob("one-command-*.log")))

    def test_failing_command_task_retries_with_backoff(self):
        self.commit_file("tools/fail.py", "raise SystemExit(3)\n")
        plan = json.loads(json.dumps(self.plan))
        plan["tasks"][0].update(provider="command", run=["python3", "tools/fail.py"])
        self.engine.tick(plan)
        row = self.engine.status()["tasks"][0]
        self.assertEqual(row["status"], "waiting")
        self.assertIn("Command exited 3", row["error"])

    def test_command_must_be_registered_by_policy(self):
        plan = json.loads(json.dumps(self.plan))
        plan["tasks"][0].update(provider="command", run=["python3", "tools/gen.py"])
        plan["policy"] = {"trusted_commands": [["python3", "other.py"]]}
        with self.assertRaisesRegex(ValueError, "command is not registered"):
            validate_plan(plan, self.repo)

    def test_output_review_rejection_blocks_commit_and_retries(self):
        plan = json.loads(json.dumps(self.plan))
        plan["tasks"][0]["review"] = {"provider": "claude", "instructions": "must be tidy"}
        verdicts = [{"approved": False, "reasoning": "Phone covers the athlete's face"},
                    {"approved": True, "reasoning": ""}]
        with patch.object(Engine, "review_output", side_effect=lambda *_: verdicts.pop(0)):
            self.assertEqual(self.engine.tick(plan, self.good_worker)[0], "progress")
            row = self.engine.status()["tasks"][0]
            self.assertEqual(row["status"], "pending")
            self.assertIn("Output review rejected", row["error"])
            self.assertIn("covers the athlete", row["error"])
            workspace = self.engine.home / "worktrees" / "test"
            self.assertEqual(git(workspace, "rev-list", "--count", "HEAD"), "1")
            self.assertEqual(self.engine.tick(plan, self.good_worker)[0], "progress")
        self.assertEqual(self.engine.status()["tasks"][0]["status"], "done")

    def test_review_that_edits_the_worktree_is_void(self):
        plan = json.loads(json.dumps(self.plan))
        plan["tasks"][0]["review"] = {"provider": "claude"}
        workspace = self.engine.home / "worktrees" / "test"
        def meddling(*_):
            (workspace / "answer.py").write_text("def add(a,b): return a+b  # edited\n")
            return {"approved": True, "reasoning": ""}
        with patch.object(Engine, "review_output", side_effect=meddling):
            self.engine.tick(plan, self.good_worker)
        self.assertIn("modified the worktree", self.engine.status()["tasks"][0]["error"])

    def test_amend_raises_budget_releases_parked_task_and_adds_tasks(self):
        plan = json.loads(json.dumps(self.plan))
        plan["provider_token_budgets"] = {"codex": 10}
        with self.engine.run_lock("test"):
            self.engine.tick(plan, self.good_worker)
        self.engine.set_task("test", "one", status="pending", sha=None)
        self.engine.db.execute("UPDATE tasks SET tokens=50 WHERE run_id='test'")
        self.engine.db.commit()
        workspace = self.engine.home / "worktrees" / "test"
        git(workspace, "reset", "-q", "--hard", "HEAD~1")
        self.engine.tick(plan, self.good_worker)
        self.assertIn("token budget", self.engine.status()["tasks"][0]["park_reason"])
        amended = json.loads(json.dumps(plan))
        amended["provider_token_budgets"] = {"codex": 100000}
        amended["tasks"].append({"id": "two", "provider": "codex", "files": ["other.py"],
                                 "prompt": "write other", "check": ["python3", "-c", "import other"]})
        summary = self.engine.amend(amended)
        self.assertEqual(summary["released"], ["one"])
        self.assertEqual(summary["added_tasks"], ["two"])
        self.assertEqual(summary["settings"], ["provider_token_budgets"])
        statuses = {row["id"]: row["status"] for row in self.engine.status()["tasks"]}
        self.assertEqual(statuses, {"one": "pending", "two": "pending"})
        self.engine.tick(amended, self.good_worker)

    def test_amend_refuses_to_change_done_tasks_or_remove_tasks(self):
        self.engine.tick(self.plan, self.good_worker)
        changed = json.loads(json.dumps(self.plan))
        changed["tasks"][0]["prompt"] = "something else"
        with self.assertRaisesRegex(ValueError, "Completed task one cannot be changed"):
            self.engine.amend(changed)
        extended = json.loads(json.dumps(self.plan))
        extended["tasks"].append({"id": "two", "provider": "codex", "files": ["b.py"],
                                  "prompt": "b", "check": ["python3", "-c", "import b"]})
        self.engine.amend(extended)
        with self.assertRaisesRegex(ValueError, "cannot be removed"):
            self.engine.amend(self.plan)

    def test_base_ref_starts_the_run_from_another_branch(self):
        git(self.repo, "checkout", "-q", "-b", "prior-work")
        self.commit_file("prior.txt", "earlier run output\n")
        prior = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "main")
        plan = dict(json.loads(json.dumps(self.plan)), base_ref="prior-work")
        self.engine.tick(plan, self.good_worker)
        run = self.engine.status()["runs"][0]
        self.assertEqual(run["base"], prior)
        self.assertTrue((Path(run["workspace"]) / "prior.txt").exists())

    def test_preflight_rejects_uncommitted_check_scripts(self):
        plan = json.loads(json.dumps(self.plan))
        plan["tasks"][0]["check"] = ["python3", "checks/verify_sum.py"]
        with self.assertRaisesRegex(ValueError, "checks/verify_sum.py is not committed"):
            self.engine.initialize(plan)

    def test_preflight_warns_about_prompt_paths_missing_from_the_worktree(self):
        plan = json.loads(json.dumps(self.plan))
        plan["tasks"][0]["prompt"] = "Copy Metadata/en/home.png into place, then write answer.py"
        self.engine.initialize(plan)
        details = [row["detail"] for row in self.engine.db.execute(
            "SELECT detail FROM events WHERE kind='preflight_warning'")]
        self.assertEqual(len(details), 1)
        self.assertIn("Metadata/en/home.png", details[0])

    def test_check_that_passes_before_work_can_skip_the_model(self):
        plan = json.loads(json.dumps(self.plan))
        plan["tasks"][0]["check"] = ["python3", "-c", "pass"]
        plan["preflight_checks"] = "skip"
        self.assertEqual(self.engine.tick(plan, self.good_worker)[0], "ready_for_pr")
        self.assertEqual(self.calls, 0)
        kinds = [row["kind"] for row in self.engine.db.execute("SELECT kind FROM events")]
        self.assertIn("check_passes_before_work", kinds)

    def test_run_summary_logs_and_supervisors(self):
        self.engine.tick(self.plan, self.good_worker)
        with self.engine.run_lock("test"):
            running = self.engine.supervisors()
            self.assertEqual([(e["run_id"], e["pid"]) for e in running], [("test", os.getpid())])
            self.assertIn("supervisor running", self.engine.run_summary("test"))
        self.assertEqual(self.engine.supervisors(), [])
        summary = self.engine.run_summary("test")
        self.assertIn("not running", summary)
        self.assertRegex(summary, r"one\s+done\s+codex")
        logs = self.engine.home / "logs" / "test"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "one-abc.jsonl").write_text("{}\n")
        self.assertEqual(self.engine.latest_log("test", "one").name, "one-abc.jsonl")

    def test_clean_removes_only_merged_or_superseded_worktrees(self):
        self.engine.tick(self.plan, self.good_worker)
        workspace = self.engine.home / "worktrees" / "test"
        self.assertEqual(self.engine.clean(apply=True), [])
        self.assertTrue(workspace.exists())
        self.engine.event("test", "release", "merged", {})
        report = self.engine.clean()
        self.assertEqual([entry["run_id"] for entry in report], ["test"])
        self.assertTrue(workspace.exists())
        self.engine.clean(apply=True)
        self.assertFalse(workspace.exists())
        self.assertTrue(git(self.repo, "branch", "--list", "loop/test"))

    def test_failed_check_rolls_back_before_retry(self):
        calls = 0
        def worker(_task, _prompt, workspace):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertFalse((workspace / "answer.py").exists())
            content = "def add(a,b): return 0\n" if calls == 1 else "def add(a,b): return a+b\n"
            (workspace / "answer.py").write_text(content)
            return Result("ok", "done")
        self.assertEqual(self.engine.tick(self.plan, worker)[0], "progress")
        workspace = self.engine.home / "worktrees" / "test"
        self.assertFalse((workspace / "answer.py").exists())
        self.assertEqual(self.engine.tick(self.plan, worker)[0], "progress")

    def test_suspicious_existing_file_truncation_is_rejected_and_rolled_back(self):
        original = "line of important existing content\n" * 100
        (self.repo / "large.txt").write_text(original)
        git(self.repo, "add", "large.txt")
        git(self.repo, "commit", "-qm", "large fixture")
        self.plan["tasks"][0]["files"] = ["large.txt"]
        self.plan["tasks"][0]["check"] = ["python3", "-c", "pass"]
        def truncating(_task, _prompt, workspace):
            (workspace / "large.txt").write_text("replacement\n")
            return Result("ok", "done")
        self.assertEqual(self.engine.tick(self.plan, truncating)[0], "progress")
        workspace = self.engine.home / "worktrees" / "test"
        self.assertEqual((workspace / "large.txt").read_text(), original)
        self.assertIn("Suspicious truncation", self.engine.status()["tasks"][0]["error"])

    def test_interrupted_worker_is_pending_and_rolled_back(self):
        def interrupted(_task, _prompt, workspace):
            (workspace / "answer.py").write_text("partial")
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.engine.tick(self.plan, interrupted)
        workspace = self.engine.home / "worktrees" / "test"
        self.assertFalse((workspace / "answer.py").exists())
        row = self.engine.status()["tasks"][0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)

    def test_worker_commit_is_rejected_and_history_is_restored(self):
        def committing(_task, _prompt, workspace):
            (workspace / "answer.py").write_text("def add(a,b): return a+b\n")
            git(workspace, "add", "answer.py")
            git(workspace, "commit", "-qm", "worker must not commit")
            return Result("ok", "done")
        self.assertEqual(self.engine.tick(self.plan, committing)[0], "progress")
        workspace = self.engine.home / "worktrees" / "test"
        self.assertEqual(git(workspace, "rev-list", "--count", "HEAD"), "1")
        self.assertFalse((workspace / "answer.py").exists())
        self.assertIn("Git history", self.engine.status()["tasks"][0]["error"])

    def test_setup_runs_before_worker_and_allows_ignored_dependencies(self):
        with (self.repo / ".gitignore").open("a") as handle:
            handle.write(".deps-ready\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-qm", "ignore setup fixture")
        self.plan["setup"] = ["python3", "-c",
                              "from pathlib import Path; Path('.deps-ready').write_text('ready')"]
        def worker(_task, _prompt, workspace):
            self.assertTrue((workspace / ".deps-ready").exists())
            (workspace / "answer.py").write_text("def add(a,b): return a+b\n")
            return Result("ok", "done")
        self.assertEqual(self.engine.tick(self.plan, worker)[0], "progress")
        self.assertEqual(self.engine.tick(self.plan, worker)[0], "ready_for_pr")

    def test_failed_setup_rolls_back_changes(self):
        self.plan["setup"] = ["python3", "-c",
                              "from pathlib import Path; Path('setup-damage').write_text('x'); raise SystemExit(1)"]
        with self.assertRaisesRegex(RuntimeError, "setup failed"):
            self.engine.tick(self.plan, self.good_worker)
        workspace = self.engine.home / "worktrees" / "test"
        self.assertFalse((workspace / "setup-damage").exists())

    def test_failure_review_advises_without_blocking_a_failed_check(self):
        self.plan["failure_review"] = True
        def bad(*_):
            return Result("ok", '{"files":[{"path":"answer.py","content":"def add(a,b): return 0"}]}')
        with patch.object(self.engine, "review_failure",
                          return_value={"retry": False, "reasoning": "The task is underspecified."}):
            self.assertEqual(self.engine.tick(self.plan, bad)[0], "progress")
        row = self.engine.status()["tasks"][0]
        self.assertEqual(row["status"], "pending")
        self.assertIn("underspecified", row["error"])

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
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "parked")
        self.assertEqual(self.calls, 0)
        self.assertIn("this section", self.engine.status()["tasks"][0]["error"])

    def test_plan_budgets_must_be_sane(self):
        for budget in ({"codex": -1}, {"nowhere": 10}, {"codex": True}, {"codex": 1.5}):
            plan = dict(self.plan, provider_token_budgets=budget)
            with self.assertRaises(ValueError):
                validate_plan(plan)

    def test_worker_cannot_own_its_trusted_check(self):
        self.plan["tasks"][0]["files"] = ["verify.py"]
        self.plan["tasks"][0]["check"] = ["python3", "verify.py"]
        with self.assertRaisesRegex(ValueError, "trusted check"):
            validate_plan(self.plan)

    def test_incompatible_antigravity_effort_is_rejected(self):
        task = self.plan["tasks"][0]
        task.update(provider="antigravity", model="gemini-3.1-pro-high", effort="low")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            validate_plan(self.plan)

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
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "parked")
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
        self.assertEqual(self.engine.tick(self.plan, self.good_worker)[0], "parked")
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
