"""Measured provider health, deterministic routing, and bounded tuning.

Three rules shape this module:

* **Raw samples, not summaries.** Storing a lifetime count and a single stored
  p90 makes it impossible to recompute a quantile later or to age old evidence
  out. Every observation is kept as a row, and the aggregates are derived.
* **A timeout is a censored observation.** If a worker is killed at 180s, the
  true duration is *at least* 180s. Recording it as exactly 180s trains the
  timeout downward until everything times out, so censored samples raise a
  learned timeout and never lower it.
* **Missing telemetry is unknown, not good.** An unmeasured provider is never
  preferred on the strength of having no failures; it is eligible only for a
  bounded probe when policy allows one.

Nothing here calls a model.
"""
import json
import time

COLD_START_SAMPLES = 5
DEFAULT_PROBE_SECONDS = 900
MIN_PROBE_SECONDS = 60
MAX_PROBE_SECONDS = 21600
MAX_TUNING_DELTA = 0.25
AGE_HALF_LIFE_SECONDS = 14 * 86400


def ensure_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
            at REAL NOT NULL, provider TEXT, account TEXT, bucket TEXT, model TEXT,
            effort TEXT, task_class TEXT, kind TEXT, outcome TEXT, tokens INTEGER,
            duration REAL, censored INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS samples_route ON samples(provider,model,effort,task_class,at);
        CREATE TABLE IF NOT EXISTS recoveries (
            at REAL, provider TEXT, account TEXT, kind TEXT, waited REAL);
        CREATE TABLE IF NOT EXISTS settings (
            name TEXT PRIMARY KEY, value REAL, baseline REAL, changed_at REAL,
            observations INTEGER DEFAULT 0);
    """)


def record_sample(db, *, provider, account="default", bucket="codex", model=None, effort="low",
                  task_class="", kind="implement", outcome="ok", tokens=None, duration=None,
                  censored=False, now=None):
    ensure_schema(db)
    db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (time.time() if now is None else now, provider, account, bucket, model, effort,
                task_class, kind, outcome, tokens, duration, 1 if censored else 0))


def record_recovery(db, provider, account, kind, waited, now=None):
    """How long a provider actually took to become usable again."""
    ensure_schema(db)
    db.execute("INSERT INTO recoveries VALUES (?,?,?,?,?)",
               (time.time() if now is None else now, provider, account, kind, waited))


def _weight(age):
    return 0.5 ** (max(0.0, age) / AGE_HALF_LIFE_SECONDS)


def route_statistics(db, provider, model, effort, task_class, now=None):
    """Age-decayed success rate and expected tokens to acceptance for one route."""
    ensure_schema(db)
    now = time.time() if now is None else now
    rows = db.execute("""SELECT at,outcome,tokens FROM samples
        WHERE provider=? AND model IS ? AND effort=? AND task_class=? AND kind='implement'""",
        (provider, model, effort, task_class)).fetchall()
    if not rows:
        return {"samples": 0, "success_rate": None, "tokens_per_acceptance": None}
    weighted = accepted = tokens = known = 0.0
    for row in rows:
        weight = _weight(now - row["at"])
        weighted += weight
        if row["outcome"] == "ok":
            accepted += weight
        if isinstance(row["tokens"], int):
            tokens += weight * row["tokens"]
            known += weight
    return {
        "samples": len(rows),
        "success_rate": (accepted / weighted) if weighted else None,
        # Tokens spent per acceptance, including the failures on the way there.
        "tokens_per_acceptance": ((tokens / accepted) if accepted and known else None),
    }


def choose_route(db, models, task_class, now=None, minimum_success=0.3):
    """Pick a configured model, preferring measured efficiency over availability.

    Returns ``(model, reason)``. Plentiful quota does not make a model
    efficient, so quota only constrains eligibility; it never ranks.
    """
    if not models:
        return None, "no configured models"
    scored, unmeasured = [], []
    for model in models:
        stats = route_statistics(db, model["provider"], model.get("model"),
                                 model.get("effort", "low"), task_class, now)
        if stats["samples"] < COLD_START_SAMPLES:
            unmeasured.append(model)
            continue
        if stats["success_rate"] is not None and stats["success_rate"] < minimum_success:
            continue
        if stats["tokens_per_acceptance"] is None:
            unmeasured.append(model)
            continue
        scored.append((stats["tokens_per_acceptance"], models.index(model), model, stats))
    if scored:
        scored.sort()
        cost, _, model, stats = scored[0]
        return model, ("lowest measured tokens per acceptance: %d over %d samples"
                       % (cost, stats["samples"]))
    if unmeasured:
        # Cold start: the cheapest configured option, in declared order.
        return unmeasured[0], "cold start: no comparable measurements yet"
    return models[0], "all measured routes fell below the success floor"


def learned_interval(db, provider, account, kind, default=DEFAULT_PROBE_SECONDS, now=None):
    """Learn a probe delay from observed recovery times, with floors and ceilings."""
    ensure_schema(db)
    rows = db.execute(
        "SELECT waited FROM recoveries WHERE provider=? AND account=? AND kind=? "
        "ORDER BY at DESC LIMIT 20", (provider, account, kind)).fetchall()
    waits = sorted(row["waited"] for row in rows if isinstance(row["waited"], (int, float)))
    if len(waits) < 3:
        return default
    median = waits[len(waits) // 2]
    return int(max(MIN_PROBE_SECONDS, min(MAX_PROBE_SECONDS, median)))


def learned_timeout(db, provider, model, effort, task_class, default, now=None):
    """A worker timeout from observed durations, raised by censored observations."""
    ensure_schema(db)
    rows = db.execute("""SELECT duration,censored FROM samples
        WHERE provider=? AND model IS ? AND effort=? AND task_class=? AND duration IS NOT NULL
        ORDER BY at DESC LIMIT 50""", (provider, model, effort, task_class)).fetchall()
    if len(rows) < COLD_START_SAMPLES:
        return default
    durations = sorted(row["duration"] for row in rows)
    quantile = durations[min(len(durations) - 1, int(0.9 * len(durations)))]
    proposed = quantile * 1.5
    if any(row["censored"] for row in rows):
        # Censored samples only tell us the true duration is longer, so a
        # timeout may grow from them but must never shrink.
        proposed = max(proposed, default)
    return int(max(default * (1 - MAX_TUNING_DELTA), min(default * (1 + MAX_TUNING_DELTA),
                                                         proposed)))


def apply_setting(db, name, proposed, baseline, now=None):
    """Move one tuned setting toward a proposal, bounded per observation period.

    The baseline is retained so a degraded setting can be reverted rather than
    being treated as the new normal.
    """
    ensure_schema(db)
    now = time.time() if now is None else now
    row = db.execute("SELECT * FROM settings WHERE name=?", (name,)).fetchone()
    current = row["value"] if row else baseline
    limit = abs(baseline) * MAX_TUNING_DELTA
    bounded = max(current - limit, min(current + limit, proposed))
    db.execute("INSERT INTO settings(name,value,baseline,changed_at,observations) "
               "VALUES (?,?,?,?,1) ON CONFLICT(name) DO UPDATE SET value=excluded.value,"
               "changed_at=excluded.changed_at,observations=settings.observations+1",
               (name, bounded, baseline, now))
    return bounded


def revert_setting(db, name):
    """Return a setting to its frozen baseline after a measured regression."""
    ensure_schema(db)
    row = db.execute("SELECT baseline FROM settings WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    db.execute("UPDATE settings SET value=baseline,changed_at=?,observations=0 WHERE name=?",
               (time.time(), name))
    return row["baseline"]


def setting(db, name, default):
    ensure_schema(db)
    row = db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
    return row["value"] if row else default


def task_class(task):
    """A coarse comparison class: routes are compared within similar work."""
    files = task.get("files", [])
    suffixes = sorted({("." + name.rsplit(".", 1)[-1]) if "." in name else "" for name in files})
    size = "single" if len(files) <= 1 else "multi"
    return json.dumps([size, suffixes], separators=(",", ":"))
