"""Durable objective queue and the deterministic service tick.

The service owns *what to work on next*; the engine owns *how one milestone
advances*. Neither consults a model to make that choice: selecting work,
sleeping, backing off after a crash, and reconciling a release are all ordinary
code, so an idle service costs nothing and survives a provider outage.

A parked or unsuccessful objective is never hidden by a healthy service: it
keeps its own terminal state and its own reason while other objectives run.
"""
import json
import os
import time
from pathlib import Path

from .engine import Engine
from .storage import atomic_json

TERMINAL = ("merged", "published", "locally_complete", "unsuccessful")
IDLE_SLEEP_SECONDS = 60
LEARN_INTERVAL_SECONDS = 900
MAX_BACKOFF_SECONDS = 900


def ensure_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS objectives (
            id TEXT PRIMARY KEY, at REAL, plan_path TEXT, plan_id TEXT, run_id TEXT,
            status TEXT DEFAULT 'queued', priority INTEGER DEFAULT 100, wake_at REAL DEFAULT 0,
            reason TEXT DEFAULT '', release TEXT, attempts INTEGER DEFAULT 0,
            updated_at REAL);
        CREATE TABLE IF NOT EXISTS releases (
            run_id TEXT PRIMARY KEY, repository TEXT, base TEXT, state TEXT,
            pr_number INTEGER, url TEXT, head_sha TEXT, expected_sha TEXT,
            deadline REAL, updated_at REAL);
    """)


class Service:
    """One installation's queue across every target repository it drives."""

    def __init__(self, engine):
        self.engine = engine
        ensure_schema(engine.db)
        engine.db.commit()
        self.last_learn = 0.0

    # -- queue -------------------------------------------------------------

    def enqueue(self, plan_path, release=None, priority=100, objective_id=None):
        plan = json.loads(Path(plan_path).read_text())
        objective_id = objective_id or plan.get("objective_id", plan["id"])
        self.engine.db.execute(
            "INSERT INTO objectives(id,at,plan_path,plan_id,status,priority,updated_at) "
            "VALUES (?,?,?,?,'queued',?,?) ON CONFLICT(id) DO UPDATE SET "
            "plan_path=excluded.plan_path,priority=excluded.priority",
            (objective_id, time.time(), str(Path(plan_path).resolve()), plan["id"],
             priority, time.time()))
        if release:
            self.engine.db.execute("UPDATE objectives SET release=? WHERE id=?",
                                   (json.dumps(release, sort_keys=True), objective_id))
        self.engine.db.commit()
        return objective_id

    def objectives(self):
        return [dict(row) for row in self.engine.db.execute(
            "SELECT * FROM objectives ORDER BY priority, at")]

    def eligible(self, now):
        """Fair selection: ready work, oldest first within a priority."""
        return self.engine.db.execute(
            "SELECT * FROM objectives WHERE status NOT IN "
            "('merged','published','locally_complete','unsuccessful','parked') "
            "AND wake_at<=? ORDER BY priority, at LIMIT 1", (now,)).fetchone()

    def set_objective(self, objective_id, **fields):
        fields["updated_at"] = time.time()
        self.engine.db.execute(
            "UPDATE objectives SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?",
            (*fields.values(), objective_id))
        self.engine.db.commit()

    # -- release -----------------------------------------------------------

    def record_release(self, run_id, repository, base, state, result, expected_sha=""):
        """Persist release identity so a retry reuses the PR instead of racing it."""
        self.engine.db.execute(
            "INSERT INTO releases(run_id,repository,base,state,pr_number,url,head_sha,"
            "expected_sha,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,pr_number=excluded.pr_number,"
            "url=excluded.url,head_sha=excluded.head_sha,expected_sha=excluded.expected_sha,"
            "updated_at=excluded.updated_at",
            (run_id, repository, base, state, result.get("number"), result.get("url"),
             result.get("headRefOid", ""), expected_sha, time.time()))
        self.engine.db.commit()

    def release_state(self, run_id):
        row = self.engine.db.execute("SELECT * FROM releases WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def invalidate_on_head_change(self, run_id, head_sha):
        """A changed head invalidates earlier approvals and check results."""
        row = self.release_state(run_id)
        if not row or row["head_sha"] == head_sha:
            return False
        self.engine.db.execute(
            "UPDATE releases SET state='pending',head_sha=?,updated_at=? WHERE run_id=?",
            (head_sha, time.time(), run_id))
        self.engine.db.commit()
        self.engine.event(run_id, "release", "head_changed", head_sha)
        return True

    def publish_objective(self, objective, plan_id):
        """Publish under the objective's configured release policy, or skip."""
        if not objective.get("release"):
            return "locally_complete", "No release destination is configured"
        settings = json.loads(objective["release"])
        from .release import publish
        result = publish(self.engine, plan_id, settings["repository"],
                         settings.get("base", "main"), settings.get("merge", False),
                         settings.get("review", False))
        self.record_release(plan_id, settings["repository"], settings.get("base", "main"),
                            result["state"], result)
        self.engine.event(plan_id, "release", result["state"], result)
        if result["state"] == "merged":
            return "merged", result.get("url", "")
        if result["state"] in ("pr_open", "waiting_for_checks", "merge_pending"):
            # A published PR is not a completed deployment.
            return "published", result.get("url", "")
        return "running", result["state"]

    # -- tick --------------------------------------------------------------

    def maybe_learn(self, now):
        """Low-frequency, deterministic learning. It makes no model calls.

        Scheduled here rather than left to an operator reading a report: a
        lesson nobody consumes changes nothing.
        """
        if now - self.last_learn < LEARN_INTERVAL_SECONDS:
            return None
        self.last_learn = now
        from .learning import learn
        try:
            return learn(self.engine, now=now)
        except Exception as exc:  # learning must never stop productive work
            self.engine.event(None, None, "learning_failed", str(exc))
            return None

    def tick(self, now=None):
        """Advance at most one objective. Returns ``(state, sleep_deadline)``."""
        now = time.time() if now is None else now
        self.maybe_learn(now)
        objective = self.eligible(now)
        if not objective:
            return "idle", now + IDLE_SLEEP_SECONDS
        plan = json.loads(Path(objective["plan_path"]).read_text())
        objective_id = objective["id"]
        try:
            with self.engine.run_lock(plan["id"]):
                state, deadline = self.engine.tick(plan)
        except RuntimeError as exc:
            # Another supervisor owns this milestone, or the worktree is
            # unusable. Back off; do not spin and do not lose the objective.
            backoff = min(MAX_BACKOFF_SECONDS, 30 * 2 ** min(8, objective["attempts"]))
            self.set_objective(objective_id, status="waiting", wake_at=now + backoff,
                               reason=str(exc), attempts=objective["attempts"] + 1)
            return "waiting", now + backoff
        if state == "superseded":
            row = self.engine.db.execute(
                "SELECT id,plan FROM runs WHERE parent_id=? ORDER BY rowid DESC LIMIT 1",
                (plan["id"],)).fetchone()
            successor = json.loads(row["plan"])
            path = Path(objective["plan_path"]).with_name(successor["id"] + ".json")
            atomic_json(path, successor)
            self.set_objective(objective_id, plan_path=str(path), plan_id=successor["id"],
                               status="running", reason="Superseded by " + successor["id"])
            return "superseded", now
        if state == "ready_for_pr":
            status, reason = self.publish_objective(dict(objective), plan["id"])
            self.set_objective(objective_id, status=status, reason=reason, run_id=plan["id"])
            return status, now
        if state == "waiting":
            self.set_objective(objective_id, status="waiting", wake_at=deadline,
                               run_id=plan["id"])
            return "waiting", deadline
        if state in ("parked", "blocked"):
            # Visibly unsuccessful, and it does not stop the other objectives.
            self.set_objective(objective_id, status="parked", run_id=plan["id"],
                               reason="Milestone " + state)
            return state, now
        self.set_objective(objective_id, status="running", run_id=plan["id"],
                           attempts=0, reason="")
        return "progress", now

    def run_forever(self, stop=None, sleep=time.sleep, now=lambda: time.time()):
        """Deterministic service loop: no inference, and sleep when idle."""
        heartbeat = self.engine.home / "service-heartbeat.json"
        while not (stop and stop()):
            moment = now()
            atomic_json(heartbeat, {"at": moment, "pid": os.getpid()})
            state, deadline = self.tick(moment)
            if stop and stop():
                return state
            delay = max(0.0, min(deadline - now(), IDLE_SLEEP_SECONDS))
            if delay:
                sleep(delay)
        return "stopped"


LAUNCHD_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string><string>loop</string>
    <string>--repo</string><string>{repo}</string>
    <string>service</string>
  </array>
  <key>WorkingDirectory</key><string>{installation}</string>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{logs}/service.out.log</string>
  <key>StandardErrorPath</key><string>{logs}/service.err.log</string>
</dict>
</plist>
"""


def launchd_plist(repo, label="com.agent-loop.service", python=None, installation=None):
    """Render a KeepAlive service definition. Installing or starting it is separate."""
    import sys
    repo = Path(repo).resolve()
    return LAUNCHD_TEMPLATE.format(
        label=label, python=python or sys.executable, repo=repo,
        installation=installation or str(Path(__file__).resolve().parent.parent),
        logs=repo / ".agent-loop")
