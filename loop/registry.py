"""Account-global admission control shared by every target repository.

Target-local state (plans, tasks, results) stays in each repository's
``.agent-loop/state.sqlite``.  Allowance is an *account* property: two targets
driving the same provider subscription must not both spend the same tokens, so
reservations live in one shared database, by default
``~/.agent-loop/registry.sqlite``.

Two modes:

* **Monitoring** (no configured cap) is the default.  Reservations are still
  written so usage is attributable, but nothing is ever refused.  A registry
  failure degrades to a recorded warning; it must never stop a supervisor from
  reaping workers or accepting finished work.
* **Capped** (an explicit ``limits`` policy).  Admission happens inside one
  ``BEGIN IMMEDIATE`` transaction keyed by provider, account and quota bucket,
  so concurrent targets serialize on the same allowance.  A registry failure
  under a cap refuses admission rather than silently reopening allowance.

An unreconciled reservation keeps holding its conservative allowance.  That is
deliberate: a crashed attempt whose real usage is unknown must not be reported
as zero tokens, and its allowance must not be handed to someone else until the
usage is actually recovered.
"""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

# A whole-invocation allowance: input, output and CLI tool turns. It is a
# conservative admission unit, not a claim about the exact token count a
# provider will bill.
DEFAULT_RESERVATION_TOKENS = 60000


class CapReached(Exception):
    """A configured lifetime limit is spent; no deadline will reopen it."""

    def __init__(self, scope, message):
        super().__init__(message)
        self.scope = scope


class CapWait(Exception):
    """A configured rolling limit is spent until ``deadline``."""

    def __init__(self, scope, deadline, message):
        super().__init__(message)
        self.scope = scope
        self.deadline = deadline


def registry_path():
    override = os.environ.get("AGENT_LOOP_REGISTRY")
    return Path(override) if override else Path.home() / ".agent-loop" / "registry.sqlite"


def account_identity(provider):
    """Name the account without reading or storing any credential."""
    return os.environ.get("AGENT_LOOP_ACCOUNT_" + provider.upper(),
                          os.environ.get("AGENT_LOOP_ACCOUNT", "default"))


def normalize_limits(limits):
    """Validate an optional ``limits`` policy; omission means monitoring mode."""
    if limits is None:
        return {}
    if not isinstance(limits, dict):
        raise ValueError("policy.limits must be an object")
    result = {}
    for scope, value in sorted(limits.items()):
        if scope not in ("account", "objective") and not scope.startswith("provider:"):
            raise ValueError("Unknown limit scope: " + scope)
        if not isinstance(value, dict):
            raise ValueError("Limit scope must be an object: " + scope)
        entry = {}
        rolling = value.get("rolling")
        if rolling is not None:
            if (not isinstance(rolling, dict)
                    or not isinstance(rolling.get("tokens"), int) or rolling["tokens"] <= 0
                    or not isinstance(rolling.get("window_seconds"), int)
                    or rolling["window_seconds"] <= 0):
                raise ValueError("rolling limit needs positive tokens and window_seconds")
            entry["rolling"] = {"tokens": rolling["tokens"],
                                "window_seconds": rolling["window_seconds"]}
        lifetime = value.get("lifetime_tokens")
        if lifetime is not None:
            if not isinstance(lifetime, int) or lifetime <= 0:
                raise ValueError("lifetime_tokens must be a positive integer")
            entry["lifetime_tokens"] = lifetime
        if entry:
            result[scope] = entry
    return result


def limits_narrow(candidate, authority):
    """A successor may keep or tighten a cap; it may never loosen or drop one."""
    for scope, entry in authority.items():
        mine = candidate.get(scope)
        if not mine:
            return False
        if "lifetime_tokens" in entry and mine.get("lifetime_tokens", float("inf")) > entry["lifetime_tokens"]:
            return False
        if "rolling" in entry:
            rolling = mine.get("rolling")
            if not rolling or rolling["tokens"] > entry["rolling"]["tokens"]:
                return False
            if rolling["window_seconds"] < entry["rolling"]["window_seconds"]:
                return False
    return True


class Registry:
    def __init__(self, path=None):
        self.path = Path(path) if path else registry_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=30000;
            CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY, at REAL NOT NULL, provider TEXT NOT NULL,
                account TEXT NOT NULL, bucket TEXT NOT NULL, objective_id TEXT,
                run_id TEXT, task_id TEXT, kind TEXT, reserved INTEGER NOT NULL,
                actual INTEGER, state TEXT NOT NULL, settled_at REAL);
            CREATE INDEX IF NOT EXISTS reservations_window
                ON reservations(provider,account,bucket,at);
            CREATE INDEX IF NOT EXISTS reservations_objective
                ON reservations(objective_id,at);
            CREATE TABLE IF NOT EXISTS holds (
                provider TEXT, account TEXT, bucket TEXT, retry_at REAL, reason TEXT,
                PRIMARY KEY(provider,account,bucket));
        """)

    def close(self):
        self.db.close()

    # -- admission ---------------------------------------------------------

    def _charged(self, where, params, since=None):
        """Charge unreconciled reservations at their conservative allowance."""
        clause = where + (" AND at>=?" if since is not None else "")
        args = (*params, since) if since is not None else params
        row = self.db.execute(
            "SELECT COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE actual END),0)"
            " AS total FROM reservations WHERE state!='void' AND " + clause, args).fetchone()
        return row["total"]

    def reserve(self, *, provider, account, bucket, objective_id, run_id, task_id, kind,
                limits, tokens=DEFAULT_RESERVATION_TOKENS, now=None):
        """Atomically admit one invocation, or raise CapWait/CapReached.

        Returns the reservation id.  With no configured cap this only records
        attribution, but it still runs in the shared transaction so that a
        later-configured cap sees the complete history.
        """
        now = time.time() if now is None else now
        limits = limits or {}
        provider_scope = "provider:" + provider
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for scope in ("account", provider_scope, "objective"):
                entry = limits.get(scope)
                if not entry:
                    continue
                if scope == "account":
                    where, params = "account=?", (account,)
                elif scope == "objective":
                    if not objective_id:
                        continue
                    where, params = "objective_id=?", (objective_id,)
                else:
                    where, params = ("provider=? AND account=? AND bucket=?",
                                     (provider, account, bucket))
                lifetime = entry.get("lifetime_tokens")
                if lifetime is not None and self._charged(where, params) + tokens > lifetime:
                    raise CapReached(scope, "Configured lifetime limit reached for " + scope)
                rolling = entry.get("rolling")
                if rolling:
                    window = now - rolling["window_seconds"]
                    if self._charged(where, params, window) + tokens > rolling["tokens"]:
                        oldest = self.db.execute(
                            "SELECT MIN(at) AS at FROM reservations WHERE state!='void' AND at>=? AND "
                            + where, (window, *params)).fetchone()["at"]
                        deadline = (oldest or now) + rolling["window_seconds"]
                        raise CapWait(scope, deadline,
                                      "Configured rolling limit reached for " + scope)
            reservation_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO reservations(id,at,provider,account,bucket,objective_id,run_id,"
                "task_id,kind,reserved,actual,state) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'reserved')",
                (reservation_id, now, provider, account, bucket, objective_id, run_id,
                 task_id, kind, tokens))
            self.db.execute("COMMIT")
            return reservation_id
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def reconcile(self, reservation_id, actual, now=None):
        """Settle one reservation exactly once. Returns True the first time."""
        if actual is None:
            return False
        cursor = self.db.execute(
            "UPDATE reservations SET actual=?,state='reconciled',settled_at=? "
            "WHERE id=? AND state='reserved'",
            (int(actual), time.time() if now is None else now, reservation_id))
        return cursor.rowcount == 1

    def void(self, reservation_id):
        """Release allowance for an invocation that provably never happened."""
        cursor = self.db.execute(
            "UPDATE reservations SET state='void',settled_at=? WHERE id=? AND state='reserved'",
            (time.time(), reservation_id))
        return cursor.rowcount == 1

    def unreconciled(self, objective_id=None):
        if objective_id:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM reservations WHERE state='reserved' AND objective_id=?",
                (objective_id,))]
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM reservations WHERE state='reserved'")]

    def hold(self, provider, account, bucket, retry_at, reason):
        self.db.execute(
            "INSERT INTO holds(provider,account,bucket,retry_at,reason) VALUES (?,?,?,?,?) "
            "ON CONFLICT(provider,account,bucket) DO UPDATE SET retry_at=excluded.retry_at,"
            "reason=excluded.reason", (provider, account, bucket, retry_at, reason))

    def held_until(self, provider, account, bucket):
        row = self.db.execute(
            "SELECT retry_at FROM holds WHERE provider=? AND account=? AND bucket=?",
            (provider, account, bucket)).fetchone()
        return row["retry_at"] if row else 0


class Outbox:
    """Durable local queue for accounting the shared registry could not take.

    Reaping a worker, validating its change and committing accepted work must
    keep working while the registry is unavailable; the accounting is replayed
    afterwards rather than dropped.
    """

    def __init__(self, directory):
        self.directory = Path(directory)

    def add(self, entry):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (uuid.uuid4().hex + ".json")
        from .storage import atomic_json
        atomic_json(path, entry)
        return path

    def flush(self, registry):
        """Replay queued reconciliations; drop only what the registry accepted."""
        if not self.directory.exists():
            return 0
        replayed = 0
        for path in sorted(self.directory.glob("*.json")):
            try:
                entry = json.loads(path.read_text())
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            try:
                if entry.get("op") == "reconcile":
                    registry.reconcile(entry["reservation_id"], entry.get("actual"))
                elif entry.get("op") == "void":
                    registry.void(entry["reservation_id"])
            except sqlite3.Error:
                return replayed
            path.unlink(missing_ok=True)
            replayed += 1
        return replayed
