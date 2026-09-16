"""Lite loop: a keeper keeps a read-only cloud supervisor delegating to workers.

    keeper (Python rules + local-model scribe)
      └─ supervisor turn: read-only, resumable CLI session → JSON actions
           └─ worker: full-tool CLI in the run's worktree; its check passing → commit

Everything the supervisor needs lives in ``.agent-loop/lite/<id>/``: the goal,
the plan, a handoff brief and the results it has not seen yet. A supervisor
session is therefore disposable — when its context grows too large, its
provider runs out of quota, or it keeps doing the work itself, the keeper starts
a fresh session (possibly on another provider) from those files.

The loop does not park. Failures are reported to the supervisor, which decides
what to dispatch next; the run ends only when ``done`` verifies or on Ctrl-C.
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import keeper
from .adapters import (SYSTEM_INSTRUCTIONS, command, context_tokens, parse, provider_limits,
                       quota_expiry,
                       run_process, session_id, supervisor_command, token_total,
                       usage_fraction)
from .storage import atomic_json

PROVIDERS = ("claude", "codex", "antigravity")
CLI = {"claude": "claude", "codex": "codex", "antigravity": "agy"}
DEFAULTS = {
    "supervisors": ["claude", "codex", "antigravity"],
    "workers": ["claude", "codex", "antigravity"],
    # Supervisor models per provider, and the stronger planning model used only
    # for a fresh session after the supervisor got stuck ("if needed").
    "supervisor_models": {"codex": "gpt-5.6-sol"},
    "supervisor_escalation": {"codex": "gpt-6-astra"},
    "supervisor_effort": "medium",
    # Worker model catalog: the only models a dispatch may use. The supervisor picks
    # a tier or an exact model; anything else resolves to the provider's standard tier.
    "catalog": None,
    # Shell commands run by the keeper once in a new worktree (dependency installs),
    # so no model call is spent on mechanical setup.
    "setup": [],
    "setup_timeout_seconds": 1800,
    # "expiring": the provider whose long quota window resets soonest goes first, so
    # allowance that would be lost at reset is spent first. "least_used": lowest usage.
    "provider_order": "expiring",
    "keeper_model": keeper.DEFAULT_KEEPER_MODEL,
    "reset_at_tokens": 120000,
    "stagnant_limit": 4,
    "supervisor_idle_seconds": 600,
    "supervisor_max_seconds": 3600,
    "worker_idle_seconds": 900,
    "worker_max_seconds": 14400,
    "check_timeout_seconds": 1200,
    "unknown_retry_seconds": 1800,
    "fresh_prompt_characters": 60000,
    # Keeper-owned acceptance gates: shell commands that must pass, in addition to a
    # task's own check, before any task is committed and before `done`. The
    # supervisor writes task checks and cannot see or weaken these.
    "gates": [],
    # After checks and gates pass, a different provider audits the diff read-only
    # for work that satisfies checks without doing the task. sample is the share of
    # passing attempts audited (1.0 = every one).
    "audit": {"enabled": True, "sample": 1.0},
    # A change where one sentence pattern makes up at least this share of its prose
    # is flagged to the supervisor and auditor as likely templated.
    "template_share": 0.2,
    "audit_diff_characters": 30000,
    "result_diff_characters": 4000,
}
ROLE = """You are the SUPERVISOR of an automated coding run. You plan, delegate and review.
You may read the repository (read-only tools). You must NOT edit files, run builds, or write
the implementation yourself: every change is made by a worker you dispatch. Keep dispatch
prompts as clear instructions (goal, relevant files, constraints, how to verify), not code.

Workers are full coding agents in the worktree below. They can edit any file, run builds,
tests, generators and dependency installs. Environment problems (missing browsers, packages)
are also fixed by dispatching a worker. Each task may have a `check` shell command; when a
worker finishes and the check passes, the keeper commits all changes for that task. A failed
check leaves the changes uncommitted: dispatch a follow-up for the same task (it continues from
the tree) or `discard` them. Tasks without a check need you to `accept` them after review.

This is not a plan-approval session: nobody will approve your plan. Your actions are executed
as soon as you reply. `dispatch` runs the worker to completion before your next turn, so there is
nothing to wait for — the next update you receive contains its result. Use `wait` only when
every worker provider is unavailable until a quota reset.

Models: choose each dispatch's `model` from the WORKER MODELS catalog, by tier name ("light",
"standard", "strong") or exact model id. Pick the lowest tier likely to pass first time: light
for mechanical edits, config, docs and simple tests; standard for ordinary features, tests and
fixes; strong for hard debugging, cross-cutting changes, or a task that already failed at a
lower tier. Among equal choices prefer the provider whose quota resets soonest. Models outside
the catalog are not allowed; omitting `model` means standard.
Don't dispatch a worker just to install dependencies or run a command the keeper's setup
already ran (see KEEPER NOTES).

Verify, don't trust. Workers optimise for passing whatever check you write, and a check that
only looks at shape (a file exists, a field is set, a validator exits 0) can pass with no real
work done. Write checks that test substance. Each result includes a DIFF SAMPLE, and you can
read files in the worktree: before relying on "passed", look at what actually changed. Treat
these as failures to re-dispatch with specific feedback:
- output where many items follow one template when each should be individual
- invented references or ids
- checks satisfied by special-casing
- unchanged dates or text passed off as new work
The keeper may also run its own GATES and an independent AUDIT by another model. An
"audit_failed" or "gate_failed" outcome means the change was not committed; its problems are
in the result.

Keep your notes current: the keeper may restart you in a fresh session, or on a different
model, at any time. Your `note` is the only memory that survives besides the plan and results.

Reply with ONE JSON object and nothing else:
{"actions": [
  {"op": "plan", "tasks": [{"id": "short-slug", "title": "...", "prompt": "...", "check": "shell command or null"}]},
  {"op": "dispatch", "task": "id", "provider": "claude|codex|antigravity", "model": "light|standard|strong or a catalog model id", "prompt": "instructions for this attempt"},
  {"op": "accept", "task": "id"},
  {"op": "discard", "task": "id"},
  {"op": "note", "text": "what is done, what you learned, what is next (<= 300 words)"},
  {"op": "wait", "seconds": 600},
  {"op": "done", "summary": "..."}
]}
Actions run in order. `plan` upserts tasks (set "status": "dropped" to drop one). Dispatch at
most one or two workers per reply, then wait for their results. Only send `done` when every
remaining task is done or dropped and the worktree is clean."""

ROLE_REMINDER = ("[Supervisor role: plan and delegate only; never edit files or run builds. "
                 "Actions execute immediately, dispatch is synchronous, nobody approves plans. "
                 "Reply with one JSON actions object.]")

# Tiers are the supervisor's vocabulary; model ids are what the CLIs accept.
# Workers never use gpt-6-astra (reserved for supervisor planning) or Gemini Pro.
DEFAULT_CATALOG = [
    {"provider": "claude", "tier": "light", "model": "claude-haiku-4-5-20251001"},
    {"provider": "claude", "tier": "standard", "model": "claude-sonnet-5"},
    {"provider": "claude", "tier": "strong", "model": "claude-opus-5"},
    {"provider": "codex", "tier": "light", "model": "gpt-5.6-luna", "effort": "low"},
    {"provider": "codex", "tier": "standard", "model": "gpt-5.6-terra"},
    {"provider": "codex", "tier": "strong", "model": "gpt-5.6-sol"},
    {"provider": "antigravity", "tier": "light", "model": "gemini-3.8-flash-low"},
    {"provider": "antigravity", "tier": "standard", "model": "gemini-3.8-flash-medium"},
    {"provider": "antigravity", "tier": "strong", "model": "gemini-3.8-flash-high"},
]


def available_models(provider):
    """Model ids a CLI currently offers, or None when it has no readable list."""
    if provider == "antigravity":
        code, out, _ = run_process(["agy", "models"], Path.home(), 30)
        return None if code else {line.split()[0] for line in out.splitlines()
                                  if line.strip() and not line.startswith("Fetching")}
    if provider == "codex":
        data = json.loads((Path.home() / ".codex" / "models_cache.json").read_text())
        models = data.get("models", data) if isinstance(data, dict) else data
        return {item["slug"] for item in models if isinstance(item, dict) and item.get("slug")}
    return None  # claude has no model list command


def git(cwd, *args):
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True,
                                   stderr=subprocess.STDOUT).strip()


def changed_files(workspace):
    names = set(git(workspace, "diff", "HEAD", "--name-only").splitlines())
    names.update(git(workspace, "ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(name for name in names if name)


def slug(text):
    value = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return value[:40].strip("-") or "run"


def extract_actions(text):
    """The first JSON object in a reply that carries an ``actions`` list."""
    cleaned = str(text or "")
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("actions"), list):
            return [item for item in value["actions"] if isinstance(item, dict)]
    return None


def check_argv(check):
    if not check:
        return None
    if isinstance(check, list) and all(isinstance(arg, str) for arg in check):
        return check
    return ["/bin/sh", "-c", str(check)]


def tail(text, lines=60, characters=4000):
    return "\n".join(str(text or "").splitlines()[-lines:])[-characters:]


AUDIT_ROLE = """You are an independent AUDITOR in an automated coding run. A worker finished a task and
the task's checks passed. Checks can be passed without doing the work. Judge whether this change
really does what the task asks. You are read-only: do not edit anything.

Look for concrete problems:
- templated or boilerplate output repeated across items that each needed individual work
- invented or mismatched content: citations, ids, data, dates changed or left stale to look new
- checks or validators made to pass by weakening, bypassing or special-casing them
- work claimed in the task but absent from the diff
- unrelated or destructive changes

You may read files in the worktree to confirm. Fail only for problems you can point to with file
and line evidence; do not fail for style.

Reply with ONE JSON object and nothing else:
{"verdict": "pass" or "fail", "problems": ["<file>: <specific problem and evidence>"], "confidence": "low|medium|high"}"""


def extract_verdict(text):
    decoder = json.JSONDecoder()
    for index, char in enumerate(str(text or "")):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(str(text)[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("verdict") in ("pass", "fail"):
            return value
    return None


class LiteRun:
    def __init__(self, repo, run_id, invoke=None, limits=None, scribe=None, sleep=None,
                 clock=None, out=None, models=None):
        self.repo = Path(repo).resolve()
        self.id = run_id
        self.dir = self.repo / ".agent-loop" / "lite" / run_id
        self.worktree = self.repo / ".agent-loop" / "worktrees" / ("lite-" + run_id)
        self.branch = "lite/" + run_id
        self.invoke = invoke or self._invoke
        self.limits = limits or (lambda provider: provider_limits(provider, self.repo, 30))
        self.sleep = sleep or time.sleep
        self.clock = clock or time.time
        self.out = out or (lambda message: print(message, flush=True))
        self._scribe = scribe
        self._limits_cache = {}
        self.models = models or available_models
        self._catalog = None

    # ---- durable state -------------------------------------------------
    def path(self, name):
        return self.dir / name

    def load(self, name, default=None):
        try:
            return json.loads(self.path(name).read_text())
        except (OSError, ValueError):
            return default

    def save(self, name, value):
        atomic_json(self.path(name), value)

    def journal(self, kind, **detail):
        record = {"at": round(self.clock(), 3), "kind": kind, **detail}
        with self.path("journal.jsonl").open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        brief = ", ".join("%s=%s" % (key, str(value)[:120]) for key, value in detail.items())
        self.out("[lite %s] %s %s" % (self.id, kind, brief))

    @property
    def config(self):
        return {**DEFAULTS, **self.load("config.json", {})}

    @property
    def scribe(self):
        if self._scribe is None:
            self._scribe = keeper.Scribe(keeper.Ollama(self.config["keeper_model"]))
        return self._scribe

    def create(self, goal, base="HEAD", options=None):
        if self.path("goal.md").exists():
            raise ValueError("Run %s already exists; use `lite resume %s`" % (self.id, self.id))
        options = {key: value for key, value in (options or {}).items() if value is not None}
        self.ensure_excluded()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "logs").mkdir(exist_ok=True)
        base_sha = git(self.repo, "rev-parse", base + "^{commit}")
        if not self.worktree.exists():
            self.worktree.parent.mkdir(parents=True, exist_ok=True)
            git(self.repo, "worktree", "add", "-b", self.branch, str(self.worktree), base_sha)
        self.path("goal.md").write_text(goal.strip() + "\n")
        self.save("config.json", {**{k: v for k, v in DEFAULTS.items()}, **options})
        self.save("plan.json", {"tasks": []})
        self.save("handoff.json", {"notes": "", "results": []})
        first = self.available_supervisors(exclude=None)
        self.save("supervisor.json", {
            "provider": first[0] if first else self.config["supervisors"][0],
            "session_id": None, "context_tokens": 0, "turns_in_session": 0,
            "violations": 0, "stagnant_turns": 0, "consecutive_errors": 0,
            "last_actions": "", "holds": {}, "pending": [], "notes_for_supervisor": [],
            "finished": False, "base": base_sha, "tokens": {}})
        self.render_handoff()
        self.journal("created", goal=goal[:200], base=base_sha, worktree=str(self.worktree))

    def ensure_excluded(self):
        path = Path(git(self.repo, "rev-parse", "--git-path", "info/exclude"))
        if not path.is_absolute():
            path = self.repo / path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_text() if path.exists() else ""
        if "/.agent-loop/" not in content.splitlines():
            with path.open("a") as stream:
                stream.write(("\n" if content and not content.endswith("\n") else "")
                             + "/.agent-loop/\n")

    # ---- providers -----------------------------------------------------
    def _invoke(self, argv, cwd, timeout, idle_timeout=None, activity=None, tee=None):
        return run_process(argv, cwd, timeout, idle_timeout=idle_timeout, activity=activity,
                           tee=tee)

    def telemetry(self, provider):
        """(usage fraction, long-window reset timestamp), each None when unknown."""
        cached = self._limits_cache.get(provider)
        if cached and self.clock() - cached[0] < 600:
            return cached[1]
        try:
            data = self.limits(provider)
            value = (usage_fraction(data), quota_expiry(data, self.clock()))
        except Exception:  # telemetry is advisory; unknown is not a failure
            value = (None, None)
        self._limits_cache[provider] = (self.clock(), value)
        return value

    def usage(self, provider):
        return self.telemetry(provider)[0]

    def held_until(self, state, provider):
        return state["holds"].get(provider, 0) if state["holds"].get(provider, 0) > self.clock() else 0

    def available(self, candidates, state, exclude=None):
        """Usable providers in ``provider_order``.

        "expiring": soonest long-window reset first, unknown resets after known
        ones; "least_used": lowest usage first. Unknown usage sorts as 50%, and
        the configured list order breaks ties.
        """
        expiring = self.config.get("provider_order", "expiring") == "expiring"
        choices = []
        for order, provider in enumerate(candidates):
            if provider == exclude or provider not in PROVIDERS:
                continue
            if state and self.held_until(state, provider):
                continue
            if self.invoke == self._invoke and not shutil.which(CLI[provider]):
                continue
            used, expiry = self.telemetry(provider)
            if used is not None and used >= 0.98:
                continue
            usage_key = 0.5 if used is None else used
            key = ((expiry is None, expiry or 0, usage_key, order) if expiring
                   else (usage_key, order))
            choices.append((key, provider))
        return [provider for _, provider in sorted(choices)]

    def available_supervisors(self, exclude=None, state=None):
        return self.available(self.config["supervisors"], state, exclude)

    def hold(self, state, provider, until, reason):
        state["holds"][provider] = until
        self.journal("provider_held", provider=provider, until=round(until), reason=reason[-300:])

    # ---- prompts -------------------------------------------------------
    def render_handoff(self):
        handoff = self.load("handoff.json", {"notes": "", "results": []})
        lines = ["# Handoff: " + self.id, "", "## Supervisor notes", "",
                 handoff["notes"] or "(none yet)", "", "## Recent results", ""]
        for item in handoff["results"][-12:]:
            summary = item.get("summary", {})
            lines.append("- %s via %s: **%s**. %s" % (item["task"], item["provider"],
                                                      item["outcome"], summary.get("handoff", "")))
            if summary.get("blocker"):
                lines.append("  - blocker: " + summary["blocker"])
            if summary.get("suggested_fix_command"):
                lines.append("  - hint: " + summary["suggested_fix_command"])
            for bullet in summary.get("progress", []):
                lines.append("  - " + bullet)
        text = "\n".join(lines) + "\n"
        self.path("handoff.md").write_text(text)
        return text

    def account(self, state, role, provider, result):
        """Estimated spend by role. Only Claude reports a figure; others count calls."""
        cost = state.setdefault("cost", {})
        entry = cost.setdefault(role + ":" + provider, {"calls": 0, "estimated_usd": 0.0})
        entry["calls"] += 1
        entry["estimated_usd"] = round(entry["estimated_usd"] + (result.estimated_usd or 0), 4)

    def run_setup(self, state):
        """Run configured setup commands once per worktree; failures are reported, not fatal."""
        if state.get("setup_done") or not self.config["setup"]:
            return
        for command_text in self.config["setup"]:
            self.journal("setup", command=command_text)
            code, out, err = run_process(["/bin/sh", "-c", command_text], self.worktree,
                                         self.config["setup_timeout_seconds"])
            if code:
                state["notes_for_supervisor"].append(
                    "Keeper setup command failed (exit %d): %s\n%s"
                    % (code, command_text, tail(out + "\n" + err, 30)))
                self.journal("setup_failed", command=command_text, code=code)
            else:
                state["notes_for_supervisor"].append("Keeper setup already ran: " + command_text)
        # Setup output (installed packages) must never be committed as task work.
        self.revert_to((git(self.worktree, "rev-parse", "HEAD"), {}))
        state["setup_done"] = True
        self.save("supervisor.json", state)

    # ---- evidence, gates, audit, operator inbox -------------------------------
    def diff_text(self, limit):
        """Uncommitted change as a diff, sampled evenly across files within ``limit``."""
        files = changed_files(self.worktree)
        if not files:
            return "", []
        tracked = set(git(self.worktree, "ls-files", "--", *files).splitlines())
        chunks, added = [], []
        for name in files:
            if name in tracked:
                body = git(self.worktree, "diff", "HEAD", "--", name)
            else:
                target = self.worktree / name
                try:
                    content = target.read_text(errors="replace") if target.is_file() else ""
                except OSError:
                    content = ""
                body = "new file " + name + "\n" + "\n".join("+" + line for line in content.splitlines())
            added += [line[1:] for line in body.splitlines()
                      if line.startswith("+") and not line.startswith("+++")]
            chunks.append((name, body))
        share = max(400, limit // max(1, len(chunks)))
        text = ""
        for name, body in chunks:
            piece = body if len(body) <= share else body[:share] + "\n[... %d more characters in %s]\n" % (len(body) - share, name)
            if len(text) + len(piece) > limit:
                text += "\n[%d more files not shown]\n" % (len(chunks) - chunks.index((name, body)))
                break
            text += piece + "\n"
        return text, added

    def run_gates(self):
        """(ok, output) for every keeper gate on the current tree."""
        outputs = []
        for gate in self.config["gates"]:
            code, out, err = run_process(["/bin/sh", "-c", gate], self.worktree,
                                         self.config["check_timeout_seconds"])
            if code:
                return False, "Keeper gate failed (exit %d): %s\n%s" % (code, gate, tail(out + "\n" + err, 40))
            outputs.append("gate ok: " + gate)
        return True, "\n".join(outputs)

    def audit(self, state, task, worker_provider, diff, template):
        """Independent read-only review of a passing change; never blocks on its own failure."""
        settings = {"enabled": True, "sample": 1.0, **(self.config.get("audit") or {})}
        if not settings["enabled"] or not diff:
            return {"verdict": "skipped"}
        digest = int(hashlib.sha256((task["id"] + str(task.get("attempts"))).encode()).hexdigest(), 16)
        if (digest % 1000) / 1000.0 >= float(settings["sample"]):
            return {"verdict": "skipped", "reason": "not sampled"}
        candidates = [name for name in self.available(self.config["workers"], state)
                      if name != worker_provider]
        if not candidates:
            return {"verdict": "unavailable", "reason": "no provider other than the worker's is available"}
        auditor = candidates[0]
        model = self.resolve_model(auditor, "standard")[0]["model"]
        prompt = ("AUDIT REQUEST\n\nTASK " + task["id"] + ": " + (task.get("title") or "") + "\n"
                  + (task.get("prompt") or "") + "\n\nTASK CHECK: " + str(task.get("check"))
                  + "\nKEEPER GATES: " + json.dumps(self.config["gates"])
                  + ("\n\nTEMPLATE WARNING: %d of %d prose lines share the pattern %r."
                     % (template["count"], template["prose_lines"], template["pattern"]) if template else "")
                  + "\n\nWORKTREE: " + str(self.worktree) + "\n\nDIFF:\n" + diff)
        if auditor not in SYSTEM_INSTRUCTIONS:
            prompt = AUDIT_ROLE + "\n\n" + prompt
        argv = supervisor_command(auditor, prompt, model, "medium", None, self.worktree,
                                  instructions=AUDIT_ROLE if auditor in SYSTEM_INSTRUCTIONS else None)
        snap = self.snapshot()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log = self.dir / "logs" / ("audit-%s-%s-%s.jsonl" % (task["id"], stamp, auditor))
        self.journal("audit", task=task["id"], auditor=auditor, model=model or "default")
        code, out, err = self.invoke(argv, self.worktree, self.config["supervisor_max_seconds"],
                                     idle_timeout=self.config["supervisor_idle_seconds"],
                                     tee=(log, log.with_suffix(".stderr")))
        result = parse(auditor, code, out, err, now=self.clock())
        self.revert_to(snap)
        self.account(state, "audit", auditor, result)
        if result.status in ("rate_limited", "provider_wait", "auth_required"):
            self.hold(state, auditor, result.retry_at or self.clock() + self.config["unknown_retry_seconds"],
                      result.error or result.status)
        verdict = extract_verdict(result.response) if result.status == "ok" else None
        if verdict is None:
            return {"verdict": "unavailable", "auditor": auditor,
                    "reason": (result.error or "no verdict in reply")[-300:]}
        verdict["auditor"] = auditor
        verdict["problems"] = [str(item)[:400] for item in (verdict.get("problems") or [])][:15]
        return verdict

    def verify_passing_change(self, state, task, worker_provider):
        """Gates, template detection and audit for a change whose own check passed.

        Returns (outcome, evidence text, extra) where outcome is "passed",
        "gate_failed" or "audit_failed".
        """
        diff, added = self.diff_text(self.config["audit_diff_characters"])
        template = keeper.template_report(added)
        if template and template["share"] < float(self.config["template_share"]):
            template = None
        extra = {"diff_sample": diff[:self.config["result_diff_characters"]]}
        if template:
            extra["template_warning"] = template
        ok, gate_output = self.run_gates()
        if not ok:
            return "gate_failed", gate_output, extra
        verdict = self.audit(state, task, worker_provider, diff, template)
        extra["audit"] = verdict
        self.journal("audit_result", task=task["id"], verdict=verdict.get("verdict"),
                     problems=len(verdict.get("problems") or []))
        if verdict.get("verdict") == "fail":
            return "audit_failed", "Independent audit failed:\n" + "\n".join(
                "- " + problem for problem in verdict.get("problems") or []), extra
        return "passed", gate_output, extra

    def note(self, text):
        """Queue an operator note; the keeper delivers it before the next supervisor turn."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path("inbox.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": round(self.clock(), 3), "text": str(text)}) + "\n")

    def add_gate(self, command_text):
        config = self.load("config.json", {})
        gates = list(config.get("gates") or [])
        if command_text not in gates:
            gates.append(command_text)
        config["gates"] = gates
        self.save("config.json", config)
        self.note("A keeper gate was added and must pass before any commit and before done: "
                  + command_text)

    def read_inbox(self, state):
        """Operator notes appended by `lite note` since the last read."""
        path = self.path("inbox.jsonl")
        if not path.exists():
            return
        lines = path.read_text().splitlines()
        seen = state.get("inbox_seen", 0)
        for line in lines[seen:]:
            try:
                note = json.loads(line)
            except ValueError:
                continue
            text = str(note.get("text") or "").strip()
            if not text:
                continue
            state.setdefault("operator_notes", []).append(text)
            state["notes_for_supervisor"].append("OPERATOR NOTE (from the person running this loop): " + text)
            self.journal("operator_note", text=text[:200])
        state["inbox_seen"] = len(lines)

    def catalog(self):
        """Worker catalog entries still offered by the installed CLIs."""
        if self._catalog is None:
            entries = self.config["catalog"] or DEFAULT_CATALOG
            offered = {}
            for provider in {entry["provider"] for entry in entries}:
                try:
                    offered[provider] = self.models(provider)
                except Exception:  # an unreadable list proves nothing; keep entries
                    offered[provider] = None
            kept = []
            for entry in entries:
                listed = offered.get(entry["provider"])
                if listed is not None and entry["model"] not in listed:
                    self.journal("catalog_model_unavailable", provider=entry["provider"],
                                 model=entry["model"])
                    continue
                kept.append(entry)
            self._catalog = kept
        return self._catalog

    def resolve_model(self, provider, requested):
        """(entry, note) for a dispatch. Never returns a model outside the catalog."""
        entries = [entry for entry in self.catalog() if entry["provider"] == provider]
        if not entries:
            return {"provider": provider, "tier": "default", "model": None}, ""
        by_tier = {entry["tier"]: entry for entry in entries}
        fallback = by_tier.get("standard") or entries[0]
        if not requested:
            return fallback, ""
        if requested in by_tier:
            return by_tier[requested], ""
        for entry in entries:
            if entry["model"] == requested:
                return entry, ""
        return fallback, ("Model %r is not in the %s worker catalog; used %s (%s) instead."
                          % (requested, provider, fallback["model"], fallback["tier"]))

    def providers_table(self, state):
        rows = []
        for provider in self.config["workers"]:
            used, expiry = self.telemetry(provider)
            held = self.held_until(state, provider)
            tiers = ", ".join("%s=%s%s" % (entry["tier"], entry["model"],
                                           " (effort %s)" % entry["effort"] if entry.get("effort") else "")
                              for entry in self.catalog() if entry["provider"] == provider)
            rows.append("- %s: usage %s, long quota window resets %s%s\n    models: %s" % (
                provider, "unknown" if used is None else "%d%%" % round(used * 100),
                "in %.1f days" % ((expiry - self.clock()) / 86400) if expiry else "at an unknown time",
                ", UNAVAILABLE for %d min" % ((held - self.clock()) // 60) if held else "",
                tiers or "CLI default"))
        return "\n".join(rows)

    def plan_text(self):
        tasks = self.load("plan.json", {"tasks": []})["tasks"]
        compact = [{key: task.get(key) for key in
                    ("id", "title", "status", "attempts", "check", "last_outcome", "history")}
                   for task in tasks]
        return json.dumps(compact, indent=1)

    def worktree_text(self):
        status = git(self.worktree, "status", "--short")
        log = git(self.worktree, "log", "--oneline", "-8")
        return ("path: %s\nbranch: %s\nuncommitted:\n%s\nrecent commits:\n%s"
                % (self.worktree, self.branch, status or "(clean)", log))

    def supervisor_prompt(self, state):
        """The user message for a supervisor turn.

        The role itself goes in system/developer instructions where the CLI has
        them; otherwise it leads a fresh prompt and is restated briefly on resume.
        """
        results = json.dumps(state["pending"], indent=1) if state["pending"] else "(none)"
        notes = "\n".join(state["notes_for_supervisor"])
        in_prompt = state["provider"] not in SYSTEM_INSTRUCTIONS
        if state["session_id"]:
            return ((ROLE_REMINDER + "\n\n" if in_prompt else "")
                    + "UPDATE FROM THE KEEPER\n\nNEW WORKER RESULTS:\n" + results
                    + "\n\nPLAN:\n" + self.plan_text() + "\n\nWORKTREE:\n" + self.worktree_text()
                    + "\n\nWORKER PROVIDERS:\n" + self.providers_table(state)
                    + ("\n\nKEEPER NOTES:\n" + notes if notes else "")
                    + "\n\nReply with the JSON actions object only.")
        sections = [
            ROLE + "\n\n" if in_prompt else "",
            "GOAL:\n" + self.path("goal.md").read_text(),
            ("\n\nOPERATOR NOTES (standing instructions from the person running this loop):\n- "
             + "\n- ".join(state.get("operator_notes", [])) if state.get("operator_notes") else ""),
            ("\n\nKEEPER GATES (must pass before any commit and before done): "
             + json.dumps(self.config["gates"]) if self.config["gates"] else ""),
            "\n\nKEEPER NOTES:\n" + notes if notes else "",
            "\n\nPLAN:\n" + self.plan_text(),
            "\n\nWORKTREE:\n" + self.worktree_text(),
            "\n\nWORKER PROVIDERS:\n" + self.providers_table(state),
            "\n\nNEW WORKER RESULTS:\n" + results,
            "\n\nHANDOFF (your notes and summarised earlier results):\n" + self.render_handoff(),
        ]
        text, budget = "", self.config["fresh_prompt_characters"]
        for section in sections:
            room = budget - len(text)
            text += section if len(section) <= room else section[:max(0, room - 40)] + "\n[trimmed]\n"
        return text

    # ---- supervisor turn ----------------------------------------------
    def snapshot(self):
        head = git(self.worktree, "rev-parse", "HEAD")
        files = {}
        for name in changed_files(self.worktree):
            target = self.worktree / name
            files[name] = target.read_bytes() if target.is_file() else None
        return head, files

    def revert_to(self, snap):
        """Undo anything a supervisor turn changed, keeping earlier worker edits."""
        head, files = snap
        edited = []
        if git(self.worktree, "rev-parse", "HEAD") != head:
            git(self.worktree, "reset", "--mixed", head)
            edited.append("(git history)")
        for name in changed_files(self.worktree):
            target = self.worktree / name
            current = target.read_bytes() if target.is_file() else None
            if name in files:
                if current != files[name]:
                    edited.append(name)
                    if files[name] is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.write_bytes(files[name])
                continue
            edited.append(name)
            if git(self.worktree, "ls-files", "--", name):
                git(self.worktree, "checkout", "HEAD", "--", name)
            elif target.is_file() or target.is_symlink():
                target.unlink()
        for name, content in files.items():
            target = self.worktree / name
            if content is not None and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                edited.append(name)
        return sorted(set(edited))

    def supervisor_turn(self, state):
        config = self.config
        provider = state["provider"]
        prompt = self.supervisor_prompt(state)
        model = (config["supervisor_escalation"].get(provider) if state.get("escalated") else None) \
            or config["supervisor_models"].get(provider)
        argv = supervisor_command(provider, prompt, model,
                                  config["supervisor_effort"], state["session_id"], self.worktree,
                                  instructions=ROLE if provider in SYSTEM_INSTRUCTIONS else None)
        snap = self.snapshot()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tee = (self.dir / "logs" / ("supervisor-%s-%s.jsonl" % (stamp, provider)),
               self.dir / "logs" / ("supervisor-%s-%s.stderr" % (stamp, provider)))
        self.journal("supervisor_turn", provider=provider, model=model or "default",
                     session="resume" if state["session_id"] else "fresh", prompt_chars=len(prompt))
        code, out, err = self.invoke(argv, self.worktree, config["supervisor_max_seconds"],
                                     idle_timeout=config["supervisor_idle_seconds"], tee=tee)
        result = parse(provider, code, out, err, now=self.clock())
        edited = self.revert_to(snap)
        denied = re.search(r"Provider denied worker actions: ([^.]*)", result.error or "")
        if denied and result.status == "error" and extract_actions(result.response) is not None:
            # A read-only session refused the supervisor's attempt to write or run
            # something. The reply is still usable; the attempt is a violation.
            result.status, result.error = "ok", ""
            edited.append("(attempted: " + denied.group(1) + ")")
        new_session = session_id(provider, out)
        if result.status == "ok" and new_session:
            state["session_id"] = new_session
        state["turns_in_session"] += 1
        if result.usage:
            state["context_tokens"] = context_tokens(provider, result.usage)
            state["tokens"][provider] = state["tokens"].get(provider, 0) + token_total(provider, result.usage)
        self.account(state, "supervisor", provider, result)
        if result.status == "ok":
            state["notes_for_supervisor"] = []
        return result, edited

    # ---- actions -------------------------------------------------------
    def task(self, plan, task_id):
        for task in plan["tasks"]:
            if task["id"] == task_id:
                return task
        return None

    def apply_plan(self, action):
        plan = self.load("plan.json", {"tasks": []})
        for item in action.get("tasks") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            task_id = slug(item["id"])
            task = self.task(plan, task_id)
            if task is None:
                task = {"id": task_id, "title": "", "prompt": "", "check": None,
                        "status": "todo", "attempts": 0}
                plan["tasks"].append(task)
            if task["status"] == "done":
                continue
            for key in ("title", "prompt", "check"):
                if key in item:
                    task[key] = item[key]
            if item.get("status") in ("todo", "dropped"):
                task["status"] = item["status"]
        self.save("plan.json", plan)
        self.journal("plan_updated", tasks=len(plan["tasks"]))

    def commit(self, task_id, paths):
        """Commit the worker's changes, then drop anything the check itself produced."""
        if paths:
            git(self.worktree, "add", "-A", "--", *paths)
        if not git(self.worktree, "diff", "--cached", "--name-only"):
            head = git(self.worktree, "rev-parse", "HEAD")
            self.revert_to((head, {}))
            return head
        git(self.worktree, "commit", "-q", "-m", "lite: " + task_id,
            "-m", "Agent-Loop-Lite: " + self.id + "/" + task_id)
        head = git(self.worktree, "rev-parse", "HEAD")
        self.revert_to((head, {}))
        return head

    def activity(self):
        latest = 0.0
        for root, dirs, files in os.walk(self.worktree):
            dirs[:] = [name for name in dirs if name not in (".git", "node_modules")]
            for name in files:
                try:
                    latest = max(latest, os.stat(os.path.join(root, name)).st_mtime)
                except OSError:
                    pass
        return latest

    def dispatch(self, state, action):
        config = self.config
        plan = self.load("plan.json", {"tasks": []})
        task_id = slug(action.get("task") or "task")
        task = self.task(plan, task_id)
        if task is None:
            task = {"id": task_id, "title": task_id, "prompt": action.get("prompt", ""),
                    "check": None, "status": "todo", "attempts": 0}
            plan["tasks"].append(task)
        provider = action.get("provider")
        if provider not in config["workers"] or self.held_until(state, provider):
            options = self.available(config["workers"], state)
            if not options:
                return self.report(state, task, provider or "none", "provider_unavailable",
                                   "No worker provider is currently available.", {})
            provider = options[0]
        if task["status"] == "done":
            return self.report(state, task, provider, "already_done",
                               "Task is already committed; plan a new task instead.", {})
        task.update(status="running", attempts=task.get("attempts", 0) + 1, provider=provider)
        self.save("plan.json", plan)
        check = check_argv(task.get("check"))
        prompt = ("TASK " + task["id"] + ": " + (task.get("title") or "") + "\n\n"
                  + (task.get("prompt") or "") + "\n\nTHIS ATTEMPT:\n" + str(action.get("prompt") or "")
                  + ("\n\nThe task is accepted when this command passes in the worktree: "
                     + (task["check"] if isinstance(task["check"], str) else " ".join(check))
                     if check else ""))
        entry, model_note = self.resolve_model(provider, action.get("model"))
        if model_note:
            state["notes_for_supervisor"].append(model_note)
        model = entry["model"]
        # Antigravity encodes effort in the model id; elsewhere the catalog's effort
        # wins over the supervisor's so a light tier stays light.
        effort = None if provider == "antigravity" else (entry.get("effort") or action.get("effort"))
        argv = command(provider, prompt, model, effort,
                       worker=True, workspace=self.worktree, scope="worktree")
        head = git(self.worktree, "rev-parse", "HEAD")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log = self.dir / "logs" / ("%s-%s-%s.jsonl" % (task["id"], stamp, provider))
        self.journal("dispatch", task=task["id"], provider=provider, model=model or "default",
                     tier=entry["tier"], attempt=task["attempts"])
        code, out, err = self.invoke(argv, self.worktree, config["worker_max_seconds"],
                                     idle_timeout=config["worker_idle_seconds"],
                                     activity=self.activity, tee=(log, log.with_suffix(".stderr")))
        result = parse(provider, code, out, err, now=self.clock())
        state["tokens"][provider] = state["tokens"].get(provider, 0) + token_total(provider, result.usage)
        self.account(state, "worker", provider, result)
        if git(self.worktree, "rev-parse", "HEAD") != head:
            # Keep the worker's changes, drop its commit: the keeper commits.
            git(self.worktree, "reset", "--mixed", head)
        plan = self.load("plan.json", {"tasks": []})
        task = self.task(plan, task["id"])
        changed = changed_files(self.worktree)
        worker_text = tail(result.response or result.error or err, 40, 2500)
        verification = {}
        if result.status in ("rate_limited", "provider_wait", "auth_required"):
            until = result.retry_at or self.clock() + config["unknown_retry_seconds"]
            self.hold(state, provider, until, result.error or result.status)
            outcome, check_output = "provider_unavailable", ""
        elif check:
            # Not enforced: workers often legitimately write the tests a check runs.
            # The supervisor is told so it can review.
            touched = [name for name in changed if any(name in arg for arg in check)]
            check_code, check_out, check_err = run_process(
                check, self.worktree, config["check_timeout_seconds"])
            outcome = "passed" if check_code == 0 else "failed"
            check_output = ("Note: the worker changed files named by the check: "
                            + ", ".join(touched) + "\n" if touched else "") + tail(check_out + "\n" + check_err)
            if outcome == "passed" and changed:
                outcome, evidence, verification = self.verify_passing_change(state, task, provider)
                check_output = (check_output + "\n" + evidence).strip()
        else:
            outcome = "needs_review" if changed else ("no_changes" if result.status == "ok"
                                                      else "worker_error")
            check_output = ""
        if outcome in ("failed", "worker_error", "needs_review", "no_changes") and changed and not verification:
            diff, _ = self.diff_text(self.config["result_diff_characters"])
            verification = {"diff_sample": diff}
        if outcome == "passed":
            task.update(status="done", sha=self.commit(task["id"], changed))
        elif outcome == "needs_review":
            task["status"] = "review"
        elif outcome == "provider_unavailable":
            task["status"] = "todo"
        else:
            task["status"] = "failed"
        task["last_outcome"] = outcome
        if outcome in ("failed", "gate_failed", "audit_failed") and task["attempts"] >= 3:
            # Three attempts on one task usually means the task is too large or the
            # approach is wrong, not that the next model will do better.
            state["notes_for_supervisor"].append(
                "Task %s has now failed %d attempts across %s. Change the approach rather than "
                "re-dispatching the same task: split it into smaller tasks (fewer files or items "
                "each), tighten its check so a partial fix cannot pass, or fix the blocking "
                "problem in its own task first. Its history: %s"
                % (task["id"], task["attempts"],
                   ", ".join(sorted({entry.split(":")[1].split("/")[0] for entry in task.get("history") or []})) or "one provider",
                   " | ".join((task.get("history") or [])[-4:])))
        # Per-attempt evidence of which tier passes what, visible to the supervisor.
        task["history"] = (task.get("history") or []) + [
            "%s:%s/%s:%s" % (entry["tier"], provider, model or "default", outcome)]
        self.save("plan.json", plan)
        return self.report(state, task, provider, outcome,
                           (check_output + "\n\nWORKER SAID:\n" + worker_text).strip(),
                           {"worker_status": result.status, "changed_files": changed[:40],
                            "sha": task.get("sha") if outcome == "passed" else None,
                            **verification})

    def report(self, state, task, provider, outcome, log_tail, extra):
        summary, source = self.scribe.summarise(task["id"], outcome, log_tail)
        record = {"task": task["id"], "provider": provider, "outcome": outcome,
                  "summary": summary, **extra, "output_tail": tail(log_tail, 40, 3000)}
        state["pending"].append(record)
        handoff = self.load("handoff.json", {"notes": "", "results": []})
        handoff["results"] = (handoff["results"] + [{key: record[key] for key in
                                                     ("task", "provider", "outcome", "summary")}])[-30:]
        self.save("handoff.json", handoff)
        self.render_handoff()
        self.journal("result", task=task["id"], provider=provider, outcome=outcome, scribe=source)
        return record

    def accept(self, state, action):
        plan = self.load("plan.json", {"tasks": []})
        task = self.task(plan, slug(action.get("task") or ""))
        if task is None:
            state["notes_for_supervisor"].append("accept: unknown task %r" % action.get("task"))
            return
        if task["status"] == "done":
            return
        changed = changed_files(self.worktree)
        check = check_argv(task.get("check"))
        if check:
            code, out, err = run_process(check, self.worktree, self.config["check_timeout_seconds"])
            if code:
                task.update(status="failed", last_outcome="failed")
                self.save("plan.json", plan)
                self.report(state, task, "keeper", "failed", tail(out + "\n" + err), {})
                return
        if changed:
            outcome, evidence, extra = self.verify_passing_change(state, task, task.get("provider"))
            if outcome != "passed":
                task.update(status="failed", last_outcome=outcome)
                self.save("plan.json", plan)
                self.report(state, task, "keeper", outcome, evidence, extra)
                return
        task.update(status="done", sha=self.commit(task["id"], changed), last_outcome="accepted")
        self.save("plan.json", plan)
        self.journal("accepted", task=task["id"], sha=task["sha"])

    def discard(self, state, action):
        head = git(self.worktree, "rev-parse", "HEAD")
        snap = (head, {})
        reverted = self.revert_to(snap)
        plan = self.load("plan.json", {"tasks": []})
        task = self.task(plan, slug(action.get("task") or ""))
        if task and task["status"] != "done":
            task["status"] = "todo"
            self.save("plan.json", plan)
        self.journal("discarded", task=action.get("task"), files=len(reverted))

    def finish(self, state, action):
        plan = self.load("plan.json", {"tasks": []})
        open_tasks = [task["id"] for task in plan["tasks"]
                      if task["status"] not in ("done", "dropped")]
        problems = []
        if open_tasks:
            problems.append("Tasks not done or dropped: " + ", ".join(open_tasks))
        if changed_files(self.worktree):
            problems.append("Worktree has uncommitted changes; accept or discard them.")
        for task in plan["tasks"]:
            check = check_argv(task.get("check")) if task["status"] == "done" else None
            if check and not problems:
                code, out, err = run_process(check, self.worktree,
                                             self.config["check_timeout_seconds"])
                if code:
                    problems.append("Check for %s fails on the final tree:\n%s"
                                    % (task["id"], tail(out + "\n" + err, 30)))
        if not problems and self.config["gates"]:
            ok, gate_output = self.run_gates()
            if not ok:
                problems.append(gate_output)
        self.revert_to((git(self.worktree, "rev-parse", "HEAD"), {}))
        if not plan["tasks"]:
            problems.append("The plan has no tasks.")
        if problems:
            state["notes_for_supervisor"].append("`done` rejected:\n" + "\n".join(problems))
            self.journal("done_rejected", problems=" | ".join(problems)[:400])
            return False
        body = (self.path("goal.md").read_text().strip() + "\n\n"
                + str(action.get("summary") or "") + "\n\nTasks:\n"
                + "\n".join("- %s: %s `%s`" % (task["id"], task["status"], task.get("sha", ""))
                            for task in plan["tasks"])
                + "\n\nBranch: `" + self.branch + "`\n")
        self.path("pr.md").write_text(body)
        state["finished"] = True
        self.journal("finished", branch=self.branch, pr_body=str(self.path("pr.md")))
        return True

    def execute(self, state, actions):
        """Run actions in order. Returns (progress, acted): acted means work moved."""
        progress = acted = False
        for action in actions:
            op = action.get("op")
            if op == "plan":
                self.apply_plan(action)
            elif op == "note":
                handoff = self.load("handoff.json", {"notes": "", "results": []})
                handoff["notes"] = str(action.get("text") or "")[:4000]
                self.save("handoff.json", handoff)
                self.render_handoff()
            elif op == "dispatch":
                record = self.dispatch(state, action)
                acted = acted or record["outcome"] != "provider_unavailable"
                progress = progress or record["outcome"] in ("passed", "needs_review")
            elif op == "accept":
                self.accept(state, action)
                progress = acted = True
            elif op == "discard":
                self.discard(state, action)
                acted = True
            elif op == "wait":
                seconds = max(0, min(3600, int(action.get("seconds") or 0)))
                if self.available(self.config["workers"], state):
                    # Workers are synchronous: with a provider available there is
                    # nothing to wait for, and sleeping only stalls the run.
                    state["notes_for_supervisor"].append(
                        "`wait` ignored: worker providers are available and dispatch is "
                        "synchronous. Dispatch the next task.")
                    self.journal("supervisor_wait_ignored", seconds=seconds)
                    continue
                self.journal("supervisor_wait", seconds=seconds)
                self.sleep(seconds)
            elif op == "done":
                if self.finish(state, action):
                    return True, True
            else:
                state["notes_for_supervisor"].append("Unknown action op %r was ignored." % op)
            self.save("supervisor.json", state)
        return progress, acted

    # ---- keeper loop ---------------------------------------------------
    def observe(self, state, result, edited, actions):
        provider = state["provider"]
        unavailable = False
        earliest = 0
        if result is None:
            unavailable = bool(self.held_until(state, provider))
        elif result.status in ("rate_limited", "provider_wait", "auth_required"):
            until = result.retry_at or self.clock() + self.config["unknown_retry_seconds"]
            self.hold(state, provider, until, result.error or result.status)
            unavailable = True
        elif result.status != "ok":
            state["consecutive_errors"] += 1
            unavailable = state["consecutive_errors"] >= 3
        else:
            state["consecutive_errors"] = 0
        options = self.available_supervisors(exclude=provider, state=state)
        if unavailable and not options:
            holds = [until for until in state["holds"].values() if until > self.clock()]
            earliest = (min(holds) - self.clock()) if holds else self.config["unknown_retry_seconds"]
        large = max([keeper.code_lines(action.get("prompt")) for action in actions or []
                     if action.get("op") == "dispatch"] or [0]) > keeper.LARGE_CODE_LINES
        if edited or large:
            state["violations"] += 1
        return {"supervisor_unavailable": unavailable, "available": options,
                "earliest_reset_seconds": earliest, "edited_files": edited, "large_code": large,
                "violations": state["violations"], "context_tokens": state["context_tokens"],
                "reset_at": self.config["reset_at_tokens"],
                "stagnant_turns": state["stagnant_turns"],
                "stagnant_limit": self.config["stagnant_limit"]}

    def apply_decision(self, state, decision, obs):
        action = decision["action"]
        self.journal("keeper", action=action, rule=decision["rule"],
                     provider=decision.get("provider"), context_tokens=obs["context_tokens"])
        if decision.get("reminder"):
            state["notes_for_supervisor"].append(decision["reminder"])
        if action in ("reset_context", "switch_supervisor"):
            state.update(session_id=None, turns_in_session=0, violations=0, context_tokens=0,
                         # A stuck supervisor gets the stronger planning model for
                         # its fresh session; any other reset returns to normal.
                         escalated=decision["rule"].startswith("4:"))
            if action == "switch_supervisor":
                state["provider"] = decision["provider"]
                state["stagnant_turns"] = 0
                state["consecutive_errors"] = 0
            else:
                state["stagnant_turns"] = 0 if "stagnant" in decision["rule"] else state["stagnant_turns"]
            state["notes_for_supervisor"].append(
                "You are starting a fresh supervisor session; the plan, your notes and the "
                "results below are the complete record of the run so far.")
        if action == "wait":
            self.save("supervisor.json", state)
            self.out("[lite %s] all supervisors unavailable; sleeping %ds"
                     % (self.id, decision["wait_seconds"]))
            self.sleep(decision["wait_seconds"])

    def recover(self, state):
        plan = self.load("plan.json", {"tasks": []})
        interrupted = [task for task in plan["tasks"] if task["status"] == "running"]
        for task in interrupted:
            task.update(status="failed", last_outcome="interrupted")
            state["notes_for_supervisor"].append(
                "Task %s was interrupted while a worker ran; its partial changes are still "
                "uncommitted in the worktree." % task["id"])
        if interrupted:
            self.save("plan.json", plan)

    def run(self, max_turns=None):
        self.dir.mkdir(parents=True, exist_ok=True)
        lock = self.path("keeper.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another keeper is already running for " + self.id)
        try:
            state = self.load("supervisor.json")
            if state is None:
                raise ValueError("No lite run named " + self.id)
            self.recover(state)
            self.run_setup(state)
            turns = 0
            while not state["finished"]:
                if max_turns is not None and turns >= max_turns:
                    break
                self.read_inbox(state)
                if self.held_until(state, state["provider"]):
                    obs = self.observe(state, None, [], [])
                    self.apply_decision(state, keeper.decide(obs), obs)
                    self.save("supervisor.json", state)
                    continue
                turns += 1
                result, edited = self.supervisor_turn(state)
                actions = extract_actions(result.response) if result.status == "ok" else None
                if result.status == "ok" and actions is None:
                    state["notes_for_supervisor"].append(
                        "Your last reply had no valid JSON actions object. Reply with JSON only.")
                if edited:
                    self.journal("supervisor_edit_reverted", files=", ".join(edited)[:300])
                progress = acted = False
                if actions:
                    state["pending"] = []
                    self.save("supervisor.json", state)
                    progress, acted = self.execute(state, actions)
                    if state["finished"]:
                        break
                fingerprint = hashlib.sha256(json.dumps(actions, sort_keys=True).encode()).hexdigest()
                if progress:
                    state["stagnant_turns"] = 0
                elif (not acted or fingerprint == state["last_actions"]
                      or result.status != "ok"):
                    # Planning and notes alone are allowed once; a supervisor that
                    # never dispatches is stuck however different its replies look.
                    state["stagnant_turns"] += 1
                state["last_actions"] = fingerprint
                obs = self.observe(state, result, edited, actions)
                self.apply_decision(state, keeper.decide(obs), obs)
                self.save("supervisor.json", state)
            self.save("supervisor.json", state)
            return state
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def status(self):
        state = self.load("supervisor.json")
        if state is None:
            raise ValueError("No lite run named " + self.id)
        plan = self.load("plan.json", {"tasks": []})
        lines = ["run %s  branch %s  %s" % (self.id, self.branch,
                                            "FINISHED" if state["finished"] else "in progress"),
                 "supervisor: %s  session: %s  context≈%s tokens  turns: %d  stagnant: %d"
                 % (state["provider"], "resumable" if state["session_id"] else "fresh",
                    state["context_tokens"], state["turns_in_session"], state["stagnant_turns"]),
                 "holds: " + (", ".join("%s until %s" % (name, time.strftime("%H:%M", time.localtime(until)))
                                         for name, until in state["holds"].items()
                                         if until > self.clock()) or "none"),
                 "tokens: " + json.dumps(state["tokens"]),
                 "gates: " + (json.dumps(self.config["gates"]) if self.config["gates"] else "none"),
                 "operator notes: %d" % len(state.get("operator_notes", [])),
                 "estimated cost: " + ", ".join(
                     "%s $%.2f (%d calls)" % (key, value["estimated_usd"], value["calls"])
                     for key, value in sorted(state.get("cost", {}).items())) or "none", ""]
        for task in plan["tasks"]:
            lines.append("  %-10s %-32s attempts=%d %s" % (task["status"], task["id"],
                                                          task.get("attempts", 0),
                                                          task.get("last_outcome") or ""))
        return "\n".join(lines)
