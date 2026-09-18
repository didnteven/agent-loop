import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from loop import keeper
from loop.adapters import quota_expiry
from loop.lite import LiteRun, extract_actions


def sh(cwd, *args):
    return subprocess.check_output(list(args), cwd=cwd, text=True).strip()


def claude_reply(text, session="sess-1", context=1000, error=False):
    return json.dumps({"type": "result", "is_error": error, "result": text,
                       "session_id": session,
                       "usage": {"input_tokens": context, "output_tokens": 10}})


def actions(*items):
    return json.dumps({"actions": list(items)})


class FakeClient:
    def __init__(self, reply=None, up=True):
        self.reply, self.up, self.calls = reply, up, 0

    def ensure(self):
        return self.up

    def chat(self, system, user, schema):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Harness:
    """Scripted supervisor replies and worker behaviours behind ``invoke``."""

    def __init__(self, testcase, supervisors=("claude", "codex"), reset_at=120000, audit=False,
                 gates=()):
        directory = tempfile.TemporaryDirectory()
        testcase.addCleanup(directory.cleanup)
        self.repo = Path(directory.name) / "repo"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q")
        sh(self.repo, "git", "config", "user.email", "t@example.invalid")
        sh(self.repo, "git", "config", "user.name", "T")
        (self.repo / "app.py").write_text("value = 1\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "init")
        self.supervisor_replies = []   # callables(worktree) -> (code, out, err)
        self.worker_actions = []       # callables(worktree) -> (code, out, err)
        self.audit_replies = []        # callables(worktree) -> (code, out, err)
        self.audit_prompts = []
        self.prompts, self.argvs = [], []
        self.client = FakeClient({"blocker": None, "suggested_fix_command": None,
                                  "progress": ["did it"], "handoff": "summary"})
        self.run = LiteRun(self.repo, "t", invoke=self.invoke, limits=lambda p: {},
                           scribe=keeper.Scribe(self.client), sleep=lambda s: None,
                           out=lambda m: None, models=lambda provider: None)
        self.run.create("Make value 2", options={"supervisors": list(supervisors),
                                                 "workers": ["claude", "codex"],
                                                 "reset_at_tokens": reset_at,
                                                 "audit": {"enabled": audit, "sample": 1.0},
                                                 "gates": list(gates)})

    def invoke(self, argv, cwd, timeout, idle_timeout=None, activity=None, tee=None):
        self.argvs.append(argv)
        supervisor = ("Read,Grep,Glob" in argv or 'sandbox_mode="read-only"' in argv
                      or (argv[0] == "agy" and "plan" in argv))
        if supervisor:
            prompt = argv[2] if argv[0] in ("claude", "agy") else argv[-1]
            if "AUDIT REQUEST" in prompt:
                self.audit_prompts.append((argv, prompt))
                return self.audit_replies.pop(0)(Path(cwd))
            self.prompts.append(prompt)
            return self.supervisor_replies.pop(0)(Path(cwd))
        return self.worker_actions.pop(0)(Path(cwd))

    def state(self):
        return self.run.load("supervisor.json")

    def plan(self):
        return {task["id"]: task for task in self.run.load("plan.json")["tasks"]}


def reply(text, **kwargs):
    return lambda worktree: (0, claude_reply(text, **kwargs), "")


def worker_writes(files, summary="done"):
    def act(worktree):
        for name, content in files.items():
            target = worktree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return 0, claude_reply(summary, session="worker"), ""
    return act


PLAN_AND_DISPATCH = actions(
    {"op": "plan", "tasks": [{"id": "bump", "title": "Bump", "prompt": "set value to 2",
                              "check": "grep -q 'value = 2' app.py"}]},
    {"op": "note", "text": "bump dispatched to claude"},
    {"op": "dispatch", "task": "bump", "provider": "claude", "prompt": "edit app.py"})
CHECK_WITH_SIDE_EFFECT = ("grep -q 'value = 2' app.py && mkdir -p __pycache__ "
                          "&& touch __pycache__/app.pyc && echo junk >> app.py.log")


class DecideTests(unittest.TestCase):
    def test_rule_table(self):
        base = {"available": ["codex", "claude"], "reset_at": 120000, "context_tokens": 1000,
                "violations": 0, "stagnant_turns": 0, "stagnant_limit": 4}
        cases = [
            (dict(base, supervisor_unavailable=True), "switch_supervisor", "codex"),
            (dict(base, context_tokens=124500), "reset_context", None),
            (dict(base), "continue", None),
            (dict(base, edited_files=["a.py"], violations=1), "remind", None),
            (dict(base, supervisor_unavailable=True, available=[], earliest_reset_seconds=3600),
             "wait", None),
            (dict(base, stagnant_turns=5), "switch_supervisor", "codex"),
            (dict(base, stagnant_turns=5, available=[]), "reset_context", None),
            (dict(base, large_code=True, violations=2), "reset_context", None),
        ]
        for obs, action, provider in cases:
            decision = keeper.decide(obs)
            self.assertEqual(decision["action"], action, obs)
            if provider:
                self.assertEqual(decision["provider"], provider)

    def test_code_lines_and_action_extraction(self):
        self.assertEqual(keeper.code_lines("x\n```py\na\nb\n```\n"), 2)
        self.assertEqual(extract_actions('Sure! {"note": 1} then {"actions": [{"op": "wait"}]}'),
                         [{"op": "wait"}])
        self.assertIsNone(extract_actions("no json"))


class ProviderOrderTests(unittest.TestCase):
    NOW = 1_789_000_000
    DAY = 86400

    def limits(self):
        return {
            # Codex: weekly window resets in 6 days; a reserve pool must be ignored.
            "codex": {"rateLimits": {
                "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": self.NOW + 3600},
                "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": self.NOW + 6 * self.DAY}},
                "rateLimitsByLimitId": {"reserve": {"primary": {
                    "usedPercent": 0, "windowDurationMins": 10080, "resetsAt": self.NOW + self.DAY}}}},
            # Claude: no durations; the latest reset (1 day) is the long window.
            "claude": {"rate_limits": {
                "five_hour": {"used_percentage": 60, "resets_at": self.NOW + 1800},
                "seven_day": {"used_percentage": 70, "resets_at": self.NOW + self.DAY}}},
            "antigravity": {},
        }

    def run_with(self, order):
        h = Harness(self)
        data = self.limits()
        h.run.limits = lambda provider: data[provider]
        h.run.clock = lambda: self.NOW
        h.run._limits_cache = {}
        h.run.save("config.json", {**h.run.load("config.json"), "provider_order": order,
                                   "supervisors": ["codex", "antigravity", "claude"]})
        return h.run

    def test_quota_expiry_uses_the_longest_window(self):
        data = self.limits()
        self.assertEqual(quota_expiry(data["codex"], self.NOW), self.NOW + 6 * self.DAY)
        self.assertEqual(quota_expiry(data["claude"], self.NOW), self.NOW + self.DAY)
        self.assertIsNone(quota_expiry({}, self.NOW))

    def test_claude_reset_parser_accepts_on_the_hour_times(self):
        from loop.adapters import _parse_claude_reset
        now = datetime(2026, 9, 14, 21, 0).timestamp()
        text = ("Current week (all models): 66% used · resets Sep 16 at 5pm (Pacific/Auckland)\n"
                "Current session: 25% used · resets Sep 14 at 11:20pm")
        self.assertEqual(_parse_claude_reset(text, now), datetime(2026, 9, 16, 17, 0).timestamp())
        self.assertEqual(_parse_claude_reset("resets Sep 14 at 11:20 pm", now),
                         datetime(2026, 9, 14, 23, 20).timestamp())
        self.assertIsNone(_parse_claude_reset("Current week: 66% used\nresets Sep 14 at 1pm", now))

    def test_expiring_first_then_unknown(self):
        run = self.run_with("expiring")
        self.assertEqual(run.available_supervisors(state={"holds": {}}),
                         ["claude", "codex", "antigravity"])
        self.assertIn("resets in 1.0 days", run.providers_table({"holds": {}}))

    def test_least_used_order(self):
        run = self.run_with("least_used")
        self.assertEqual(run.available_supervisors(state={"holds": {}}),
                         ["codex", "antigravity", "claude"])


class LiteRunTests(unittest.TestCase):
    def test_worker_can_edit_any_file_and_check_pass_commits(self):
        h = Harness(self)
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n",
                                               "public/content-index.json": "{}\n",
                                               "reports/link-graph.json": "[]\n"}))
        h.run.run(max_turns=1)
        task = h.plan()["bump"]
        self.assertEqual(task["status"], "done")
        committed = sh(h.run.worktree, "git", "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(committed), ["app.py", "public/content-index.json",
                                             "reports/link-graph.json"])
        worker_argv = h.argvs[1]
        self.assertIn("bypassPermissions", worker_argv)
        self.assertNotIn("--restricted", worker_argv)
        # The result is queued for the supervisor's next prompt and in the handoff.
        self.assertEqual(h.state()["pending"][0]["outcome"], "passed")
        self.assertIn("bump dispatched", h.run.path("handoff.md").read_text())

    def test_files_produced_by_the_check_are_not_committed(self):
        h = Harness(self)
        plan = json.loads(PLAN_AND_DISPATCH)
        plan["actions"][0]["tasks"][0]["check"] = CHECK_WITH_SIDE_EFFECT
        h.supervisor_replies.append(reply(json.dumps(plan)))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.run.run(max_turns=1)
        committed = sh(h.run.worktree, "git", "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(committed, ["app.py"])
        self.assertEqual(sh(h.run.worktree, "git", "status", "--porcelain"), "")
        h.supervisor_replies.append(reply(actions({"op": "done", "summary": "ok"})))
        self.assertTrue(h.run.run(max_turns=1)["finished"])

    def test_supervisor_edit_is_reverted_and_reminded_then_reset(self):
        h = Harness(self)

        def cheating(worktree):
            (worktree / "app.py").write_text("value = 2\n")
            (worktree / "new.py").write_text("x = 1\n")
            return 0, claude_reply(actions({"op": "note", "text": "I fixed it myself"})), ""

        h.supervisor_replies += [cheating, reply(actions({"op": "wait", "seconds": 1})), cheating]
        h.run.run(max_turns=1)
        self.assertEqual((h.run.worktree / "app.py").read_text(), "value = 1\n")
        self.assertFalse((h.run.worktree / "new.py").exists())
        self.assertEqual(h.state()["violations"], 1)
        h.run.run(max_turns=1)
        self.assertIn("Keeper reminder", h.prompts[1])
        h.run.run(max_turns=1)
        state = h.state()
        self.assertIsNone(state["session_id"])  # second violation: fresh session
        journal = h.run.path("journal.jsonl").read_text()
        self.assertIn("repeated supervisor violation", journal)

    def test_supervisor_revert_keeps_earlier_worker_changes(self):
        h = Harness(self)
        failing = actions(
            {"op": "plan", "tasks": [{"id": "bump", "title": "b", "prompt": "p", "check": "false"}]},
            {"op": "dispatch", "task": "bump", "provider": "claude", "prompt": "go"})
        h.supervisor_replies.append(reply(failing))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.run.run(max_turns=1)

        def cheating(worktree):
            (worktree / "app.py").write_text("value = 3\n")
            return 0, claude_reply(actions({"op": "wait", "seconds": 0})), ""

        h.supervisor_replies.append(cheating)
        h.run.run(max_turns=1)
        self.assertEqual((h.run.worktree / "app.py").read_text(), "value = 2\n")

    def test_rate_limited_supervisor_switches_with_state_intact(self):
        h = Harness(self)
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH, session="claude-1"))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.supervisor_replies.append(lambda w: (1, claude_reply("Claude usage limit reached",
                                                                error=True), "rate_limit"))
        h.run.run(max_turns=2)
        state = h.state()
        self.assertEqual(state["provider"], "codex")
        self.assertIsNone(state["session_id"])
        self.assertGreater(state["holds"]["claude"], 0)

        def codex_turn(worktree):
            return 0, "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "codex-1"}),
                json.dumps({"item": {"type": "agent_message", "text": actions(
                    {"op": "done", "summary": "value bumped"})}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50}})]), ""

        h.supervisor_replies.append(codex_turn)
        state = h.run.run(max_turns=1)
        fresh = h.prompts[-1]
        self.assertIn("GOAL:", fresh)
        self.assertIn("bump dispatched to claude", fresh)   # supervisor notes survived
        self.assertIn('"id": "bump"', fresh)                 # plan survived
        self.assertTrue(state["finished"])
        self.assertTrue(h.run.path("pr.md").exists())

    def test_context_threshold_resets_session(self):
        h = Harness(self, reset_at=5000)
        h.supervisor_replies.append(reply(actions({"op": "note", "text": "n"}), context=6000))
        h.run.run(max_turns=1)
        self.assertIsNone(h.state()["session_id"])
        h.supervisor_replies.append(reply(actions({"op": "note", "text": "n2"}), context=100))
        h.run.run(max_turns=1)
        self.assertIn("GOAL:", h.prompts[-1])
        self.assertEqual(h.state()["session_id"], "sess-1")
        h.supervisor_replies.append(reply(actions({"op": "note", "text": "n3"}), context=100))
        h.run.run(max_turns=1)
        self.assertTrue(h.prompts[-1].startswith("UPDATE FROM THE KEEPER"))

    def test_failing_check_outcome_comes_from_exit_code_and_discard_resets(self):
        h = Harness(self)
        h.client.reply = {"outcome": "passed", "blocker": None, "suggested_fix_command": None,
                          "progress": [], "handoff": "all good"}
        failing = actions(
            {"op": "plan", "tasks": [{"id": "bump", "title": "b", "prompt": "p",
                                      "check": "grep -q 'value = 3' app.py"}]},
            {"op": "dispatch", "task": "bump", "provider": "claude", "prompt": "go"})
        h.supervisor_replies.append(reply(failing))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.run.run(max_turns=1)
        self.assertEqual(h.plan()["bump"]["status"], "failed")
        self.assertEqual(h.state()["pending"][0]["outcome"], "failed")
        self.assertEqual((h.run.worktree / "app.py").read_text(), "value = 2\n")
        h.supervisor_replies.append(reply(actions({"op": "discard", "task": "bump"})))
        h.run.run(max_turns=1)
        self.assertEqual((h.run.worktree / "app.py").read_text(), "value = 1\n")
        self.assertEqual(h.plan()["bump"]["status"], "todo")

    def test_local_model_down_uses_fallback_summary(self):
        h = Harness(self)
        h.client.up = False
        failing = actions(
            {"op": "plan", "tasks": [{"id": "e2e", "title": "b", "prompt": "p",
                                      "check": "echo \"browserType.launch: Executable doesn't exist\" >&2; exit 1"}]},
            {"op": "dispatch", "task": "e2e", "provider": "claude", "prompt": "go"})
        h.supervisor_replies.append(reply(failing))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.run.run(max_turns=1)
        summary = h.state()["pending"][0]["summary"]
        self.assertIn("Executable doesn't exist", summary["blocker"])
        self.assertIn('"scribe": "fallback"', h.run.path("journal.jsonl").read_text())

    def test_done_with_failing_check_is_rejected(self):
        h = Harness(self)
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.run.run(max_turns=1)
        (h.run.worktree / "app.py").write_text("value = 5\n")
        sh(h.run.worktree, "git", "commit", "-qam", "regress")
        h.supervisor_replies.append(reply(actions({"op": "done", "summary": "x"})))
        state = h.run.run(max_turns=1)
        self.assertFalse(state["finished"])
        h.supervisor_replies.append(reply(actions({"op": "wait", "seconds": 0})))
        h.run.run(max_turns=1)
        self.assertIn("`done` rejected", h.prompts[-1])

    def test_supervisor_that_only_waits_is_stagnant_and_rotated(self):
        h = Harness(self)
        for index in range(4):
            h.supervisor_replies.append(reply(actions(
                {"op": "note", "text": "waiting for approval %d" % index},
                {"op": "wait", "seconds": 600})))
        slept = []
        h.run.sleep = slept.append
        h.run.run(max_turns=4)
        self.assertEqual(slept, [])                       # wait ignored: workers available
        self.assertEqual(h.state()["provider"], "codex")  # rotated after 4 idle turns
        self.assertNotIn("--permission-mode", h.argvs[0])

    def test_role_is_sent_as_system_instructions_not_plan_mode(self):
        from loop.adapters import supervisor_command
        from loop.lite import ROLE
        claude = supervisor_command("claude", "msg", instructions=ROLE, session_id="s")
        self.assertEqual(claude[claude.index("--append-system-prompt") + 1], ROLE)
        codex = supervisor_command("codex", "msg", instructions=ROLE, session_id="t")
        developer = [arg for arg in codex if arg.startswith("developer_instructions=")][0]
        self.assertEqual(json.loads(developer.split("=", 1)[1]), ROLE)
        agy = supervisor_command("antigravity", "msg", session_id="u")
        for argv in (claude, codex):
            self.assertNotIn("plan", argv)
            self.assertNotIn("--permission-mode", argv)
        # agy's default headless mode denies reads too; its plan mode reads,
        # blocks writes and still replies with actions.
        self.assertEqual(agy[agy.index("--mode") + 1], "plan")
        self.assertNotIn("--disable-slash-commands", agy)  # would silently cancel plan mode
        self.assertNotIn("--dangerously-skip-permissions", agy)

    def test_prompt_carries_role_only_for_providers_without_system_prompts(self):
        h = Harness(self, supervisors=("claude", "antigravity"))
        h.supervisor_replies.append(reply(actions({"op": "note", "text": "n"})))
        h.run.run(max_turns=1)
        self.assertNotIn("You are the SUPERVISOR", h.prompts[0])
        self.assertIn("--append-system-prompt", h.argvs[0])
        state = h.state()
        state.update(provider="antigravity", session_id=None)
        h.run.save("supervisor.json", state)
        self.assertTrue(h.run.supervisor_prompt(state).startswith("You are the SUPERVISOR"))
        state["session_id"] = "conv"
        self.assertTrue(h.run.supervisor_prompt(state).startswith("[Supervisor role"))

    def test_denied_write_by_read_only_supervisor_is_a_violation_not_a_failure(self):
        h = Harness(self)
        out = json.dumps({"conversation_id": "c1", "status": "SUCCESS",
                          "response": actions({"op": "note", "text": "tried to edit"}),
                          "denied_actions": [{"action": "write_file", "display_name": "WriteToFile"}]})
        h.run.save("supervisor.json", dict(h.state(), provider="antigravity"))
        h.supervisor_replies.append(lambda w: (0, out, ""))
        h.run.run(max_turns=1)
        state = h.state()
        self.assertEqual(state["violations"], 1)
        self.assertEqual(state["consecutive_errors"], 0)
        self.assertIn("tried to edit", h.run.path("handoff.md").read_text())

    def dispatch_models(self, *dispatches):
        h = Harness(self)
        items = [{"op": "plan", "tasks": [{"id": "t%d" % i, "title": "t", "prompt": "p",
                                           "check": "true"} for i in range(len(dispatches))]}]
        for index, (provider, model) in enumerate(dispatches):
            items.append({"op": "dispatch", "task": "t%d" % index, "provider": provider,
                          "model": model, "effort": "high", "prompt": "go"})
            h.worker_actions.append(worker_writes({"f%d.txt" % index: "x\n"}))
        h.supervisor_replies.append(reply(actions(*items)))
        h.run.save("config.json", {**h.run.load("config.json"),
                                   "workers": ["claude", "codex", "antigravity"]})
        h.run.run(max_turns=1)
        return h, h.argvs[1:1 + len(dispatches)]

    @staticmethod
    def flag(argv, name):
        return argv[argv.index(name) + 1] if name in argv else None

    def test_supervisor_picks_tiers_and_catalog_models(self):
        h, argvs = self.dispatch_models(("claude", None), ("claude", "light"),
                                        ("codex", "strong"), ("codex", "light"),
                                        ("antigravity", "standard"), ("claude", "claude-opus-5"))
        self.assertEqual(self.flag(argvs[0], "--model"), "claude-sonnet-5")
        self.assertEqual(self.flag(argvs[1], "--model"), "claude-haiku-4-5-20251001")
        self.assertIn("gpt-5.6-sol", argvs[2])
        self.assertIn("gpt-5.6-luna", argvs[3])
        self.assertIn('model_reasoning_effort="low"', argvs[3])   # catalog effort wins
        self.assertEqual(self.flag(argvs[4], "--model"), "gemini-3.8-flash-medium")
        self.assertEqual(self.flag(argvs[5], "--model"), "claude-opus-5")
        self.assertIn("light=gemini-3.8-flash-low", h.prompts[0])
        self.assertEqual(h.plan()["t2"]["history"], ["strong:codex/gpt-5.6-sol:passed"])

    def test_models_outside_catalog_are_refused(self):
        h, argvs = self.dispatch_models(("codex", "gpt-6-astra"),
                                        ("antigravity", "gemini-3.1-pro-high"))
        self.assertIn("gpt-5.6-terra", argvs[0])
        self.assertNotIn("gpt-6-astra", argvs[0])
        self.assertEqual(self.flag(argvs[1], "--model"), "gemini-3.8-flash-medium")
        notes = h.state()["notes_for_supervisor"]
        self.assertTrue(any("gpt-6-astra" in note and "not in the codex" in note for note in notes))

    def test_catalog_follows_a_config_change_without_a_restart(self):
        h = Harness(self)
        self.assertEqual(h.run.resolve_model("claude", "standard")[0]["model"], "claude-sonnet-5")
        h.run.save("config.json", {**h.run.load("config.json"),
                                   "catalog": [{"provider": "claude", "tier": "standard",
                                                "model": "claude-haiku-4-5-20251001"}]})
        self.assertEqual(h.run.resolve_model("claude", "standard")[0]["model"],
                         "claude-haiku-4-5-20251001")

    def test_catalog_drops_models_the_cli_no_longer_offers(self):
        h = Harness(self)
        h.run.models = lambda provider: ({"gemini-3.8-flash-low", "gemini-3.8-flash-high"}
                                         if provider == "antigravity" else None)
        entry, note = h.run.resolve_model("antigravity", "standard")
        self.assertEqual(entry["model"], "gemini-3.8-flash-low")   # standard gone: first left
        self.assertIn("not in the antigravity", note)
        self.assertEqual(h.run.resolve_model("claude", "strong")[0]["model"], "claude-opus-5")

    def test_stuck_codex_supervisor_escalates_to_astra_for_a_fresh_session(self):
        h = Harness(self, supervisors=("codex",))
        h.run.save("supervisor.json", dict(h.state(), provider="codex"))

        def codex_note(worktree):
            return 0, "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "c"}),
                json.dumps({"item": {"type": "agent_message",
                                     "text": actions({"op": "note", "text": "thinking"})}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}})]), ""

        for _ in range(6):
            h.supervisor_replies.append(codex_note)
        h.run.run(max_turns=5)
        models = [argv[argv.index("--model") + 1] for argv in h.argvs if "--model" in argv]
        self.assertEqual(models[:4], ["gpt-5.6-sol"] * 4)
        self.assertEqual(models[4], "gpt-6-astra")
        self.assertTrue(h.state()["escalated"])

    def test_setup_runs_once_without_a_model_and_is_not_committed(self):
        h = Harness(self)
        h.run.save("config.json", {**h.run.load("config.json"),
                                   "setup": ["mkdir -p deps && touch deps/ready", "exit 3"]})
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.run.run(max_turns=1)
        self.assertTrue(h.state()["setup_done"])
        self.assertIn("Keeper setup already ran: mkdir", h.prompts[0])
        self.assertIn("failed (exit 3)", h.prompts[0])
        committed = sh(h.run.worktree, "git", "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(committed, ["app.py"])
        h.supervisor_replies.append(reply(actions({"op": "note", "text": "n"})))
        h.run.run(max_turns=1)
        self.assertEqual(sum('"kind": "setup"' in line for line in
                             h.run.path("journal.jsonl").read_text().splitlines()), 2)

    def test_estimated_cost_is_split_by_role(self):
        h = Harness(self)

        def costly(text, usd):
            body = json.loads(claude_reply(text))
            body["total_cost_usd"] = usd
            return lambda w: (0, json.dumps(body), "")

        h.supervisor_replies.append(costly(PLAN_AND_DISPATCH, 0.25))
        h.worker_actions.append(lambda w: ((w / "app.py").write_text("value = 2\n"),
                                           costly("done", 0.5)(w))[1])
        h.run.run(max_turns=1)
        cost = h.state()["cost"]
        self.assertEqual(cost["supervisor:claude"], {"calls": 1, "estimated_usd": 0.25})
        self.assertEqual(cost["worker:claude"], {"calls": 1, "estimated_usd": 0.5})
        self.assertIn("worker:claude $0.50", h.run.status())

    def test_keeper_gate_blocks_commit_even_when_task_check_passes(self):
        h = Harness(self, gates=["test ! -e forbidden.txt"])
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n", "forbidden.txt": "x\n"}))
        h.run.run(max_turns=1)
        self.assertEqual(h.plan()["bump"]["status"], "failed")
        record = h.state()["pending"][0]
        self.assertEqual(record["outcome"], "gate_failed")
        self.assertIn("Keeper gate failed", record["output_tail"])
        self.assertEqual(sh(h.run.worktree, "git", "log", "--oneline").count("\n"), 0)  # only init
        self.assertIn("KEEPER GATES", h.prompts[0])

    def test_gate_added_mid_run_applies_to_done(self):
        h = Harness(self)
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.run.run(max_turns=1)
        h.run.add_gate("grep -q 'value = 3' app.py")
        h.supervisor_replies.append(reply(actions({"op": "done", "summary": "x"})))
        state = h.run.run(max_turns=1)
        self.assertFalse(state["finished"])
        self.assertIn("A keeper gate was added", h.prompts[-1])
        h.supervisor_replies.append(reply(actions({"op": "note", "text": "n"})))
        h.run.run(max_turns=1)
        self.assertIn("Keeper gate failed", h.prompts[-1])

    def test_audit_by_another_provider_can_block_a_passing_change(self):
        h = Harness(self, audit=True)
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.audit_replies.append(lambda w: (0, "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "a"}),
            json.dumps({"item": {"type": "agent_message", "text": json.dumps(
                {"verdict": "fail", "problems": ["app.py: value hard-coded to satisfy the check"],
                 "confidence": "high"})}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}})]), ""))
        h.run.run(max_turns=1)
        argv, prompt = h.audit_prompts[0]
        self.assertEqual(argv[0], "codex")                 # not the worker's provider (claude)
        self.assertIn("+value = 2", prompt)               # the auditor sees the diff
        record = h.state()["pending"][0]
        self.assertEqual(record["outcome"], "audit_failed")
        self.assertIn("hard-coded", record["output_tail"])
        self.assertEqual(h.plan()["bump"]["status"], "failed")
        self.assertEqual((h.run.worktree / "app.py").read_text(), "value = 2\n")  # kept, uncommitted

    def test_audit_tries_another_provider_before_giving_up(self):
        h = Harness(self, audit=True)
        h.run.save("config.json", {**h.run.load("config.json"),
                                   "workers": ["claude", "codex", "antigravity"]})
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.audit_replies.append(lambda w: (1, "", "invalid model selection"))   # first auditor errors
        h.audit_replies.append(lambda w: (0, json.dumps({                      # agy-shaped reply
            "conversation_id": "c1", "status": "SUCCESS",
            "response": json.dumps({"verdict": "fail", "problems": ["app.py: hard-coded"],
                                    "confidence": "high"})}), ""))
        h.run.run(max_turns=1)
        self.assertEqual(len(h.audit_prompts), 2)                       # fell through to the second
        self.assertEqual(h.state()["pending"][0]["outcome"], "audit_failed")
        self.assertIn('"kind": "audit_unavailable"', h.run.path("journal.jsonl").read_text())

    def test_agy_worker_model_does_not_conflict_with_effort(self):
        from loop.adapters import command
        argv = command("antigravity", "p", "gemini-3.8-flash-high", None, workspace="/tmp/x")
        self.assertNotIn("--effort", argv)
        self.assertIn("--effort", command("antigravity", "p", None, "low", workspace="/tmp/x"))

    def test_agy_auditor_model_does_not_conflict_with_effort(self):
        from loop.adapters import supervisor_command
        argv = supervisor_command("antigravity", "x", "gemini-3.8-flash-high", "medium")
        self.assertNotIn("--effort", argv)
        self.assertIn("gemini-3.8-flash-high", argv)
        self.assertIn("--effort", supervisor_command("antigravity", "x", None, "medium"))

    def test_same_provider_audit_runs_when_no_other_provider_is_free(self):
        h = Harness(self, audit=True)
        # Only the worker's own provider is configured, so there is no independent auditor.
        h.run.save("config.json", {**h.run.load("config.json"), "workers": ["claude"]})
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.audit_replies.append(lambda w: (0, claude_reply(json.dumps(
            {"verdict": "fail", "problems": ["app.py: hard-coded"], "confidence": "medium"})), ""))
        h.run.run(max_turns=1)
        record = h.state()["pending"][0]
        self.assertEqual(record["outcome"], "audit_failed")
        self.assertIn("same provider", record["audit"]["independence"])

    def test_same_provider_audit_is_used_last_and_labelled(self):
        h = Harness(self, audit=True)
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.audit_replies.append(lambda w: (1, "", "denied"))     # the independent auditor fails
        h.audit_replies.append(lambda w: (0, claude_reply(json.dumps(   # same provider, other model
            {"verdict": "pass", "problems": [], "confidence": "medium"})), ""))
        h.run.run(max_turns=1)
        record = h.state()["pending"][0]
        self.assertEqual(record["outcome"], "passed")
        self.assertIn("same provider", record["audit"]["independence"])

    def test_required_audit_blocks_when_it_cannot_run(self):
        h = Harness(self, audit=True)
        h.run.save("config.json", {**h.run.load("config.json"),
                                   "audit": {"enabled": True, "sample": 1.0, "required": True}})
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.audit_replies.append(lambda w: (1, "", "denied"))    # the other provider
        h.audit_replies.append(lambda w: (1, "", "denied"))    # same-provider fallback
        h.run.run(max_turns=1)
        record = h.state()["pending"][0]
        self.assertEqual(record["outcome"], "audit_unavailable")
        self.assertEqual(h.plan()["bump"]["status"], "todo")          # retried, not committed
        self.assertEqual(sh(h.run.worktree, "git", "log", "--oneline").count("\n"), 0)

    def test_unavailable_audit_does_not_block_progress(self):
        h = Harness(self, audit=True)
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.audit_replies.append(lambda w: (1, "", "connection reset"))
        h.audit_replies.append(lambda w: (1, "", "connection reset"))
        h.run.run(max_turns=1)
        record = h.state()["pending"][0]
        self.assertEqual(record["outcome"], "passed")
        self.assertEqual(record["audit"]["verdict"], "unavailable")
        self.assertEqual(h.plan()["bump"]["status"], "done")

    def test_templated_change_is_flagged_with_a_diff_sample(self):
        h = Harness(self)
        lines = "\n".join("reason_%d = 'Reviewed as an adjustable %s cue, not a claim of guaranteed anatomy health or outcome.'"
                          % (i, muscle) for i, muscle in enumerate(
                              ["glute", "curl", "hamstring", "calf", "lat", "quad", "chest", "trap", "delt", "ab", "neck"]))
        h.supervisor_replies.append(reply(PLAN_AND_DISPATCH))
        h.worker_actions.append(worker_writes({"app.py": "value = 2\n", "reasons.py": lines + "\n"}))
        h.run.run(max_turns=1)
        record = h.state()["pending"][0]
        self.assertIn("template_warning", record)
        self.assertGreaterEqual(record["template_warning"]["share"], 0.8)
        self.assertIn("+value = 2", record["diff_sample"])

    def test_operator_note_reaches_resumed_and_fresh_sessions(self):
        h = Harness(self)
        h.supervisor_replies.append(reply(actions({"op": "note", "text": "n"})))
        h.run.run(max_turns=1)
        h.run.note("Prefer claude-sonnet-5 for adjudication batches.")
        h.supervisor_replies.append(reply(actions({"op": "note", "text": "n2"})))
        h.run.run(max_turns=1)
        self.assertIn("OPERATOR NOTE", h.prompts[-1])
        self.assertTrue(h.prompts[-1].startswith("UPDATE FROM THE KEEPER"))
        state = h.state()
        state["session_id"] = None
        h.run.save("supervisor.json", state)
        h.supervisor_replies.append(reply(actions({"op": "note", "text": "n3"})))
        h.run.run(max_turns=1)
        self.assertIn("OPERATOR NOTES (standing instructions", h.prompts[-1])
        self.assertIn("Prefer claude-sonnet-5", h.prompts[-1])
        self.assertEqual(h.run.load("supervisor.json")["inbox_seen"], 1)   # not re-delivered

    def test_template_report_ignores_tables_and_short_changes(self):
        self.assertIsNone(keeper.template_report(["| a | b |"] * 30))
        self.assertIsNone(keeper.template_report(["A single honest sentence about this exact item."] * 3))
        varied = ["Claim %d is supported by the cited trial measuring %s in trained adults." % (i, w)
                  for i, w in enumerate("strength power mass speed endurance balance grip reach flexibility sprint".split())]
        report = keeper.template_report(varied)
        self.assertIsNotNone(report)   # same ending on all of them: reported, share decides

    def test_repeated_task_failures_prompt_a_change_of_approach(self):
        h = Harness(self)
        failing = actions(
            {"op": "plan", "tasks": [{"id": "bump", "title": "b", "prompt": "p", "check": "false"}]},
            {"op": "dispatch", "task": "bump", "provider": "claude", "prompt": "go"})
        for _ in range(3):
            h.supervisor_replies.append(reply(failing))
            h.worker_actions.append(worker_writes({"app.py": "value = 2\n"}))
        h.run.run(max_turns=3)
        self.assertEqual(h.plan()["bump"]["attempts"], 3)
        self.assertTrue(any("failed 3 attempts" in note and "split it into smaller tasks" in note
                            for note in h.state()["notes_for_supervisor"]))

    def test_invalid_json_and_repeats_rotate_supervisor(self):
        h = Harness(self)
        for _ in range(4):
            h.supervisor_replies.append(reply("I think we should look at the code first."))
        h.run.run(max_turns=4)
        self.assertEqual(h.state()["provider"], "codex")
        self.assertIn("stagnant", h.run.path("journal.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
