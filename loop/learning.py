"""Incremental learning from structured events, with measured promotion.

`learn` consumes *new* events only, using a durable per-target cursor, and makes
no model calls. Every occurrence it counts is tied to a unique event identity,
so reprocessing the same event — after a crash, or because a cursor was
rewound — can never inflate a lesson's evidence.

Lessons are evidence, never authority. Nothing here can widen an execution
policy, edit a trusted check, or promote itself on its own say-so: a lesson
becomes active only after a trial whose comparison baseline was frozen before
it started, and insufficient evidence always means "no promotion", never
"promote tentatively".
"""
import hashlib
import json
import time

OBSERVED, CANDIDATE, TRIAL, ACTIVE = "observed", "candidate", "trial", "active"
REJECTED, RETIRED, ROLLED_BACK = "rejected", "retired", "rolled_back"

MIN_OCCURRENCES = 3
MIN_OBJECTIVES = 2
TRIAL_ACCEPTANCES = 10
TRIAL_SECONDS = 14 * 86400
MIN_TERMINAL_TASKS = 10
MAX_COMPLETION_DROP = 0.05
MAX_OBSERVATION_SECONDS = 45 * 86400

SIGNAL_KINDS = ("setup_recipe", "failed_strategy", "routing", "planning_context",
                "adapter_defect")


def ensure_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS lessons (
            id TEXT PRIMARY KEY, fingerprint TEXT, signal_kind TEXT, scope TEXT,
            occurrences INTEGER DEFAULT 0, first_seen REAL, last_seen REAL,
            proposed_action TEXT, confidence REAL DEFAULT 0, status TEXT DEFAULT 'observed',
            policy_version TEXT, proposed_plan_id TEXT, baseline_metrics TEXT,
            observed_metrics TEXT, resolution_note TEXT, status_changed_at REAL,
            objectives TEXT DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS lesson_events (
            lesson_id TEXT, event_id TEXT, at REAL, objective_id TEXT,
            PRIMARY KEY(lesson_id,event_id));
        CREATE TABLE IF NOT EXISTS lesson_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lesson_id TEXT, at REAL, run_id TEXT,
            task_id TEXT, outcome TEXT, tokens INTEGER);
    """)


def ensure_cursor_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS learning_cursors (
            source TEXT PRIMARY KEY, position INTEGER DEFAULT 0, updated_at REAL);
    """)


def lesson_id(signal_kind, scope, fingerprint):
    encoded = json.dumps([signal_kind, scope, fingerprint], separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def observe(db, *, signal_kind, scope, fingerprint, event_id, objective_id,
            proposed_action="", now=None):
    """Record one occurrence of a signal, exactly once per event identity.

    Returns the lesson row. An event already attributed to this lesson is a
    no-op: replaying a journal must not manufacture corroboration.
    """
    if signal_kind not in SIGNAL_KINDS:
        raise ValueError("Unknown signal kind: " + signal_kind)
    ensure_schema(db)
    now = time.time() if now is None else now
    identifier = lesson_id(signal_kind, scope, fingerprint)
    db.execute("""INSERT INTO lessons(id,fingerprint,signal_kind,scope,occurrences,first_seen,
        last_seen,proposed_action,status,status_changed_at,objectives)
        VALUES (?,?,?,?,0,?,?,?,?,?,'[]') ON CONFLICT(id) DO NOTHING""",
        (identifier, fingerprint, signal_kind, scope, now, now, proposed_action, OBSERVED, now))
    cursor = db.execute(
        "INSERT OR IGNORE INTO lesson_events(lesson_id,event_id,at,objective_id) VALUES (?,?,?,?)",
        (identifier, str(event_id), now, objective_id))
    if cursor.rowcount:
        objectives = sorted({row["objective_id"] for row in db.execute(
            "SELECT DISTINCT objective_id FROM lesson_events WHERE lesson_id=?", (identifier,))
            if row["objective_id"]})
        db.execute("UPDATE lessons SET occurrences=occurrences+1,last_seen=?,objectives=? WHERE id=?",
                   (now, json.dumps(objectives), identifier))
    return db.execute("SELECT * FROM lessons WHERE id=?", (identifier,)).fetchone()


def promote_candidates(db, now=None):
    """Generalize only from independent corroboration.

    Successor retries of one failure are the same objective, so they cannot
    corroborate each other; the objective count, not the occurrence count, is
    what makes evidence independent.
    """
    ensure_schema(db)
    now = time.time() if now is None else now
    promoted = []
    for row in db.execute("SELECT * FROM lessons WHERE status=?", (OBSERVED,)).fetchall():
        objectives = json.loads(row["objectives"] or "[]")
        if row["occurrences"] < MIN_OCCURRENCES or len(objectives) < MIN_OBJECTIVES:
            continue
        db.execute("UPDATE lessons SET status=?,status_changed_at=?,confidence=? WHERE id=?",
                   (CANDIDATE, now, min(1.0, row["occurrences"] / 10.0), row["id"]))
        promoted.append(row["id"])
    return promoted


def start_trial(db, identifier, baseline_metrics, now=None):
    """Begin a trial against a baseline frozen before the lesson is applied."""
    ensure_schema(db)
    now = time.time() if now is None else now
    row = db.execute("SELECT status FROM lessons WHERE id=?", (identifier,)).fetchone()
    if not row or row["status"] != CANDIDATE:
        return False
    if db.execute("SELECT 1 FROM lessons WHERE status=?", (TRIAL,)).fetchone():
        # One trial at a time: concurrent trials cannot be attributed.
        return False
    db.execute("UPDATE lessons SET status=?,status_changed_at=?,baseline_metrics=? WHERE id=?",
               (TRIAL, now, json.dumps(baseline_metrics, sort_keys=True), identifier))
    return True


def record_application(db, identifier, run_id, task_id, outcome, tokens, now=None):
    """Every assigned task counts — failures, parks and unknown usage included."""
    ensure_schema(db)
    db.execute("INSERT INTO lesson_applications(lesson_id,at,run_id,task_id,outcome,tokens) "
               "VALUES (?,?,?,?,?,?)",
               (identifier, time.time() if now is None else now, run_id, task_id, outcome, tokens))


def trial_metrics(db, identifier):
    """Summarize a trial window. Unknown usage makes accounting incomplete."""
    ensure_schema(db)
    rows = db.execute("SELECT * FROM lesson_applications WHERE lesson_id=?",
                      (identifier,)).fetchall()
    terminal = [row for row in rows if row["outcome"] in ("ok", "parked", "unsuccessful")]
    acceptances = [row for row in terminal if row["outcome"] == "ok"]
    known = [row for row in terminal if isinstance(row["tokens"], int)]
    return {
        "terminal": len(terminal),
        "acceptances": len(acceptances),
        "completion_rate": (len(acceptances) / len(terminal)) if terminal else None,
        "tokens_per_acceptance": (sum(row["tokens"] for row in known) / len(acceptances))
                                 if acceptances and len(known) == len(terminal) else None,
        "accounting_complete": bool(terminal) and len(known) == len(terminal),
    }


def evaluate_trial(db, identifier, now=None):
    """Promote, retire or reject a trial. Insufficient evidence never promotes."""
    ensure_schema(db)
    now = time.time() if now is None else now
    row = db.execute("SELECT * FROM lessons WHERE id=? AND status=?",
                     (identifier, TRIAL)).fetchone()
    if not row:
        return None
    metrics = trial_metrics(db, identifier)
    age = now - (row["status_changed_at"] or now)
    due = metrics["acceptances"] >= TRIAL_ACCEPTANCES or age >= TRIAL_SECONDS
    if not due:
        return None
    def settle(status, note):
        db.execute("UPDATE lessons SET status=?,status_changed_at=?,observed_metrics=?,"
                   "resolution_note=? WHERE id=?",
                   (status, now, json.dumps(metrics, sort_keys=True), note, identifier))
        return status
    if metrics["terminal"] < MIN_TERMINAL_TASKS or not metrics["accounting_complete"]:
        if age >= MAX_OBSERVATION_SECONDS:
            return settle(RETIRED, "Inconclusive at the maximum observation period")
        return settle(RETIRED, "Insufficient comparable evidence") if age >= TRIAL_SECONDS else None
    baseline = json.loads(row["baseline_metrics"] or "{}")
    baseline_tokens = baseline.get("tokens_per_acceptance")
    baseline_completion = baseline.get("completion_rate")
    if (isinstance(baseline_tokens, (int, float))
            and metrics["tokens_per_acceptance"] > baseline_tokens):
        return settle(REJECTED, "Total workload tokens per acceptance increased")
    if (isinstance(baseline_completion, (int, float))
            and metrics["completion_rate"] < baseline_completion - MAX_COMPLETION_DROP):
        return settle(REJECTED, "Completion rate dropped beyond the allowed margin")
    return settle(ACTIVE, "Trial met the declared thresholds")


def disable(db, identifier, note, status=ROLLED_BACK, now=None):
    ensure_schema(db)
    db.execute("UPDATE lessons SET status=?,status_changed_at=?,resolution_note=? WHERE id=?",
               (status, time.time() if now is None else now, note, identifier))


def retrieve(db, *, signal_kind=None, scope=None, limit=5):
    """Return active lessons matching a scope, most corroborated first."""
    ensure_schema(db)
    query = "SELECT * FROM lessons WHERE status=?"
    params = [ACTIVE]
    if signal_kind:
        query += " AND signal_kind=?"
        params.append(signal_kind)
    if scope:
        query += " AND scope=?"
        params.append(scope)
    query += " ORDER BY occurrences DESC, last_seen DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in db.execute(query, params)]


# -- incremental consumption ------------------------------------------------

_SIGNALS = {
    "setup_recipe": ("setup_recipe", "An authorized setup recipe resolved this dependency"),
    "setup_recipe_exhausted": ("setup_recipe", "This recipe does not resolve the dependency"),
    "strategy_changed": ("failed_strategy", "This strategy failed on unchanged evidence"),
    "parked_no_progress": ("failed_strategy", "No configured strategy resolved this failure"),
    "provider_recovered": ("routing", "This provider recovers without another task"),
    "stale_result_rejected": ("adapter_defect", "A worker result failed identity verification"),
    "replan_equivalent": ("planning_context", "Repair proposed no materially different approach"),
}


def learn(engine, registry=None, now=None):
    """Consume new target events into account-global lessons, transactionally.

    The cursor advances in the same target transaction that records what was
    consumed, and each lesson occurrence is keyed by the event's identity, so a
    crash between the two can only ever cause a harmless replay.
    """
    now = time.time() if now is None else now
    registry = registry or engine.registry
    ensure_schema(registry.db)
    ensure_cursor_schema(engine.db)
    row = engine.db.execute(
        "SELECT position FROM learning_cursors WHERE source='events'").fetchone()
    position = row["position"] if row else 0
    events = engine.db.execute(
        "SELECT rowid,* FROM events WHERE rowid>? ORDER BY rowid", (position,)).fetchall()
    consumed = position
    for event in events:
        consumed = event["rowid"]
        signal = _SIGNALS.get(event["kind"])
        if not signal:
            continue
        signal_kind, action = signal
        objective = engine.db.execute(
            "SELECT objective_id FROM runs WHERE id=?", (event["run_id"],)).fetchone()
        observe(registry.db,
                signal_kind=signal_kind,
                scope=json.dumps({"target": str(engine.repo), "kind": event["kind"]},
                                 sort_keys=True),
                fingerprint=hashlib.sha256(
                    (event["kind"] + "\n" + str(event["detail"])[:600]).encode()).hexdigest()[:16],
                event_id=str(engine.repo) + ":" + str(event["rowid"]),
                objective_id=(objective["objective_id"] if objective else event["run_id"]),
                proposed_action=action, now=now)
    promote_candidates(registry.db, now)
    engine.db.execute(
        "INSERT INTO learning_cursors(source,position,updated_at) VALUES ('events',?,?) "
        "ON CONFLICT(source) DO UPDATE SET position=excluded.position,updated_at=excluded.updated_at",
        (consumed, now))
    engine.db.commit()
    return {"consumed_through": consumed, "events": len(events)}
