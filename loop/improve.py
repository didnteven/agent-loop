"""Self-improvement of the supervisor itself, gated by independent evidence.

The supervisor may propose changes to its own code, but it may not judge them.
Three separations make that safe:

* **Fenced surfaces.** The enforcement code that decides what a worker may do —
  path safety, change validation, managed history, plan and policy validation,
  release policy, accounting, and the independent test harness — is not writable
  by an improvement worker. This is enforced here in code, not only in a prompt.
* **Pinned evaluation.** A candidate is judged by a fixture manifest that lives
  outside every worker write root and is pinned by digest. Tests the candidate
  wrote itself may supplement that evidence; they can never constitute it.
* **Versioned installations.** A candidate is a separate checkout. Runs are
  assigned to a version and finish on it, so a rollback routes *new* work to the
  retained baseline instead of rewriting code that is currently executing or
  deleting work in a target repository.

Missing evidence is an ineligible candidate, never a successful deployment.
"""
import hashlib
import json
import subprocess
import time
from pathlib import Path

# Enforcement and accounting surfaces an improvement worker may never edit.
FENCED_PATHS = (
    "loop/policy.py",
    "loop/registry.py",
    "loop/release.py",
    "tests/test_loop.py",
    "tests/fixtures/manifest.json",
)
# Functions inside otherwise-editable files that must not change without a
# deliberate human decision; the fence is enforced by comparing their source.
FENCED_FUNCTIONS = (
    ("loop/engine.py", "safe_path"),
    ("loop/engine.py", "validate_worker_changes"),
    ("loop/engine.py", "validate_managed_history"),
    ("loop/engine.py", "validate_plan"),
)


def ensure_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS installations (
            version TEXT PRIMARY KEY, path TEXT, sha TEXT, role TEXT, created_at REAL,
            evidence TEXT, status TEXT DEFAULT 'built');
        CREATE TABLE IF NOT EXISTS version_assignments (
            run_id TEXT PRIMARY KEY, version TEXT, assigned_at REAL);
        CREATE TABLE IF NOT EXISTS improvement_trials (
            id TEXT PRIMARY KEY, lesson_id TEXT, version TEXT, started_at REAL,
            status TEXT, note TEXT);
    """)


def function_source(path, name):
    """Extract one function's source for fence comparison, without importing it."""
    lines = Path(path).read_text().splitlines()
    collected, capturing = [], False
    for line in lines:
        if line.startswith("def " + name + "(") or line.startswith("    def " + name + "("):
            capturing = True
            indent = len(line) - len(line.lstrip())
            collected.append(line)
            continue
        if capturing:
            if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                break
            collected.append(line)
    return "\n".join(collected)


def fence_digest(root):
    """A digest over every fenced surface of one installation."""
    root = Path(root)
    parts = []
    for name in FENCED_PATHS:
        path = root / name
        parts.append(name + ":" + (hashlib.sha256(path.read_bytes()).hexdigest()
                                   if path.exists() else "absent"))
    for name, function in FENCED_FUNCTIONS:
        path = root / name
        source = function_source(path, function) if path.exists() else ""
        parts.append(name + "::" + function + ":"
                     + hashlib.sha256(source.encode()).hexdigest())
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def fenced_violations(candidate_root, baseline_root):
    """Name every fenced surface a candidate changed."""
    violations = []
    for name in FENCED_PATHS:
        candidate, baseline = Path(candidate_root) / name, Path(baseline_root) / name
        if candidate.exists() != baseline.exists():
            violations.append(name)
        elif candidate.exists() and candidate.read_bytes() != baseline.read_bytes():
            violations.append(name)
    for name, function in FENCED_FUNCTIONS:
        if (function_source(Path(candidate_root) / name, function)
                != function_source(Path(baseline_root) / name, function)):
            violations.append(name + "::" + function)
    return violations


def improvement_allowlist(root):
    """Files an improvement worker may edit: never a fenced surface."""
    root = Path(root)
    allowed = []
    for path in sorted((root / "loop").glob("*.py")):
        name = "loop/" + path.name
        if name not in FENCED_PATHS:
            allowed.append(name)
    return allowed


def propose(engine, registry=None, now=None):
    """Generate at most one improvement trial from corroborated evidence.

    Prefer a configuration or context lesson where one exists: changing code is
    the expensive answer and is only justified when nothing cheaper resolves the
    recurring evidence.
    """
    from . import learning
    now = time.time() if now is None else now
    registry = registry or engine.registry
    ensure_schema(registry.db)
    learning.ensure_schema(registry.db)
    if registry.db.execute(
            "SELECT 1 FROM improvement_trials WHERE status IN ('proposed','running')").fetchone():
        return None, "an improvement trial is already in flight"
    candidates = registry.db.execute(
        "SELECT * FROM lessons WHERE status IN (?,?) AND signal_kind IN "
        "('adapter_defect','failed_strategy') ORDER BY occurrences DESC",
        (learning.CANDIDATE, learning.ACTIVE)).fetchall()
    for lesson in candidates:
        cheaper = registry.db.execute(
            "SELECT 1 FROM lessons WHERE fingerprint=? AND signal_kind IN "
            "('setup_recipe','routing','planning_context') AND status=?",
            (lesson["fingerprint"], learning.ACTIVE)).fetchone()
        if cheaper:
            continue
        if registry.db.execute("SELECT 1 FROM improvement_trials WHERE lesson_id=?",
                               (lesson["id"],)).fetchone():
            continue  # deduplicated: the same proposal is not raised twice
        identifier = hashlib.sha256((lesson["id"] + str(now)).encode()).hexdigest()[:16]
        registry.db.execute(
            "INSERT INTO improvement_trials(id,lesson_id,version,started_at,status,note) "
            "VALUES (?,?,'',?,'proposed',?)",
            (identifier, lesson["id"], now, lesson["proposed_action"] or ""))
        return identifier, lesson["id"]
    return None, "no corroborated evidence needs a code change"


def build_installation(engine, sha, version, installation_root=None, registry=None):
    """Check out a pinned, separate installation; never rewrite the running one."""
    registry = registry or engine.registry
    ensure_schema(registry.db)
    source = Path(__file__).resolve().parent.parent
    root = Path(installation_root or (Path.home() / ".agent-loop" / "installations"))
    target = root / version
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        subprocess.check_output(["git", "-C", str(source), "worktree", "add", "--detach",
                                 str(target), sha], text=True, stderr=subprocess.STDOUT)
    registry.db.execute(
        "INSERT INTO installations(version,path,sha,role,created_at,status) "
        "VALUES (?,?,?,'candidate',?, 'built') ON CONFLICT(version) DO UPDATE SET path=excluded.path",
        (version, str(target), sha, time.time()))
    return target


def load_manifest(root):
    """Read the pinned fixture manifest that judges a candidate."""
    path = Path(root) / "tests" / "fixtures" / "manifest.json"
    if not path.exists():
        return None
    manifest = json.loads(path.read_text())
    if not isinstance(manifest.get("fixtures"), list) or not manifest["fixtures"]:
        return None
    if not isinstance(manifest.get("thresholds"), dict):
        return None
    return manifest


def run_fixtures(root, manifest, timeout=900):
    """Run the independent fixtures from the baseline's pinned manifest."""
    from .adapters import run_process
    results = []
    for fixture in manifest["fixtures"]:
        code, out, err = run_process(fixture["argv"], root, fixture.get("timeout_seconds", timeout))
        results.append({"id": fixture["id"], "passed": code == 0,
                        "output": (out + err)[-2000:]})
    return results


def evaluate(engine, candidate_root, baseline_root, registry=None, timeout=900):
    """Compare a candidate with the pinned baseline. Missing evidence is ineligible."""
    registry = registry or engine.registry
    ensure_schema(registry.db)
    manifest = load_manifest(baseline_root)
    if not manifest:
        return {"eligible": False, "reason": "no pinned independent fixture manifest"}
    violations = fenced_violations(candidate_root, baseline_root)
    if violations:
        return {"eligible": False, "reason": "candidate changed fenced surfaces: "
                + ", ".join(violations)}
    baseline = run_fixtures(baseline_root, manifest, timeout)
    candidate = run_fixtures(candidate_root, manifest, timeout)
    regressions = [entry["id"] for entry, before in zip(candidate, baseline)
                   if before["passed"] and not entry["passed"]]
    if regressions:
        return {"eligible": False, "reason": "regressed fixtures: " + ", ".join(regressions),
                "candidate": candidate, "baseline": baseline}
    if not all(entry["passed"] for entry in candidate):
        return {"eligible": False, "reason": "candidate does not pass the pinned fixtures",
                "candidate": candidate, "baseline": baseline}
    return {"eligible": True, "reason": "candidate passed every pinned fixture",
            "candidate": candidate, "baseline": baseline}


def assign_version(engine, run_id, version, registry=None):
    """Pin one run to a supervisor version; existing runs finish on theirs."""
    registry = registry or engine.registry
    ensure_schema(registry.db)
    registry.db.execute(
        "INSERT INTO version_assignments(run_id,version,assigned_at) VALUES (?,?,?) "
        "ON CONFLICT(run_id) DO NOTHING", (run_id, version, time.time()))
    return registry.db.execute("SELECT version FROM version_assignments WHERE run_id=?",
                               (run_id,)).fetchone()["version"]


def active_version(engine, registry=None, default="baseline"):
    registry = registry or engine.registry
    ensure_schema(registry.db)
    row = registry.db.execute(
        "SELECT version FROM installations WHERE role='active' ORDER BY created_at DESC "
        "LIMIT 1").fetchone()
    return row["version"] if row else default


def start_canary(engine, version, registry=None):
    """Route new runs to a candidate after its gates pass."""
    registry = registry or engine.registry
    ensure_schema(registry.db)
    registry.db.execute("UPDATE installations SET role='canary',status='canary' WHERE version=?",
                        (version,))
    return version


def promote(engine, version, registry=None):
    registry = registry or engine.registry
    ensure_schema(registry.db)
    registry.db.execute("UPDATE installations SET role='retained' WHERE role='active'")
    registry.db.execute("UPDATE installations SET role='active',status='active' WHERE version=?",
                        (version,))
    return version


def rollback(engine, version, note, registry=None):
    """Route new work back to the baseline without touching target work.

    Runs already assigned to the failed version keep their assignment: they are
    reconciled by the supervisor that owns them, not cancelled from here, and no
    accepted work in any target repository is deleted.
    """
    from . import learning
    registry = registry or engine.registry
    ensure_schema(registry.db)
    registry.db.execute(
        "UPDATE installations SET role='rolled_back',status='rolled_back' WHERE version=?",
        (version,))
    registry.db.execute("UPDATE installations SET role='active' WHERE role='retained'")
    trial = registry.db.execute(
        "SELECT * FROM improvement_trials WHERE version=?", (version,)).fetchone()
    if trial:
        registry.db.execute("UPDATE improvement_trials SET status='rolled_back',note=? WHERE id=?",
                            (note, trial["id"]))
        learning.disable(registry.db, trial["lesson_id"], note, learning.ROLLED_BACK)
    affected = [row["run_id"] for row in registry.db.execute(
        "SELECT run_id FROM version_assignments WHERE version=?", (version,))]
    return {"version": version, "active": active_version(engine, registry),
            "runs_still_owned_by_candidate": affected}
