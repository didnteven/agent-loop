"""Single-worker milestone runner, durable across quota waits and restarts."""
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .adapters import command, codex_limits, parse, quota_deadline, run_process, token_total


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                   stderr=subprocess.STDOUT).strip()


def safe_path(root, name):
    if not isinstance(name, str):
        raise ValueError("Path must be a string")
    relative = Path(name)
    if (not isinstance(name, str) or not name or relative.is_absolute()
            or any(part in ("..", ".git", ".agent-loop") for part in relative.parts)):
        raise ValueError("Disallowed path: " + str(name))
    target = root / relative
    if target.resolve() == root.resolve() or root.resolve() not in target.resolve().parents:
        raise ValueError("Path escapes workspace")
    current = target
    while current != root:
        if current.is_symlink():
            raise ValueError("Symlinks are not supported")
        current = current.parent
    return target


def check_budget(budget):
    if budget is None:
        return
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise ValueError("Token budgets must be non-negative integers")


def validate_plan(plan):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", plan["id"]):
        raise ValueError("Plan id must be a short lowercase slug")
    ids = set()
    for task in plan["tasks"]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", task["id"]) or task["id"] in ids:
            raise ValueError("Invalid or duplicate task id")
        ids.add(task["id"])
        if task["provider"] not in ("codex", "claude", "antigravity"):
            raise ValueError("Unsupported provider")
        if not task["files"] or len(set(task["files"])) != len(task["files"]):
            raise ValueError("Each task needs unique allowed files")
        for name in task["files"]:
            safe_path(Path.cwd(), name)
        if not isinstance(task.get("check"), list) or not task["check"]:
            raise ValueError("Every task needs a trusted check argv")
        check_budget(task.get("token_budget"))
    if not ids:
        raise ValueError("Plan needs tasks")
    budgets = plan.get("provider_token_budgets", {})
    if not isinstance(budgets, dict):
        raise ValueError("provider_token_budgets must be an object")
    for provider, budget in budgets.items():
        if provider not in ("codex", "claude", "antigravity"):
            raise ValueError("Unsupported provider in provider_token_budgets")
        check_budget(budget)


class Engine:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        self.home = self.repo / ".agent-loop"
        self.home.mkdir(exist_ok=True)
        self.holding_repo_lock = False
        self.locks = self.home / "locks"
        self.locks.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.home / "state.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, plan TEXT NOT NULL, digest TEXT NOT NULL,
                workspace TEXT NOT NULL, branch TEXT NOT NULL, base TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT, id TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                retry_at REAL DEFAULT 0, sha TEXT, error TEXT DEFAULT '',
                tokens INTEGER DEFAULT 0,
                PRIMARY KEY(run_id,id));
            CREATE TABLE IF NOT EXISTS providers (
                name TEXT PRIMARY KEY, retry_at REAL DEFAULT 0, reason TEXT DEFAULT '',
                tokens INTEGER DEFAULT 0, estimated_usd REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                at REAL, run_id TEXT, task_id TEXT, kind TEXT, detail TEXT);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "tokens" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN tokens INTEGER DEFAULT 0")
        for name in ("codex", "claude", "antigravity"):
            self.db.execute("INSERT OR IGNORE INTO providers(name) VALUES (?)", (name,))
        self.db.commit()

    @contextmanager
    def lock(self, name="repo", wait=0.0, message="Another supervisor is active for this repository"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,70}", name):
            raise ValueError("Invalid lock name")
        with (self.locks / (name + ".lock")).open("w") as handle:
            # Non-blocking with a bounded wait: a held lock must never hang a supervisor.
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(message)
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def run_lock(self, run_id):
        """Exclude only supervisors of the same milestone; distinct plans run concurrently."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", str(run_id)):
            raise ValueError("Invalid milestone id")
        return self.lock("run-" + run_id,
                         message="Another supervisor is active for milestone " + run_id)

    @contextmanager
    def repo_lock(self):
        """Serialize the few operations that mutate shared repository state.

        Held only around git plumbing, never across a sleep or a model call, so the
        ordering run lock then repo lock cannot deadlock.
        """
        if self.holding_repo_lock:
            yield
            return
        self.holding_repo_lock = True
        try:
            with self.lock("repo", wait=30.0,
                           message="Timed out waiting for shared repository access"):
                yield
        finally:
            self.holding_repo_lock = False

    def event(self, run_id, task, kind, detail):
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                        (time.time(), run_id, task, kind, str(detail)))
        self.db.commit()
        print(json.dumps({"run": run_id, "task": task, "state": kind, "detail": detail}), flush=True)

    def initialize(self, plan):
        validate_plan(plan)
        encoded = json.dumps(plan, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        existing = self.db.execute("SELECT * FROM runs WHERE id=?", (plan["id"],)).fetchone()
        if existing:
            if existing["digest"] != digest:
                raise ValueError("Plan changed: use a new id, or restore the original plan")
            return existing
        with self.repo_lock():
            existing = self.db.execute("SELECT * FROM runs WHERE id=?", (plan["id"],)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise ValueError("Plan changed: use a new id, or restore the original plan")
                return existing
            if git(self.repo, "status", "--porcelain"):
                raise RuntimeError("Commit the target repository before starting a new milestone")
            base = git(self.repo, "rev-parse", "HEAD")
            branch = "loop/" + plan["id"]
            workspace = self.home / "worktrees" / plan["id"]
            workspace.parent.mkdir(exist_ok=True)
            git(self.repo, "worktree", "add", "-b", branch, str(workspace), base)
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                            (plan["id"], encoded, digest, str(workspace), branch, base))
            for task in plan["tasks"]:
                self.db.execute("INSERT INTO tasks(run_id,id) VALUES (?,?)", (plan["id"], task["id"]))
            self.db.commit()
            return self.db.execute("SELECT * FROM runs WHERE id=?", (plan["id"],)).fetchone()

    def set_task(self, run_id, task_id, **fields):
        self.db.execute("UPDATE tasks SET " + ",".join(k+"=?" for k in fields)
                        + " WHERE run_id=? AND id=?", (*fields.values(), run_id, task_id))
        self.db.commit()

    def run_tokens(self, plan, provider):
        ids = [task["id"] for task in plan["tasks"] if task["provider"] == provider]
        if not ids:
            return 0
        row = self.db.execute(
            "SELECT COALESCE(SUM(tokens),0) AS total FROM tasks WHERE run_id=? AND id IN ("
            + ",".join("?" * len(ids)) + ")", (plan["id"], *ids)).fetchone()
        return row["total"]

    def hold(self, provider, until, reason):
        self.db.execute("UPDATE providers SET retry_at=?,reason=? WHERE name=?",
                        (until, reason, provider))
        self.db.commit()

    def tick(self, plan, worker=None, now=None):
        now = time.time() if now is None else now
        run = self.initialize(plan)
        workspace = Path(run["workspace"])
        if git(workspace, "branch", "--show-current") != run["branch"]:
            raise RuntimeError("Managed worktree branch changed")
        # Sequential section dependencies: a later task may depend on earlier files.
        for task in plan["tasks"]:
            row = self.db.execute("SELECT * FROM tasks WHERE run_id=? AND id=?",
                                  (plan["id"], task["id"])).fetchone()
            if row["status"] == "done":
                git(workspace, "merge-base", "--is-ancestor", row["sha"], "HEAD")
                continue
            if row["status"] == "blocked":
                return "blocked", 0
            changed = set(git(workspace, "diff", "HEAD", "--name-only").splitlines())
            changed.update(git(workspace, "ls-files", "--others", "--exclude-standard").splitlines())
            if changed - set(task["files"]):
                raise RuntimeError("Unexpected changes in managed worktree; inspect before resuming")
            marker = "Agent-Loop-Task: " + plan["id"] + "/" + task["id"]
            # Reconcile a crash after git commit but before the database transaction.
            sha = git(workspace, "log", run["base"]+"..HEAD", "--format=%H", "--fixed-strings",
                      "--grep="+marker, "-1")
            if sha and not row["error"].startswith("Milestone regression:"):
                self.set_task(plan["id"], task["id"], status="done", sha=sha)
                continue
            provider = self.db.execute("SELECT * FROM providers WHERE name=?", (task["provider"],)).fetchone()
            retry_at = max(row["retry_at"], provider["retry_at"])
            if retry_at > now:
                return "waiting", retry_at
            budget = plan.get("provider_token_budgets", {}).get(task["provider"])
            if budget is not None and self.run_tokens(plan, task["provider"]) >= budget:
                self.set_task(plan["id"], task["id"], status="blocked",
                              error="Local admission token budget reached for provider " + task["provider"])
                return "blocked", 0
            task_budget = task.get("token_budget")
            if task_budget is not None and row["tokens"] >= task_budget:
                self.set_task(plan["id"], task["id"], status="blocked",
                              error="Local admission token budget reached for this section")
                return "blocked", 0
            if row["attempts"] >= plan.get("max_attempts", 3):
                self.set_task(plan["id"], task["id"], status="blocked", error="Repair attempts exhausted")
                return "blocked", 0
            # Codex quota reads are optional: unknown telemetry is not zero allowance.
            if worker is None and task["provider"] == "codex" and plan.get("read_codex_quotas", True):
                try:
                    limits = codex_limits(workspace)
                    # Avoid unrelated model buckets; pick the configured bucket or standard codex bucket.
                    bucket = task.get("quota_bucket", "codex")
                    selected = limits.get("rateLimitsByLimitId", {}).get(bucket, limits.get("rateLimits", {}))
                    deadline = quota_deadline(selected, now)
                    snapshot = self.home / "codex-quota.json"
                    scratch = snapshot.with_suffix(".json." + str(os.getpid()) + ".tmp")
                    scratch.write_text(json.dumps(limits, indent=2))
                    os.replace(scratch, snapshot)
                    if deadline:
                        self.hold("codex", deadline, "Provider quota exhausted")
                        return "waiting", deadline
                except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
                    self.event(plan["id"], task["id"], "quota_unknown", str(exc))
            prompt = ("Implement this small coding section. Do not use tools, run commands, or edit files. "
                      "Return ONLY a JSON object with a files array of {path, content}, containing complete file contents. "
                      "Use exactly these paths: " + json.dumps(task["files"]) + ".\n" + task["prompt"])
            for name in task.get("context_files", []):
                path = safe_path(workspace, name)
                prompt += "\nCONTEXT " + name + "\n" + path.read_text()[:30000]
            if row["error"]:
                prompt += "\nPrevious attempt failed this trusted check; fix the issue:\n" + row["error"][-4000:]
            self.set_task(plan["id"], task["id"], status="running", attempts=row["attempts"]+1, retry_at=0)
            self.event(plan["id"], task["id"], "running", task["provider"])
            try:
                if worker:
                    result = worker(task, prompt, workspace)
                else:
                    code, out, err = run_process(command(task["provider"], prompt, task.get("model")),
                                                 workspace, plan.get("worker_timeout_seconds", 180))
                    logdir = self.home / "logs" / plan["id"]
                    logdir.mkdir(parents=True, exist_ok=True)
                    prefix = task["id"] + "-" + str(row["attempts"]+1) + "-" + str(time.time_ns())
                    (logdir / (prefix+".jsonl")).write_text(out)
                    (logdir / (prefix+".stderr")).write_text(err)
                    result = parse(task["provider"], code, out, err, now)
            except OSError as exc:
                self.set_task(plan["id"], task["id"], status="blocked", error=str(exc))
                return "blocked", 0
            spent = token_total(task["provider"], result.usage)
            self.db.execute("UPDATE providers SET tokens=tokens+?,estimated_usd=estimated_usd+? WHERE name=?",
                            (spent, result.estimated_usd, task["provider"]))
            self.db.execute("UPDATE tasks SET tokens=tokens+? WHERE run_id=? AND id=?",
                            (spent, plan["id"], task["id"]))
            self.db.commit()
            if result.status == "rate_limited":
                # Unknown reset: probe slowly; this is a retry time, not a claimed reset.
                delay = plan.get("unknown_quota_retry_seconds", 1800)
                deadline = result.retry_at or now + delay
                self.hold(task["provider"], deadline, result.error or "Rate limited")
                self.set_task(plan["id"], task["id"], status="waiting", attempts=row["attempts"], error=result.error)
                self.event(plan["id"], task["id"], "waiting", deadline)
                return "waiting", deadline
            if result.status != "ok":
                state = "waiting" if result.status == "transient" else "blocked"
                deadline = now + min(900, 30 * 2**row["attempts"]) if state == "waiting" else 0
                self.set_task(plan["id"], task["id"], status=state, retry_at=deadline, error=result.error)
                self.event(plan["id"], task["id"], state, result.error)
                return state, deadline
            if result.retry_at:
                self.hold(task["provider"], result.retry_at, "Quota exhausted after completed section")
            try:
                self.apply_result(task, result.response, workspace)
                code, out, err = run_process(task["check"], workspace, plan.get("check_timeout_seconds", 30))
                if code:
                    raise ValueError((out+err)[-4000:] or "Check exited " + str(code))
                git(workspace, "add", "--", *task["files"])
                if git(workspace, "diff", "--cached", "--name-only") == "":
                    raise ValueError("No code change to commit")
                staged = git(workspace, "diff", "--cached", "--name-only").splitlines()
                if set(staged) - set(task["files"]):
                    raise ValueError("Unexpected files staged; inspect the managed worktree")
                git(workspace, "commit", "-m", "feat: " + task["id"], "-m", marker)
                sha = git(workspace, "rev-parse", "HEAD")
                self.set_task(plan["id"], task["id"], status="done", sha=sha, error="")
                self.event(plan["id"], task["id"], "done", sha)
                return "progress", 0
            except (ValueError, OSError, subprocess.CalledProcessError) as exc:
                self.set_task(plan["id"], task["id"], status="pending", error=str(exc))
                self.event(plan["id"], task["id"], "check_failed", str(exc))
                return "progress", 0
        if git(workspace, "status", "--porcelain"):
            raise RuntimeError("Milestone worktree must be clean before preparing its PR")
        for task in plan["tasks"]:
            code, out, err = run_process(task["check"], workspace, plan.get("check_timeout_seconds", 30))
            if code:
                self.set_task(plan["id"], task["id"], status="blocked", error="Milestone regression: " + (out+err)[-4000:])
                return "blocked", 0
        self.prepare_pr(plan, run)
        return "ready_for_pr", 0

    def apply_result(self, task, response, workspace):
        text = response.strip()
        if text.startswith("```") and text.endswith("```"):
            text = "\n".join(text.splitlines()[1:-1])
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Response must be a JSON object")
        files = data.get("files")
        if not isinstance(files, list) or len(files) != len(task["files"]):
            raise ValueError("Response must contain exactly the allowed files")
        if any(not isinstance(f, dict) or not isinstance(f.get("path"), str)
               or not isinstance(f.get("content"), str) for f in files):
            raise ValueError("Every file needs a string path and content")
        if {f["path"] for f in files} != set(task["files"]):
            raise ValueError("Response paths do not match the task")
        targets = []
        for file in files:
            if not isinstance(file["content"], str) or len(file["content"]) > 200000:
                raise ValueError("Invalid file content")
            targets.append((safe_path(workspace, file["path"]), file["content"]))
        for target, content in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def prepare_pr(self, plan, run):
        rows = self.db.execute("SELECT id,sha FROM tasks WHERE run_id=?", (plan["id"],)).fetchall()
        body = (plan.get("description", plan["id"]) + "\n\nValidation: every section passed its configured check.\n\n"
                + "\n".join("- " + r["id"] + ": `" + r["sha"] + "`" for r in rows)
                + "\n\nBranch: `" + run["branch"] + "`\n")
        (self.home / (plan["id"]+"-pr.md")).write_text(body)

    def status(self):
        return {table: [dict(r) for r in self.db.execute("SELECT * FROM " + table)]
                for table in ("tasks", "providers")}
