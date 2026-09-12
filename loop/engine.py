"""Single-worker milestone runner, durable across quota waits and restarts."""
import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .registry import (CapReached, CapWait, DEFAULT_RESERVATION_TOKENS, Outbox,
                       Registry, account_identity)
from .storage import atomic_json
from .context import fit, model_budget
from .diagnosis import MISSING_DEPENDENCY, classify, missing_dependency
from .policy import (canonical_policy, matching_recipe, path_allowed, policy_digest,
                     policy_narrows, recipe_id, validate_against_policy)

from . import health
# Provider argv assembly and transcript parsing now belong to the worker
# boundary; the supervisor only consumes validated results.
from .adapters import (Result, asked_question, auth_probe, provider_limits, quota_deadline,
                       run_process, token_total, usage_fraction)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                   stderr=subprocess.STDOUT).strip()


def git_common_dir(repo):
    """Return the shared Git directory for ordinary and linked worktrees."""
    path = Path(git(repo, "rev-parse", "--git-common-dir"))
    return (Path(repo) / path).resolve() if not path.is_absolute() else path.resolve()


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


def changed_files(workspace):
    changed = set(git(workspace, "diff", "HEAD", "--name-only").splitlines())
    changed.update(git(workspace, "ls-files", "--others", "--exclude-standard").splitlines())
    return {name for name in changed if name}


def restore_task_files(workspace, names):
    """Discard an unsuccessful worker attempt inside its supervisor-owned worktree."""
    tracked = set(git(workspace, "ls-files", "--", *names).splitlines())
    if tracked:
        git(workspace, "restore", "--source=HEAD", "--staged", "--worktree", "--", *tracked)
    for name in set(names) - tracked:
        target = safe_path(workspace, name)
        if target.exists():
            if not target.is_file():
                raise ValueError("Worker created a non-file at " + name)
            target.unlink()


def restore_worker_attempt(workspace, head=None):
    if head and git(workspace, "rev-parse", "HEAD") != head:
        git(workspace, "reset", "--mixed", head)
    names = changed_files(workspace)
    if names:
        restore_task_files(workspace, names)


def file_snapshot(workspace, names):
    """Record pre-attempt file sizes.

    Sizes rather than contents: this snapshot is persisted with the attempt so
    truncation can still be judged after a supervisor restart, and a durable
    record must not carry a copy of the worktree.
    """
    snapshot = {}
    for name in names:
        path = safe_path(workspace, name)
        snapshot[name] = path.stat().st_size if path.is_file() else None
    return snapshot


def validate_worker_changes(task, workspace, before, head):
    if git(workspace, "rev-parse", "HEAD") != head:
        raise ValueError("Worker changed the managed Git history")
    changed = changed_files(workspace)
    unexpected = changed - set(task["files"])
    if unexpected:
        raise ValueError("Worker changed files outside its allowlist: " + ", ".join(sorted(unexpected)))
    if not changed:
        raise ValueError("Worker completed without changing an allowed file")
    max_shrink = task.get("max_file_shrink_fraction", 0.5)
    if task.get("allow_large_deletions", False):
        max_shrink = 1.0
    for name in changed:
        old = before.get(name)
        path = safe_path(workspace, name)
        if old is None or not path.is_file() or old < 1000:
            continue
        shrink = 1 - (path.stat().st_size / old)
        if shrink > max_shrink:
            raise ValueError(
                f"Suspicious truncation of {name}: {old} bytes became "
                f"{path.stat().st_size} bytes ({shrink:.0%} smaller)")
    return changed


def validate_managed_history(plan, run, workspace, done_shas):
    """Allow only recorded task commits or crash-recovery commits with exact markers."""
    task_ids = {task["id"] for task in plan["tasks"]}
    commits = git(workspace, "rev-list", run["base"] + "..HEAD").splitlines()
    for sha in commits:
        if sha in done_shas:
            continue
        message = git(workspace, "show", "-s", "--format=%B", sha)
        if not any("Agent-Loop-Task: " + plan["id"] + "/" + task_id in message
                   for task_id in task_ids):
            raise RuntimeError("Managed worktree contains an unrecognized commit: " + sha)


def dependencies(plan):
    return {task["id"]: task.get("depends_on", [plan["tasks"][i-1]["id"]] if i else [])
            for i, task in enumerate(plan["tasks"])}


def fingerprint(text, workspace=None):
    value = str(text or "").lower()
    value = re.sub(r"\d{4}-\d\d-\d\d[t ][0-9:.+-]+", "<time>", value)
    value = re.sub(r"\b\d{9,}\b", "<number>", value)
    value = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", value)
    roots = ["/tmp", "/private/tmp"]
    if workspace:
        roots.append(str(Path(workspace).resolve()))
    for root in sorted(roots, key=len, reverse=True):
        value = re.sub(re.escape(root.lower()) + r"[^\s:'\"]*", "<workspace>", value)
    return hashlib.sha256(value[:600].encode()).hexdigest()[:16]


def validate_plan(plan, root=None):
    root = Path.cwd() if root is None else Path(root).resolve()
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
            safe_path(root, name)
        if not isinstance(task.get("check"), list) or not task["check"]:
            raise ValueError("Every task needs a trusted check argv")
        if any(name in arg for name in task["files"]
               for arg in task["check"] if isinstance(arg, str)):
            raise ValueError("A worker cannot edit the trusted check it runs")
        effort = task.get("effort", "low")
        if effort not in ("low", "medium", "high"):
            raise ValueError("Task effort must be low, medium, or high")
        model_effort = str(task.get("model", "")).rsplit("-", 1)[-1]
        if (task["provider"] == "antigravity" and task.get("model")
                and model_effort in ("low", "medium", "high")
                and "effort" in task and effort != model_effort):
            raise ValueError("Antigravity model tier conflicts with task effort")
        shrink = task.get("max_file_shrink_fraction", 0.5)
        if isinstance(shrink, bool) or not isinstance(shrink, (int, float)) or not 0 <= shrink <= 1:
            raise ValueError("max_file_shrink_fraction must be between 0 and 1")
        check_budget(task.get("token_budget"))
    if not ids:
        raise ValueError("Plan needs tasks")
    policy = canonical_policy(plan, root)
    validate_against_policy(plan, policy)
    edges = dependencies(plan)
    for task_id, parents in edges.items():
        if not isinstance(parents, list) or any(not isinstance(p, str) or p not in ids for p in parents):
            raise ValueError("Unknown or invalid dependency")
        if len(set(parents)) != len(parents):
            raise ValueError("Duplicate dependency")
    resolved = set()
    while len(resolved) < len(ids):
        ready = {t for t, parents in edges.items() if t not in resolved and set(parents) <= resolved}
        if not ready:
            raise ValueError("Dependency cycle")
        resolved.update(ready)
    budgets = plan.get("provider_token_budgets", {})
    if not isinstance(budgets, dict):
        raise ValueError("provider_token_budgets must be an object")
    for provider, budget in budgets.items():
        if provider not in ("codex", "claude", "antigravity"):
            raise ValueError("Unsupported provider in provider_token_budgets")
        check_budget(budget)
    if not isinstance(plan.get("failure_review", False), bool):
        raise ValueError("failure_review must be a boolean")
    failure_provider = plan.get("failure_review_provider")
    if failure_provider is not None and failure_provider not in ("codex", "claude", "antigravity"):
        raise ValueError("Unsupported failure_review_provider")
    setup = plan.get("setup")
    if setup is not None and (not isinstance(setup, list) or not setup
                              or not all(isinstance(arg, str) and arg for arg in setup)):
        raise ValueError("setup must be a non-empty argv list")


class Engine:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        self.home = self.repo / ".agent-loop"
        self.ensure_excluded()
        self.home.mkdir(exist_ok=True)
        self.holding_repo_lock = False
        self.caps = {}
        self.registry_failed = False
        self.outbox = Outbox(self.home / "outbox")
        self._registry = None
        self.locks = self.home / "locks"
        self.locks.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.home / "state.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, plan TEXT NOT NULL, digest TEXT NOT NULL,
                workspace TEXT NOT NULL, branch TEXT NOT NULL, base TEXT NOT NULL,
                objective_id TEXT, policy TEXT, policy_version TEXT,
                parent_id TEXT, reason TEXT DEFAULT '', superseded_by TEXT);
            CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT, id TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                retry_at REAL DEFAULT 0, sha TEXT, error TEXT DEFAULT '',
                tokens INTEGER DEFAULT 0,
                question_count INTEGER DEFAULT 0, selected_provider TEXT,
                selected_model TEXT, selected_effort TEXT,
                PRIMARY KEY(run_id,id));
            CREATE TABLE IF NOT EXISTS providers (
                name TEXT PRIMARY KEY, retry_at REAL DEFAULT 0, reason TEXT DEFAULT '',
                tokens INTEGER DEFAULT 0, estimated_usd REAL DEFAULT 0,
                probe_at REAL DEFAULT 0, held_since REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                at REAL, run_id TEXT, task_id TEXT, kind TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, at REAL, objective_id TEXT, run_id TEXT, task_id TEXT,
                question TEXT, alternatives TEXT, chosen TEXT, reason TEXT, evidence TEXT,
                policy_version TEXT, validation TEXT);
            CREATE TABLE IF NOT EXISTS invocations (
                id TEXT PRIMARY KEY, at REAL, objective_id TEXT, run_id TEXT, task_id TEXT,
                attempt INTEGER, kind TEXT, provider TEXT, model TEXT, effort TEXT,
                input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                estimated_usd REAL, duration REAL, status TEXT, fingerprint TEXT,
                reservation_id TEXT);
            CREATE TABLE IF NOT EXISTS setup_runs (
                run_id TEXT, recipe TEXT, environment TEXT, at REAL, status TEXT,
                output TEXT, PRIMARY KEY(run_id,recipe,environment));
            CREATE TABLE IF NOT EXISTS fingerprints (
                objective_id TEXT, task_key TEXT, fp TEXT, strategy TEXT, count INTEGER,
                last_at REAL, last_example TEXT, replan_attempted INTEGER DEFAULT 0,
                PRIMARY KEY(objective_id,task_key,fp,strategy));
        """)
        invocation_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(invocations)")}
        if "reservation_id" not in invocation_columns:
            self.db.execute("ALTER TABLE invocations ADD COLUMN reservation_id TEXT")
        provider_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(providers)")}
        for name in ("probe_at", "held_since", "headroom_at"):
            if name not in provider_columns:
                self.db.execute("ALTER TABLE providers ADD COLUMN " + name + " REAL DEFAULT 0")
        if "headroom" not in provider_columns:
            # NULL, not 1.0: unknown headroom must never read as "plenty left".
            self.db.execute("ALTER TABLE providers ADD COLUMN headroom REAL")
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "tokens" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN tokens INTEGER DEFAULT 0")
        for name, declaration in (("park_reason", "TEXT DEFAULT ''"),
                                  ("wake_kind", "TEXT DEFAULT ''"), ("wake_at", "REAL DEFAULT 0"),
                                  ("question_count", "INTEGER DEFAULT 0"),
                                  ("selected_provider", "TEXT"), ("selected_model", "TEXT"),
                                  ("selected_effort", "TEXT")):
            if name not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN " + name + " " + declaration)
        run_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(runs)")}
        for name, declaration in (("objective_id", "TEXT"), ("policy", "TEXT"),
                                  ("policy_version", "TEXT"), ("parent_id", "TEXT"),
                                  ("reason", "TEXT DEFAULT ''"), ("superseded_by", "TEXT")):
            if name not in run_columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN " + name + " " + declaration)
        for name in ("codex", "claude", "antigravity"):
            self.db.execute("INSERT OR IGNORE INTO providers(name) VALUES (?)", (name,))
        self.db.commit()

    def ensure_excluded(self):
        path = Path(git(self.repo, "rev-parse", "--git-path", "info/exclude"))
        if not path.is_absolute():
            path = self.repo / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            content = stream.read()
            if "/.agent-loop/" not in content.splitlines():
                stream.write(("\n" if content and not content.endswith("\n") else "") + "/.agent-loop/\n")
                stream.flush()
                os.fsync(stream.fileno())

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
        validate_plan(plan, self.repo)
        encoded = json.dumps(plan, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        policy = canonical_policy(plan, self.repo)
        encoded_policy = json.dumps(policy, sort_keys=True)
        version = policy_digest(policy)
        objective_id = plan.get("objective_id", plan["id"])
        # The frozen policy, not generated plan content, is the limit authority.
        self.caps = policy.get("limits", {})
        intent_path = self.home / "intents" / (plan["id"] + ".json")
        with self.repo_lock():
            existing = self.db.execute("SELECT * FROM runs WHERE id=?", (plan["id"],)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise ValueError("Plan changed: use a new id, or restore the original plan")
                if existing["policy_version"] and existing["policy_version"] != version:
                    raise ValueError("Execution policy changed for an immutable run")
                if not existing["policy_version"]:
                    self.db.execute("UPDATE runs SET objective_id=?,policy=?,policy_version=? WHERE id=?",
                                    (objective_id, encoded_policy, version, plan["id"]))
                    self.db.commit()
                    existing = self.db.execute("SELECT * FROM runs WHERE id=?", (plan["id"],)).fetchone()
                if intent_path.exists():
                    intent = json.loads(intent_path.read_text())
                    if intent.get("digest") == digest and intent.get("workspace") == existing["workspace"]:
                        intent_path.unlink()
                return existing
            if git(self.repo, "status", "--porcelain"):
                raise RuntimeError("Commit the target repository before starting a new milestone")
            common = str(git_common_dir(self.repo))
            intent = json.loads(intent_path.read_text()) if intent_path.exists() else None
            if intent and not (intent.get("digest") == digest and intent.get("run_id") == plan["id"]
                               and intent.get("common") == common):
                atomic_json(intent_path.with_name(intent_path.stem + ".conflict-" + uuid.uuid4().hex + ".json"), intent)
                intent = None
            if intent:
                workspace = Path(intent["workspace"])
                if workspace.parent != self.home / "worktrees" or workspace.is_symlink():
                    raise RuntimeError("Initialization intent has invalid workspace ownership")
                branch, base = intent["branch"], intent["base"]
                matching = False
                if workspace.exists():
                    try:
                        matching = (Path(git(workspace, "rev-parse", "--show-toplevel")).resolve()
                                    == workspace.resolve()
                                    and git(workspace, "branch", "--show-current") == branch
                                    and git(workspace, "rev-parse", "HEAD") == base
                                    and str(git_common_dir(workspace)) == common
                                    and not git(workspace, "status", "--porcelain"))
                    except subprocess.CalledProcessError:
                        pass
                    if not matching:
                        atomic_json(intent_path.with_name(intent_path.stem + ".conflict-" + uuid.uuid4().hex + ".json"), intent)
                        intent = None
                if intent and not workspace.exists():
                    # An interrupted branch creation is preserved; choose a new owned branch.
                    if git(self.repo, "branch", "--list", branch):
                        intent = None
            if not intent:
                base = git(self.repo, "rev-parse", "HEAD")
                name = plan["id"]
                workspace = self.home / "worktrees" / name
                branch = "loop/" + name
                if workspace.exists() or git(self.repo, "branch", "--list", branch):
                    name += "-" + uuid.uuid4().hex[:12]
                    workspace = self.home / "worktrees" / name
                    branch = "loop/" + name
                intent = dict(run_id=plan["id"], plan=plan, digest=digest, common=common,
                              workspace=str(workspace), branch=branch, base=base, nonce=uuid.uuid4().hex)
                atomic_json(intent_path, intent)
            workspace = Path(intent["workspace"])
            workspace.parent.mkdir(exist_ok=True)
            if not workspace.exists():
                git(self.repo, "worktree", "add", "-b", intent["branch"], str(workspace), intent["base"])
            with self.db:
                self.db.execute("""INSERT INTO runs(
                    id,plan,digest,workspace,branch,base,objective_id,policy,policy_version)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (plan["id"], encoded, digest, str(workspace), intent["branch"], intent["base"],
                     objective_id, encoded_policy, version))
                for task in plan["tasks"]:
                    self.db.execute("""INSERT INTO tasks(
                        run_id,id,selected_provider,selected_model,selected_effort) VALUES (?,?,?,?,?)""",
                        (plan["id"], task["id"], task["provider"], task.get("model"),
                         task.get("effort", "low")))
            intent_path.unlink()
            return self.db.execute("SELECT * FROM runs WHERE id=?", (plan["id"],)).fetchone()

    def park(self, run_id, task_id, reason, wake_kind="new_evidence"):
        self.set_task(run_id, task_id, status="parked", error=reason, park_reason=reason,
                      wake_kind=wake_kind, wake_at=0)

    def decide(self, run_id, task_id, question, alternatives, chosen, reason,
               evidence=None, validation=""):
        run = self.db.execute("SELECT objective_id,policy_version FROM runs WHERE id=?",
                              (run_id,)).fetchone()
        if not run:
            raise ValueError("Unknown run for decision")
        decision_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            decision_id, time.time(), run["objective_id"], run_id, task_id, question,
            json.dumps(alternatives), chosen, reason, json.dumps(evidence or []),
            run["policy_version"], validation))
        self.db.commit()
        return decision_id

    @property
    def registry(self):
        """Open the shared account registry lazily; never fail construction."""
        if self._registry is None:
            self._registry = Registry()
        return self._registry

    def flush_outbox(self):
        """Replay accounting the shared registry could not accept earlier."""
        try:
            self.outbox.flush(self.registry)
        except sqlite3.Error as exc:
            self.event(None, None, "registry_degraded", str(exc))

    def reserve(self, provider, bucket, objective_id, run_id, task_id, kind, tokens=None):
        """Take account-global allowance before dispatching one invocation.

        Under a configured cap this is the admission gate and a registry fault
        refuses admission: silently reopening allowance would let concurrent
        targets overspend the same subscription. In default monitoring mode a
        registry fault only degrades attribution, which is recorded and does
        not stop productive work.
        """
        capped = bool(self.caps)
        try:
            return self.registry.reserve(
                provider=provider, account=account_identity(provider), bucket=bucket,
                objective_id=objective_id, run_id=run_id, task_id=task_id, kind=kind,
                limits=self.caps, tokens=tokens or DEFAULT_RESERVATION_TOKENS)
        except (CapReached, CapWait):
            raise
        except (sqlite3.Error, OSError) as exc:
            self.registry_failed = True
            self.event(run_id, task_id, "registry_degraded", str(exc))
            if capped:
                raise CapWait("registry", time.time() + 300,
                              "Accounting registry unavailable under a configured limit")
            return None

    def settle(self, reservation_id, actual):
        """Reconcile one reservation exactly once, or queue it durably."""
        if not reservation_id:
            return
        try:
            self.registry.reconcile(reservation_id, actual)
        except (sqlite3.Error, OSError):
            self.outbox.add({"op": "reconcile", "reservation_id": reservation_id,
                             "actual": actual})

    def begin_invocation(self, run_id, task_id, attempt, kind, provider, model, effort,
                         bucket="codex", tokens=None):
        if kind not in ("plan", "implement", "repair", "review", "improve"):
            raise ValueError("Invalid invocation kind")
        run = self.db.execute("SELECT objective_id FROM runs WHERE id=?", (run_id,)).fetchone()
        objective_id = run["objective_id"] if run else run_id
        reservation_id = self.reserve(provider, bucket, objective_id, run_id, task_id,
                                      kind, tokens)
        invocation_id = uuid.uuid4().hex
        self.db.execute("""INSERT INTO invocations(
            id,at,objective_id,run_id,task_id,attempt,kind,provider,model,effort,status,
            reservation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (invocation_id, time.time(),
            objective_id, run_id, task_id, attempt, kind,
            provider, model, effort, "intended", reservation_id))
        self.db.commit()
        return invocation_id

    def finish_invocation(self, invocation_id, result):
        if not invocation_id:
            return
        row = self.db.execute("SELECT at,provider,reservation_id FROM invocations WHERE id=?",
                              (invocation_id,)).fetchone()
        if not row:
            raise ValueError("Unknown invocation")
        usage = result.usage if isinstance(result.usage, dict) else {}
        def known(*names):
            values = [usage.get(name) for name in names]
            values = [value for value in values if isinstance(value, int)]
            return sum(values) if values else None
        input_tokens = known("input_tokens")
        output_tokens = known("output_tokens")
        cached_tokens = known("cached_input_tokens", "cache_read_input_tokens",
                              "cache_creation_input_tokens")
        self.db.execute("""UPDATE invocations SET input_tokens=?,output_tokens=?,cached_tokens=?,
            estimated_usd=?,duration=?,status=?,fingerprint=? WHERE id=? AND status='intended'""",
            (input_tokens, output_tokens, cached_tokens, result.estimated_usd,
             max(0, time.time() - row["at"]), result.status,
             fingerprint(result.error or result.response), invocation_id))
        self.db.commit()
        # Unknown usage stays unreconciled: its conservative reservation keeps
        # holding allowance rather than being reported as zero tokens.
        spent = token_total(row["provider"], usage) if usage else None
        self.settle(row["reservation_id"], spent)

    def record_failure(self, run, task, failure, strategy, workspace, count=True):
        """Count one occurrence of this evidence, or read its history when count=False."""
        fp = fingerprint(classify(failure) + "\n" + str(failure), workspace)
        task_key = task.get("semantic_id", task["id"])
        if not count:
            row = self.db.execute("""SELECT count,replan_attempted FROM fingerprints
                WHERE objective_id=? AND task_key=? AND fp=? AND strategy=?""",
                (run["objective_id"], task_key, fp, strategy)).fetchone()
            return fp, (row["count"] if row else 0), bool(row and row["replan_attempted"])
        self.db.execute("""INSERT INTO fingerprints(
            objective_id,task_key,fp,strategy,count,last_at,last_example) VALUES (?,?,?,?,1,?,?)
            ON CONFLICT(objective_id,task_key,fp,strategy) DO UPDATE SET
            count=count+1,last_at=excluded.last_at,last_example=excluded.last_example""",
            (run["objective_id"], task_key, fp, strategy, time.time(), failure[-4000:]))
        self.db.commit()
        row = self.db.execute("""SELECT count,replan_attempted FROM fingerprints
            WHERE objective_id=? AND task_key=? AND fp=? AND strategy=?""",
            (run["objective_id"], task_key, fp, strategy)).fetchone()
        return fp, row["count"], bool(row["replan_attempted"])

    def mark_replan(self, run, task, fp, strategy):
        """Consume this evidence's single replanning opportunity, durably.

        Persisted *before* the planner call so a crash mid-replan cannot buy a
        second one, and keyed by the failure evidence rather than the plan id so
        a renamed successor cannot reset the history.
        """
        self.db.execute("""UPDATE fingerprints SET replan_attempted=1
            WHERE objective_id=? AND task_key=? AND fp=? AND strategy=?""",
            (run["objective_id"], task.get("semantic_id", task["id"]), fp, strategy))
        self.db.commit()

    def material_change(self, plan, successor):
        """Renaming, rewording, or resplitting a failed task is not a new approach."""
        def shape(value):
            return sorted((task.get("semantic_id", task["id"]), tuple(sorted(task["files"])),
                           tuple(task["check"])) for task in value["tasks"])
        return shape(plan) != shape(successor)

    def replan(self, plan, run, task, failure):
        """Author one validated successor from the failure evidence, or return None."""
        from .planner import repair_plan
        try:
            repaired = repair_plan(plan, failure, self.repo, engine=self)
        except (OSError, RuntimeError, ValueError) as exc:
            self.event(plan["id"], task["id"], "replan_unavailable", str(exc))
            return None
        if not self.material_change(plan, repaired):
            self.event(plan["id"], task["id"], "replan_equivalent",
                       "The repaired plan proposes no materially different approach")
            return None
        changes = {key: value for key, value in repaired.items()
                   if key not in ("id", "objective_id", "parent_id", "reason")}
        try:
            successor = self.succeed_plan(plan, "Replanned after repeated failure: " + failure[-400:],
                                          changes)
        except (OSError, RuntimeError, ValueError) as exc:
            self.event(plan["id"], task["id"], "replan_invalid", str(exc))
            return None
        self.decide(plan["id"], task["id"], "Repeated failure with no untried strategy",
                    ["retry same strategy", "replan", "park"], "replan", failure[-400:],
                    [successor["id"]], "passed")
        self.event(plan["id"], task["id"], "superseded", successor["id"])
        return successor

    def succeed_plan(self, plan, reason, changes):
        """Create an immutable successor rooted at the parent's accepted HEAD."""
        parent = self.initialize(plan)
        workspace = Path(parent["workspace"])
        if self.db.execute("SELECT 1 FROM tasks WHERE run_id=? AND status='running'",
                           (plan["id"],)).fetchone():
            raise RuntimeError("Cannot supersede a plan with an active worker")
        successor = json.loads(json.dumps(plan))
        successor.update(changes)
        root_id = re.sub(r"-r\d+$", "", plan["id"])
        number = 1
        while self.db.execute("SELECT 1 FROM runs WHERE id=?", (root_id + "-r" + str(number),)).fetchone():
            number += 1
        suffix = "-r" + str(number)
        successor["id"] = root_id[:61-len(suffix)] + suffix
        successor["objective_id"] = parent["objective_id"] or plan.get("objective_id", plan["id"])
        successor["parent_id"] = plan["id"]
        successor["reason"] = reason
        authority = json.loads(parent["policy"])
        if "policy" not in changes:
            successor["policy"] = authority
        candidate = canonical_policy(successor, self.repo)
        if not policy_narrows(candidate, authority):
            raise ValueError("Successor policy expands its parent's authority")
        validate_plan(successor, self.repo)
        encoded = json.dumps(successor, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        base = git(workspace, "rev-parse", "HEAD")
        branch = "loop/" + successor["id"]
        target = self.home / "worktrees" / successor["id"]
        intent_path = self.home / "intents" / (successor["id"] + ".json")
        atomic_json(intent_path, {"run_id": successor["id"], "plan": successor,
                    "digest": digest, "common": str(git_common_dir(self.repo)),
                    "workspace": str(target), "branch": branch, "base": base,
                    "nonce": uuid.uuid4().hex, "parent_id": plan["id"]})
        with self.repo_lock():
            git(self.repo, "worktree", "add", "-b", branch, str(target), base)
            with self.db:
                self.db.execute("""INSERT INTO runs(
                    id,plan,digest,workspace,branch,base,objective_id,policy,policy_version,
                    parent_id,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (successor["id"], encoded, digest, str(target), branch, base,
                     successor["objective_id"], json.dumps(candidate, sort_keys=True),
                     policy_digest(candidate), plan["id"], reason))
                old_tasks = {task["id"]: task for task in plan["tasks"]}
                for task in successor["tasks"]:
                    status, sha = "pending", None
                    old = old_tasks.get(task["id"])
                    row = self.db.execute("SELECT * FROM tasks WHERE run_id=? AND id=?",
                                          (plan["id"], task["id"])).fetchone()
                    if old == task and row and row["status"] == "done":
                        code, _, _ = run_process(task["check"], target,
                                                successor.get("check_timeout_seconds", 30))
                        if code == 0:
                            status, sha = "done", row["sha"]
                    self.db.execute("""INSERT INTO tasks(
                        run_id,id,status,sha,selected_provider,selected_model,selected_effort)
                        VALUES (?,?,?,?,?,?,?)""", (successor["id"], task["id"], status, sha,
                        task["provider"], task.get("model"), task.get("effort", "low")))
                self.db.execute("UPDATE runs SET superseded_by=? WHERE id=?",
                                (successor["id"], plan["id"]))
            intent_path.unlink()
        return successor

    # -- detached worker lifecycle ----------------------------------------

    def attempt_path(self, attempt_id):
        return self.home / "attempts" / (attempt_id + ".json")

    def workspace_owner_present(self, workspace):
        """True when someone still owns this workspace; ownership is the lock."""
        from .worker import workspace_lock_path
        path = workspace_lock_path(self.home, workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False

    def open_attempt(self, run_id, task_id):
        """Return the most recent unreaped attempt record for this task, if any."""
        directory = self.home / "attempts"
        if not directory.exists():
            return None
        best = None
        for path in directory.glob("*.json"):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if record.get("run_id") != run_id or record.get("task_id") != task_id:
                continue
            if best is None or record.get("created_at", 0) > best.get("created_at", 0):
                best = record
        return best

    def read_result(self, record):
        """Load a worker result only if every recorded identity still matches."""
        path = Path(record["result_path"])
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return "stale"
        for key in ("attempt_id", "nonce", "run_id", "task_id", "base", "policy_version"):
            if payload.get(key) != record.get(key):
                return "stale"
        data = payload.get("result", {})
        return Result(data.get("status", "error"), data.get("response", ""),
                      data.get("usage", {}) or {}, data.get("retry_at", 0) or 0,
                      data.get("error", ""), data.get("estimated_usd", 0) or 0,
                      bool(data.get("invoked", True)))

    def retire_attempt(self, record):
        self.attempt_path(record["attempt_id"]).unlink(missing_ok=True)
        Path(record["result_path"]).unlink(missing_ok=True)

    def reconcile_attempt(self, run, task, workspace, now=None):
        """Resolve an earlier attempt before considering a replacement.

        Returns ``(disposition, result)`` where disposition is ``free`` (nothing
        outstanding), ``recovering`` (ownership unknown; never dispatch), or
        ``reaped`` with the worker's result.
        """
        now = time.time() if now is None else now
        record = self.open_attempt(run["id"], task["id"])
        if not record:
            return "free", None
        result = self.read_result(record)
        if result == "stale":
            self.event(run["id"], task["id"], "stale_result_rejected", record["attempt_id"])
            self.retire_attempt(record)
            return "free", None
        if result is not None:
            if record.get("base") != git(workspace, "rev-parse", "HEAD"):
                # The worktree moved under a finished attempt: its result
                # describes a state we are no longer in.
                self.event(run["id"], task["id"], "stale_result_rejected", record["attempt_id"])
                self.retire_attempt(record)
                return "free", None
            return "reaped", (record, result)
        if self.workspace_owner_present(workspace):
            stalled = now - max(record.get("heartbeat_at", 0), record.get("created_at", 0))
            if stalled > record.get("lease_seconds", 86400):
                self.terminate_worker(run, task, record)
            # A live owner holds the workspace. A replacement worker here would
            # corrupt shared state, so wait for it rather than guessing.
            self.event(run["id"], task["id"], "recovering", record["attempt_id"])
            return "recovering", record
        # No owner, no result: the worker died. Its uncommitted work is discarded.
        self.event(run["id"], task["id"], "worker_lost", record["attempt_id"])
        self.retire_attempt(record)
        return "free", None

    def terminate_worker(self, run, task, record):
        """Stop an over-lease worker, but only a process group we can prove is ours.

        A recorded PID is not ownership: PIDs are reused. The process start
        identity captured at registration is what distinguishes this worker
        from an unrelated process that inherited its number.
        """
        from .worker import process_identity
        pid = record.get("pid")
        if not pid or not record.get("start_identity"):
            return False
        if process_identity(pid) != record["start_identity"]:
            self.event(run["id"], task["id"], "ownership_unverified", pid)
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except (OSError, PermissionError) as exc:
            self.event(run["id"], task["id"], "terminate_failed", str(exc))
            return False
        self.event(run["id"], task["id"], "lease_expired", record["attempt_id"])
        return True

    def launch_worker(self, plan, run, task, attempt, workspace, previous_error="",
                      snapshot=None, invocation_id=None):
        """Persist launch intent, then spawn a detached, self-owning worker."""
        attempt_id = uuid.uuid4().hex
        record = {
            "attempt_id": attempt_id, "nonce": uuid.uuid4().hex, "run_id": run["id"],
            "task_id": task["id"], "attempt": attempt, "task": task,
            "workspace": str(workspace), "home": str(self.home),
            "base": git(workspace, "rev-parse", "HEAD"),
            "policy_version": run["policy_version"], "created_at": time.time(),
            "state": "intended", "pid": None, "start_identity": "", "heartbeat_at": 0,
            "result_path": str(self.home / "results" / (attempt_id + ".json")),
            "log_directory": str(self.home / "logs" / run["id"]),
            "previous_error": previous_error,
            "snapshot": snapshot or {}, "invocation_id": invocation_id,
            "context_characters": model_budget(json.loads(run["policy"]), task.get("model"),
                                               plan.get("context_characters")),
            "worker_timeout_seconds": self.worker_timeout(plan, task),
            "unknown_quota_retry_seconds": plan.get("unknown_quota_retry_seconds", 1800),
            "provider_runner_timeout_seconds": plan.get("provider_runner_timeout_seconds", 86400),
            "lease_seconds": plan.get("worker_lease_seconds", 86400),
            "sandbox": plan.get("provider_sandbox", False),
        }
        path = self.attempt_path(attempt_id)
        # Launch intent is durable before the spawn, so a crash in between
        # leaves a record to reconcile rather than an invisible orphan.
        atomic_json(path, record)
        self.spawn([sys.executable, "-m", "loop.worker", str(path)])
        return record

    def spawn(self, argv):
        """Start a detached session leader; the worker owns its own lifetime."""
        return subprocess.Popen(argv, cwd=str(Path(__file__).resolve().parent.parent),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    def await_worker(self, record, workspace, poll=0.1, launch_grace=30.0):
        """Poll for a durable result independently of the maximum lease.

        Ownership only becomes evidence once the worker has completed its launch
        handshake: between the spawn and its first registration the workspace
        lock is legitimately free, and treating that as a dead worker would race
        a replacement into the same worktree.
        """
        started = time.time()
        deadline = started + record["lease_seconds"]
        path = self.attempt_path(record["attempt_id"])
        while time.time() < deadline:
            result = self.read_result(record)
            if result == "stale":
                return Result("error", error="Worker wrote a result for another attempt")
            if isinstance(result, Result):
                return result
            try:
                current = json.loads(path.read_text())
            except (OSError, ValueError):
                current = record
            registered = current.get("state") in ("registered", "running", "result_written")
            if not registered and time.time() - started < launch_grace:
                time.sleep(poll)
                continue
            if not self.workspace_owner_present(workspace):
                # Give an exiting worker a moment to land its atomic result.
                time.sleep(poll)
                result = self.read_result(record)
                return result if isinstance(result, Result) else None
            time.sleep(poll)
        return None

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

    def hold(self, provider, until, reason, now=None):
        now = time.time() if now is None else now
        self.db.execute(
            "UPDATE providers SET retry_at=?,reason=?,held_since=CASE WHEN held_since>0 "
            "THEN held_since ELSE ? END WHERE name=?", (until, reason, now, provider))
        self.db.commit()

    def recover_providers(self, plan, now=None):
        """Probe held providers without inference, so waiting can end on its own.

        Credential and quota recovery must not depend on some other task
        succeeding: when every task is waiting, nothing else will ever run to
        discover that the provider came back.
        """
        now = time.time() if now is None else now
        for row in self.db.execute("SELECT * FROM providers WHERE retry_at>0").fetchall():
            provider, held_until = row["name"], row["retry_at"]
            probe_at = row["probe_at"] if "probe_at" in row.keys() else 0
            if probe_at and probe_at > now:
                continue
            kind = "auth" if row["reason"].startswith("auth:") else "quota"
            interval = health.learned_interval(self.registry.db, provider,
                                               account_identity(provider), kind,
                                               plan.get("probe_interval_seconds", 900))
            recovered = None
            if kind == "auth":
                recovered = auth_probe(provider, self.repo,
                                       plan.get("probe_timeout_seconds", 30))
            elif held_until <= now:
                try:
                    limits = provider_limits(provider, self.repo,
                                             plan.get("probe_timeout_seconds", 30))
                    recovered = not quota_deadline(limits, now)
                except (FileNotFoundError, OSError, RuntimeError, TimeoutError, ValueError):
                    recovered = None
            if recovered:
                waited = max(0.0, now - (row["held_since"] or now))
                try:
                    health.record_recovery(self.registry.db, provider,
                                           account_identity(provider), kind, waited, now)
                except sqlite3.Error:
                    pass
                self.db.execute(
                    "UPDATE providers SET retry_at=0,reason='',probe_at=0,held_since=0 WHERE name=?",
                    (provider,))
                self.db.execute(
                    "UPDATE tasks SET status='pending',retry_at=0 WHERE status='waiting' AND id IN "
                    "(SELECT id FROM tasks WHERE selected_provider=?)", (provider,))
                self.db.commit()
                self.event(None, None, "provider_recovered", provider)
                continue
            # Unknown or still unavailable: schedule the next non-inference probe.
            self.db.execute("UPDATE providers SET probe_at=? WHERE name=?",
                            (now + interval, provider))
            self.db.commit()

    def worker_timeout(self, plan, task):
        """A per-invocation timeout learned from durations, bounded and censored-safe."""
        configured = plan.get("worker_timeout_seconds", 180)
        if not plan.get("auto_tune", False):
            return configured
        try:
            return health.learned_timeout(self.registry.db, task["provider"], task.get("model"),
                                          task.get("effort", "low"), health.task_class(task),
                                          configured)
        except sqlite3.Error:
            return configured

    def refresh_headroom(self, plan, providers, now=None):
        """Cache each provider's remaining quota fraction, politely.

        Reading quota spawns a CLI, so it is rate-limited per provider and never
        blocks work: an unreadable provider keeps NULL headroom, which routing
        treats as unknown rather than as capacity.
        """
        now = time.time() if now is None else now
        interval = plan.get("headroom_refresh_seconds", 600)
        timeout = plan.get("probe_timeout_seconds", 30)
        for provider in sorted(set(providers)):
            row = self.db.execute("SELECT headroom_at FROM providers WHERE name=?",
                                  (provider,)).fetchone()
            if row and row["headroom_at"] and now - row["headroom_at"] < interval:
                continue
            fraction = None
            try:
                used = usage_fraction(provider_limits(provider, self.repo, timeout))
                fraction = None if used is None else max(0.0, 1.0 - used)
            except (FileNotFoundError, OSError, RuntimeError, TimeoutError, ValueError):
                fraction = None
            self.db.execute("UPDATE providers SET headroom=?,headroom_at=? WHERE name=?",
                            (fraction, now, provider))
        self.db.commit()

    def headroom(self, providers):
        known = {}
        for provider in set(providers):
            row = self.db.execute("SELECT headroom FROM providers WHERE name=?",
                                  (provider,)).fetchone()
            if row and row["headroom"] is not None:
                known[provider] = row["headroom"]
        return known

    def failover_model(self, run, plan, task, now):
        """Find a configured alternative that can run this task right now.

        Used when the current provider's wait is long enough that continuing on
        another provider beats sitting idle. The task keeps its identity, its
        history and its workspace: this changes who does the next attempt, it
        does not restart the work.
        """
        policy = json.loads(run["policy"])
        configured = policy.get("models", [])
        current = (task["provider"], task.get("model"), task.get("effort", "low"))
        alternatives = []
        for choice in configured:
            if (choice["provider"], choice["model"], choice.get("effort", "low")) == current:
                continue
            held = self.db.execute("SELECT retry_at FROM providers WHERE name=?",
                                   (choice["provider"],)).fetchone()
            if held and held["retry_at"] > now:
                continue
            alternatives.append(choice)
        if not alternatives:
            return None, "no configured alternative is available"
        self.refresh_headroom(plan, [choice["provider"] for choice in alternatives], now)
        try:
            chosen, reason = health.choose_route(
                self.registry.db, alternatives, health.task_class(task), now,
                headroom=self.headroom([choice["provider"] for choice in alternatives]))
        except sqlite3.Error:
            chosen, reason = alternatives[0], "first configured alternative"
        return chosen, reason

    def route_task(self, run, plan, task, row):
        """Select a configured model on measured evidence, recording the reason."""
        if task.get("model") or row["selected_model"] or not plan.get("auto_route", False):
            return None
        policy = json.loads(run["policy"])
        models = policy.get("models", [])
        try:
            self.refresh_headroom(plan, [m["provider"] for m in models])
            choice, reason = health.choose_route(
                self.registry.db, models, health.task_class(task),
                headroom=self.headroom([m["provider"] for m in models]))
        except sqlite3.Error:
            return None
        if not choice:
            return None
        self.set_task(run["id"], task["id"], selected_provider=choice["provider"],
                      selected_model=choice["model"],
                      selected_effort=choice.get("effort", "low"))
        self.decide(run["id"], task["id"], "Which configured model should implement this task?",
                    policy.get("models", []), choice["model"], reason,
                    [health.task_class(task)], "routed")
        return choice

    def ensure_setup(self, plan, run, workspace):
        setup = plan.get("setup")
        if not setup:
            return
        directory = self.home / "setup"
        directory.mkdir(exist_ok=True)
        marker = directory / (plan["id"] + "-" + run["digest"] + ".done")
        if marker.exists():
            return
        self.event(plan["id"], "setup", "running", setup)
        head = git(workspace, "rev-parse", "HEAD")
        code, out, err = run_process(setup, workspace, plan.get("setup_timeout_seconds", 600))
        if code:
            restore_worker_attempt(workspace, head)
            self.event(plan["id"], "setup", "blocked", (out + err)[-4000:])
            raise RuntimeError("Workspace setup failed: " + ((out + err)[-4000:] or str(code)))
        if changed_files(workspace):
            restore_worker_attempt(workspace, head)
            raise RuntimeError("Workspace setup changed tracked or unignored files")
        marker.write_text(str(time.time()))
        self.event(plan["id"], "setup", "done", "Workspace dependencies are ready")

    def environment_version(self, run, workspace):
        """Identify the environment a setup recipe would act on."""
        return run["policy_version"] + ":" + git(workspace, "rev-parse", "HEAD")

    def attempt_setup_recipe(self, plan, run, task, workspace, failure):
        """Run one policy-authorized setup recipe for a missing dependency.

        Returns True when the environment was changed and the task deserves
        another attempt. An unchanged recipe that already failed in this
        environment is never retried, and no unauthorized command ever runs.
        """
        policy = json.loads(run["policy"])
        recipe = matching_recipe(policy, failure)
        if not recipe:
            return False
        identity = recipe_id(recipe)
        environment = self.environment_version(run, workspace)
        previous = self.db.execute(
            "SELECT status FROM setup_runs WHERE run_id=? AND recipe=? AND environment=?",
            (run["id"], identity, environment)).fetchone()
        if previous:
            # Same recipe, same environment, already tried: repeating it is waste.
            self.event(run["id"], task["id"], "setup_recipe_exhausted", identity)
            return False
        # Persist the intent before execution so a crash mid-recipe cannot hide it.
        self.db.execute("INSERT INTO setup_runs VALUES (?,?,?,?,?,?)",
                        (run["id"], identity, environment, time.time(), "running", ""))
        self.db.commit()
        head = git(workspace, "rev-parse", "HEAD")
        self.event(run["id"], task["id"], "setup_recipe", recipe["argv"])
        code, out, err = run_process(recipe["argv"], workspace,
                                     recipe.get("timeout_seconds", 600))
        output = (out + err)[-4000:]
        if not code and changed_files(workspace):
            restore_worker_attempt(workspace, head)
            code, output = 1, "Setup recipe changed tracked or unignored files"
        self.db.execute(
            "UPDATE setup_runs SET status=?,output=? WHERE run_id=? AND recipe=? AND environment=?",
            ("done" if not code else "failed", output, run["id"], identity, environment))
        self.db.commit()
        self.decide(run["id"], task["id"], "Missing dependency: " + (missing_dependency(failure) or "unnamed"),
                    [recipe["argv"], "park for the environment"],
                    "setup_recipe" if not code else "recipe_failed",
                    "Applied an execution-policy setup recipe." if not code
                    else "The authorized recipe did not succeed.",
                    [output[-400:]], "passed" if not code else "failed")
        return not code

    def regression_candidates(self, plan, run, workspace, failing):
        """Files that plausibly caused a regression, intersected with policy.

        The failing test's own task is not assumed guilty: any accepted commit
        in this milestone could have caused it, so the candidate set is every
        accepted change that policy allows a worker to edit, minus the trusted
        acceptance surface.
        """
        policy = json.loads(run["policy"])
        changed = set(git(workspace, "diff", "--name-only", run["base"], "HEAD").splitlines())
        check_arguments = {argument for task in plan["tasks"] for argument in task["check"]}
        candidates = set()
        for name in changed:
            if not name or not path_allowed(name, policy):
                continue
            if any(name in argument for argument in check_arguments):
                continue
            candidates.add(name)
        return sorted(candidates)

    def repair_regression(self, plan, run, task, workspace, failure):
        """Author a validated successor carrying one repair task, or return None."""
        strategy = "regression"
        fp, _, repaired = self.record_failure(run, task, failure, strategy, workspace)
        if repaired:
            self.event(plan["id"], task["id"], "regression_repair_exhausted", fp)
            return None
        candidates = self.regression_candidates(plan, run, workspace, task)
        if not candidates:
            self.event(plan["id"], task["id"], "regression_no_candidates", fp)
            return None
        self.mark_replan(run, task, fp, strategy)
        repair_id = ("repair-" + task["id"])[:60]
        successor_tasks = [dict(item) for item in plan["tasks"]]
        if any(item["id"] == repair_id for item in successor_tasks):
            repair_id = (repair_id + "-" + uuid.uuid4().hex[:6])[:60]
        successor_tasks.append({
            "id": repair_id, "provider": task["provider"], "model": task.get("model"),
            "effort": task.get("effort", "low"), "files": candidates,
            "depends_on": [item["id"] for item in plan["tasks"]],
            "check": task["check"],
            "prompt": ("A previously accepted milestone now fails its trusted check. "
                       "Diagnose which accepted change caused it and repair the cause; do "
                       "not weaken or edit the check.\n\nFAILING CHECK: "
                       + json.dumps(task["check"]) + "\n\nDIAGNOSTIC:\n" + failure[-4000:])})
        try:
            successor = self.succeed_plan(
                plan, "Milestone regression after " + task["id"], {"tasks": successor_tasks})
        except (OSError, RuntimeError, ValueError) as exc:
            self.event(plan["id"], task["id"], "regression_repair_invalid", str(exc))
            return None
        self.decide(plan["id"], task["id"], "Milestone regression: " + failure[-200:],
                    ["repair the cause", "park"], "repair",
                    "Accepted work is preserved; a successor repairs the cause.",
                    candidates, "passed")
        self.event(plan["id"], task["id"], "superseded", successor["id"])
        return successor

    def review_failure(self, plan, task, workspace, failure):
        """Ask one advisory checker only when a plan opts into failure review."""
        if not plan.get("failure_review", False):
            return None
        from .review import review_failure
        provider = plan.get("failure_review_provider", task["provider"])
        try:
            verdict = review_failure(provider, plan.get("failure_review_model"), task, failure,
                                     workspace, plan.get("failure_review_timeout_seconds", 180),
                                     self, plan["id"])
            self.event(plan["id"], task["id"], "failure_review",
                       "retry" if verdict["retry"] else "block")
            return verdict
        except (OSError, RuntimeError, ValueError) as exc:
            self.event(plan["id"], task["id"], "failure_review_unavailable", str(exc))
            return None

    def tick(self, plan, worker=None, now=None):
        now = time.time() if now is None else now
        run = self.initialize(plan)
        self.flush_outbox()
        self.recover_providers(plan, now)
        workspace = Path(run["workspace"])
        if git(workspace, "branch", "--show-current") != run["branch"]:
            raise RuntimeError("Managed worktree branch changed")
        self.ensure_setup(plan, run, workspace)
        done_shas = {row["sha"] for row in self.db.execute(
            "SELECT sha FROM tasks WHERE run_id=? AND sha IS NOT NULL", (plan["id"],))}
        validate_managed_history(plan, run, workspace, done_shas)
        edges = dependencies(plan)
        deadlines = []
        # Dependencies determine eligibility before any workspace cleanup.
        for task in plan["tasks"]:
            row = self.db.execute("SELECT * FROM tasks WHERE run_id=? AND id=?",
                                  (plan["id"], task["id"])).fetchone()
            task = dict(task)
            task["provider"] = row["selected_provider"] or task["provider"]
            if row["selected_model"]:
                task["model"] = row["selected_model"]
            task["effort"] = row["selected_effort"] or task.get("effort", "low")
            if row["status"] == "done":
                git(workspace, "merge-base", "--is-ancestor", row["sha"], "HEAD")
                continue
            if row["status"] == "blocked":
                return "blocked", 0
            if row["status"] == "parked":
                continue
            if any(self.db.execute("SELECT status FROM tasks WHERE run_id=? AND id=?",
                                   (plan["id"], parent)).fetchone()[0] != "done"
                   for parent in edges[task["id"]]):
                continue
            # Reconcile an outstanding detached attempt before touching the
            # worktree: its uncommitted work is evidence, not debris.
            carried = None
            if worker is None and row["status"] == "running":
                disposition, payload = self.reconcile_attempt(run, task, workspace, now)
                if disposition == "recovering":
                    deadlines.append(now + 30)
                    continue
                if disposition == "reaped":
                    carried = payload
                    self.event(plan["id"], task["id"], "reaped", carried[0]["attempt_id"])
            changed = changed_files(workspace)
            if changed - set(task["files"]):
                raise RuntimeError("Unexpected changes in managed worktree; inspect before resuming")
            marker = "Agent-Loop-Task: " + plan["id"] + "/" + task["id"]
            # Reconcile a crash after git commit but before the database transaction.
            sha = git(workspace, "log", run["base"]+"..HEAD", "--format=%H", "--fixed-strings",
                      "--grep="+marker, "-1")
            if sha and not row["error"].startswith("Milestone regression:"):
                self.set_task(plan["id"], task["id"], status="done", sha=sha)
                continue
            # A failed check or interrupted process must never become the next
            # attempt's starting point. Managed worktrees are supervisor-owned.
            if changed and carried is None:
                restore_task_files(workspace, task["files"])
            if row["status"] == "running" and carried is None:
                self.set_task(plan["id"], task["id"], status="pending",
                              error="Recovered an interrupted worker attempt")
                row = self.db.execute("SELECT * FROM tasks WHERE run_id=? AND id=?",
                                      (plan["id"], task["id"])).fetchone()
            if carried is None:
                provider = self.db.execute("SELECT * FROM providers WHERE name=?", (task["provider"],)).fetchone()
                retry_at = max(row["retry_at"], provider["retry_at"])
                if retry_at > now:
                    # A long wait is not free: finishing on another configured
                    # provider beats idling. The task keeps its id, attempts,
                    # accumulated error context and workspace — only the next
                    # attempt's provider changes, so this continues the work
                    # rather than restarting it.
                    threshold = plan.get("failover_after_seconds", 3600)
                    if retry_at - now >= threshold:
                        choice, reason = self.failover_model(run, plan, task, now)
                        if choice:
                            self.set_task(plan["id"], task["id"],
                                          selected_provider=choice["provider"],
                                          selected_model=choice["model"],
                                          selected_effort=choice.get("effort", "low"),
                                          retry_at=0)
                            self.decide(plan["id"], task["id"],
                                        "Provider %s is unavailable for %d minutes"
                                        % (task["provider"], (retry_at - now) // 60),
                                        [task["provider"], choice["provider"]],
                                        choice["provider"],
                                        "Continued the same task on an available provider: "
                                        + reason, [str(retry_at)], "routed")
                            self.event(plan["id"], task["id"], "failover", choice["provider"])
                            task = dict(task, provider=choice["provider"],
                                        model=choice["model"],
                                        effort=choice.get("effort", "low"))
                            row = self.db.execute(
                                "SELECT * FROM tasks WHERE run_id=? AND id=?",
                                (plan["id"], task["id"])).fetchone()
                        else:
                            self.event(plan["id"], task["id"], "failover_unavailable", reason)
                            deadlines.append(retry_at)
                            continue
                    else:
                        deadlines.append(retry_at)
                        continue
                budget = plan.get("provider_token_budgets", {}).get(task["provider"])
                if budget is not None and self.run_tokens(plan, task["provider"]) >= budget:
                    self.park(plan["id"], task["id"], "Local admission token budget reached for provider " + task["provider"], "policy")
                    continue
                task_budget = task.get("token_budget")
                if task_budget is not None and row["tokens"] >= task_budget:
                    self.park(plan["id"], task["id"], "Local admission token budget reached for this section", "policy")
                    continue
                if plan.get("max_attempts") is not None and row["attempts"] >= plan["max_attempts"]:
                    self.park(plan["id"], task["id"], "Repair attempts exhausted")
                    continue
                # One bound over the whole prompt, shared with the worker
                # boundary, rather than a per-file cap that bounds nothing.
                sections = [("instructions",
                             "Implement this coding section directly in the managed worktree. "
                             "Use repository tools to read, search, edit, and run focused diagnostics. "
                             "You may modify only these paths: " + json.dumps(task["files"]) + ". "
                             "Do not commit or change branches.\n" + task["prompt"])]
                if row["error"]:
                    sections.append(("previous failure",
                                     "\nPrevious attempt failed this trusted check; fix the issue:\n"
                                     + row["error"][-4000:]))
                for name in task.get("context_files", []):
                    sections.append(("context " + name, "\nCONTEXT " + name + "\n"
                                     + safe_path(workspace, name).read_text()))
                budget = model_budget(json.loads(run["policy"]), task.get("model"),
                                      plan.get("context_characters"))
                prompt = fit(sections, budget)
                routed = self.route_task(run, plan, task, row)
                if routed:
                    task = dict(task, provider=routed["provider"], model=routed["model"],
                                effort=routed.get("effort", "low"))
                # Admission precedes the attempt: a refused invocation never happened,
                # so it must not consume this task's retry history.
                try:
                    invocation_id = self.begin_invocation(
                        plan["id"], task["id"], row["attempts"] + 1, "implement",
                        task["provider"], task.get("model"), task.get("effort", "low"),
                        task.get("quota_bucket", "codex"))
                except CapWait as wait:
                    # A rolling limit is a deadline, not a coding failure: the task
                    # keeps its attempt count and releases the slot.
                    self.set_task(plan["id"], task["id"], status="waiting",
                                  retry_at=wait.deadline, error=str(wait))
                    self.event(plan["id"], task["id"], "limit_waiting", wait.deadline)
                    deadlines.append(wait.deadline)
                    continue
                except CapReached as reached:
                    self.park(plan["id"], task["id"], str(reached), "policy")
                    self.event(plan["id"], task["id"], "limit_reached", reached.scope)
                    continue
                self.set_task(plan["id"], task["id"], status="running", attempts=row["attempts"]+1, retry_at=0)
                self.event(plan["id"], task["id"], "running", task["provider"])
                head = git(workspace, "rev-parse", "HEAD")
                before = file_snapshot(workspace, task["files"])
            else:
                # A reaped result is validated against the identities recorded
                # before its worker started, not against the worktree today.
                record, result = carried
                invocation_id = record.get("invocation_id")
                head, before = record["base"], record["snapshot"]
            try:
                if carried is not None:
                    self.retire_attempt(record)
                elif worker:
                    result = worker(task, prompt, workspace)
                else:
                    record = self.launch_worker(plan, run, task, row["attempts"] + 1,
                                                workspace, row["error"],
                                                before, invocation_id)
                    result = self.await_worker(record, workspace)
                    if result is None:
                        # Ownership is unresolved. Leave the attempt record in
                        # place: the next tick reconciles it instead of racing
                        # a replacement worker into the same worktree.
                        self.set_task(plan["id"], task["id"], status="waiting",
                                      retry_at=now + 30,
                                      error="Worker ownership is unresolved; recovering")
                        self.event(plan["id"], task["id"], "recovering", record["attempt_id"])
                        deadlines.append(now + 30)
                        continue
                    self.retire_attempt(record)
            except KeyboardInterrupt:
                restore_worker_attempt(workspace, head)
                self.set_task(plan["id"], task["id"], status="pending", attempts=row["attempts"],
                              error="Worker interrupted; its uncommitted changes were discarded")
                raise
            except OSError as exc:
                restore_worker_attempt(workspace, head)
                self.finish_invocation(invocation_id, Result("error", error=str(exc)))
                self.park(plan["id"], task["id"], str(exc), "environment")
                continue
            self.finish_invocation(invocation_id, result)
            spent = token_total(task["provider"], result.usage)
            try:
                health.record_sample(
                    self.registry.db, provider=task["provider"],
                    account=account_identity(task["provider"]),
                    bucket=task.get("quota_bucket", "codex"), model=task.get("model"),
                    effort=task.get("effort", "low"), task_class=health.task_class(task),
                    kind="implement", outcome=result.status, tokens=spent or None,
                    duration=max(0.0, time.time() - now),
                    censored=result.status == "transient" and "timeout" in (result.error or "").lower(),
                    now=now)
            except sqlite3.Error:
                self.registry_failed = True
            self.db.execute("UPDATE providers SET tokens=tokens+?,estimated_usd=estimated_usd+? WHERE name=?",
                            (spent, result.estimated_usd, task["provider"]))
            self.db.execute("UPDATE tasks SET tokens=tokens+? WHERE run_id=? AND id=?",
                            (spent, plan["id"], task["id"]))
            self.db.commit()
            if result.status in ("rate_limited", "provider_wait"):
                restore_worker_attempt(workspace, head)
                # Unknown reset: probe slowly; this is a retry time, not a claimed reset.
                delay = plan.get("unknown_quota_retry_seconds", 1800)
                deadline = result.retry_at or now + delay
                self.hold(task["provider"], deadline, result.error or "Rate limited")
                attempts = row["attempts"] if result.status == "provider_wait" and not result.invoked else row["attempts"] + 1
                self.set_task(plan["id"], task["id"], status="waiting", attempts=attempts,
                              retry_at=deadline, error=result.error)
                self.event(plan["id"], task["id"], "waiting", deadline)
                deadlines.append(deadline)
                continue
            if result.status != "ok":
                restore_worker_attempt(workspace, head)
                state = "waiting" if result.status == "transient" else "parked"
                deadline = now + min(900, 30 * 2**row["attempts"]) if state == "waiting" else 0
                strategy = ":".join((task["provider"], str(task.get("model") or "default"),
                                     task.get("effort", "low")))
                # A provider fault is not a coding failure, but unchanged evidence
                # must still terminate: an endlessly retried transient is waste.
                _, occurrences, _ = self.record_failure(
                    run, task, result.status + ": " + (result.error or ""), strategy, workspace)
                if state == "waiting" and occurrences >= plan.get("transient_attempts", 5):
                    self.park(plan["id"], task["id"],
                              "Unchanged transient provider failure: " + (result.error or ""),
                              "environment")
                    self.event(plan["id"], task["id"], "parked_no_progress", result.error)
                    continue
                if state == "parked" and result.status == "auth_required":
                    # Credentials recover through a periodic non-inference probe,
                    # never through another task happening to succeed.
                    interval = plan.get("probe_interval_seconds", 900)
                    self.hold(task["provider"], now + interval,
                              "auth: " + (result.error or "authentication failed"), now)
                    self.set_task(plan["id"], task["id"], status="waiting",
                                  retry_at=now + interval, wake_kind="credentials",
                                  error=result.error)
                    self.event(plan["id"], task["id"], "waiting", now + interval)
                    deadlines.append(now + interval)
                    continue
                if state == "parked" and result.status == "error":
                    verdict = self.review_failure(plan, task, workspace, result.error)
                    if verdict and verdict["retry"]:
                        state = "pending"
                        result.error = "Failure review: " + verdict["reasoning"] + "\n" + result.error
                self.set_task(plan["id"], task["id"], status=state, retry_at=deadline, error=result.error)
                self.event(plan["id"], task["id"], state, result.error)
                if state == "parked":
                    self.park(plan["id"], task["id"], result.error, "environment")
                elif state == "waiting":
                    deadlines.append(deadline)
                else:
                    return "progress", 0
                continue
            if result.retry_at:
                self.hold(task["provider"], result.retry_at, "Quota exhausted after completed section")
            try:
                # Injected workers used by tests may still return the legacy JSON
                # payload. Real CLI workers edit the managed worktree directly.
                if git(workspace, "rev-parse", "HEAD") != head:
                    raise ValueError("Worker changed the managed Git history")
                if not changed_files(workspace) and asked_question(result.response):
                    restore_worker_attempt(workspace, head)
                    rule = ("Decision rule: choose the simplest option consistent with the objective "
                            "and repository conventions; do not ask.")
                    count = row["question_count"] + 1
                    if count == 1:
                        self.set_task(plan["id"], task["id"], status="pending",
                                      question_count=count, error=rule)
                        self.decide(plan["id"], task["id"], result.response[-400:],
                                    ["choose within policy", "change strategy"], "answered_once",
                                    "Routine implementation choices are delegated to the worker.",
                                    ["objective", "repository conventions"])
                        return "progress", 0
                    policy = json.loads(run["policy"])
                    configured = policy.get("models", [])
                    current = (task["provider"], task.get("model"), task.get("effort", "low"))
                    positions = [(m["provider"], m["model"], m.get("effort", "low"))
                                 for m in configured]
                    next_model = None
                    if current in positions and positions.index(current) + 1 < len(positions):
                        next_model = configured[positions.index(current) + 1]
                    elif current not in positions and configured:
                        next_model = configured[0]
                    if next_model:
                        self.set_task(plan["id"], task["id"], status="pending",
                                      question_count=count, error=rule,
                                      selected_provider=next_model["provider"],
                                      selected_model=next_model["model"],
                                      selected_effort=next_model.get("effort", "low"))
                        self.decide(plan["id"], task["id"], "Repeated worker question",
                                    positions, next_model["model"], "Changed configured strategy",
                                    [result.response[-400:]])
                        return "progress", 0
                    self.park(plan["id"], task["id"], "Repeated question without a configured alternative")
                    return "parked", 0
                if worker is not None and not changed_files(workspace) and result.response.lstrip().startswith("{"):
                    self.apply_result(task, result.response, workspace)
                if not changed_files(workspace):
                    code, out, err = run_process(task["check"], workspace,
                                                 plan.get("check_timeout_seconds", 30))
                    if code == 0:
                        self.set_task(plan["id"], task["id"], status="done", sha=head, error="")
                        self.decide(plan["id"], task["id"], "Worker made no change",
                                    ["already satisfied", "retry"], "already_satisfied",
                                    "The trusted check passed at the recorded SHA.", [task["check"]],
                                    "passed")
                        self.event(plan["id"], task["id"], "already_satisfied", head)
                        return "progress", 0
                    raise ValueError((out + err)[-4000:] or "Check exited " + str(code))
                validate_worker_changes(task, workspace, before, head)
                code, out, err = run_process(task["check"], workspace, plan.get("check_timeout_seconds", 30))
                if code:
                    raise ValueError((out+err)[-4000:] or "Check exited " + str(code))
                if git(workspace, "rev-parse", "HEAD") != head:
                    raise ValueError("Trusted check changed the managed Git history")
                unexpected = changed_files(workspace) - set(task["files"])
                if unexpected:
                    raise ValueError("Trusted check changed unexpected files: "
                                     + ", ".join(sorted(unexpected)))
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
                failure = str(exc)
                strategy = ":".join((task["provider"], str(task.get("model") or "default"),
                                     task.get("effort", "low")))
                _, count, _ = self.record_failure(run, task, failure, strategy, workspace)
                if classify(failure) == MISSING_DEPENDENCY:
                    # The environment, not the code, is what failed. Repeated
                    # occurrences still count toward waste control above.
                    restore_worker_attempt(workspace, head)
                    if self.attempt_setup_recipe(plan, run, task, workspace, failure):
                        self.set_task(plan["id"], task["id"], status="pending", error=failure)
                        self.event(plan["id"], task["id"], "needs_setup", failure)
                        return "progress", 0
                    if count >= 2:
                        self.park(plan["id"], task["id"],
                                  "No authorized setup recipe resolves: " + failure,
                                  "environment")
                        self.event(plan["id"], task["id"], "parked_no_progress", failure)
                        return "parked", 0
                    self.set_task(plan["id"], task["id"], status="pending", error=failure)
                    self.event(plan["id"], task["id"], "needs_setup", failure)
                    return "progress", 0
                verdict = self.review_failure(plan, task, workspace, failure) if count == 1 else None
                restore_worker_attempt(workspace, head)
                if verdict:
                    failure = "Failure review: " + verdict["reasoning"] + "\nTrusted check: " + failure
                if count >= 2:
                    policy = json.loads(run["policy"])
                    choices = policy.get("models", [])
                    current = (task["provider"], task.get("model"), task.get("effort", "low"))
                    positions = [(choice["provider"], choice["model"], choice.get("effort", "low"))
                                 for choice in choices]
                    next_model = None
                    if current in positions and positions.index(current) + 1 < len(positions):
                        next_model = choices[positions.index(current) + 1]
                    elif current not in positions and choices:
                        next_model = choices[0]
                    if next_model:
                        self.set_task(plan["id"], task["id"], status="pending", error=failure,
                                      selected_provider=next_model["provider"],
                                      selected_model=next_model["model"],
                                      selected_effort=next_model.get("effort", "low"))
                        self.event(plan["id"], task["id"], "strategy_changed", next_model)
                        return "progress", 0
                    fp, _, replanned = self.record_failure(run, task, failure, strategy,
                                                           workspace, count=False)
                    if plan.get("auto_replan", False) and not replanned:
                        self.mark_replan(run, task, fp, strategy)
                        self.set_task(plan["id"], task["id"], status="pending", error=failure)
                        if self.replan(plan, run, task, failure):
                            return "superseded", 0
                    self.park(plan["id"], task["id"], failure)
                    self.event(plan["id"], task["id"], "parked_no_progress", failure)
                    return "parked", 0
                self.set_task(plan["id"], task["id"], status="pending", error=failure)
                self.event(plan["id"], task["id"], "check_failed", failure)
                return "progress", 0
        unfinished = self.db.execute("SELECT 1 FROM tasks WHERE run_id=? AND status!='done'", (plan["id"],)).fetchone()
        if deadlines:
            return "waiting", min(deadlines)
        if unfinished and not plan.get("partial_pr", False):
            return "parked", 0
        if git(workspace, "status", "--porcelain"):
            raise RuntimeError("Milestone worktree must be clean before preparing its PR")
        for task in plan["tasks"]:
            if self.db.execute("SELECT status FROM tasks WHERE run_id=? AND id=?", (plan["id"], task["id"])).fetchone()[0] != "done":
                continue
            code, out, err = run_process(task["check"], workspace, plan.get("check_timeout_seconds", 30))
            if code:
                failure = (out + err)[-4000:] or "Check exited " + str(code)
                # The accepted record stays accepted: a successor repairs the
                # cause rather than reopening immutable work.
                if plan.get("auto_replan", False) and self.repair_regression(
                        plan, run, task, workspace, failure):
                    return "superseded", 0
                self.park(plan["id"], task["id"], "Milestone regression: " + failure,
                          "new_evidence")
                return "parked", 0
        self.prepare_pr(plan, run)
        return ("parked", 0) if unfinished else ("ready_for_pr", 0)

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
        rows = self.db.execute("SELECT id,sha FROM tasks WHERE run_id=? AND status='done'", (plan["id"],)).fetchall()
        decisions = self.db.execute(
            "SELECT question,chosen,reason FROM decisions WHERE run_id=? ORDER BY at", (plan["id"],)).fetchall()
        decision_body = ""
        if decisions:
            decision_body = "\n\nDecisions\n\n" + "\n".join(
                "- " + row["question"].replace("\n", " ")[:160] + ": **" + row["chosen"]
                + "** — " + row["reason"].replace("\n", " ")[:240] for row in decisions)
        body = (plan.get("description", plan["id"]) + "\n\nValidation: listed accepted sections passed their configured checks.\n\n"
                + "\n".join("- " + r["id"] + ": `" + r["sha"] + "`" for r in rows)
                + decision_body
                + "\n\nBranch: `" + run["branch"] + "`\n")
        (self.home / (plan["id"]+"-pr.md")).write_text(body)

    def status(self):
        return {table: [dict(r) for r in self.db.execute("SELECT * FROM " + table)]
                for table in ("runs", "tasks", "providers", "decisions")}
