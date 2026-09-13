"""Detached worker boundary: prompt assembly, ownership, and durable results.

The supervisor never builds a provider command line or interprets a transcript.
It writes a validated *attempt record* and launches this module as its own
session leader; everything after that — prompt assembly, provider argv, the
quota-aware runner, and the final result — belongs to the worker.

Ownership is proven by an exclusive flock on the workspace that is held from
before the provider starts until after the result is durably written. A PID, or
``kill -0`` on one, proves nothing: PIDs are reused, and a live PID says nothing
about whether that process still owns this workspace. So recovery asks the lock,
not the process table, and uses the recorded process-start identity only to
refuse a reused PID.

Because the lock is held across the provider call, a supervisor that cannot take
it must treat ownership as *unknown* and enter recovery rather than dispatching
a replacement worker into the same worktree.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .adapters import command, parse, runner_command, run_process
from .storage import atomic_json

HEARTBEAT_SECONDS = 5


def workspace_lock_path(home, workspace):
    digest = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:16]
    return Path(home) / "locks" / ("ws-" + digest + ".lock")


def process_identity(pid):
    """A start time that distinguishes this process from a later reused PID."""
    try:
        out = subprocess.check_output(["ps", "-o", "lstart=", "-p", str(pid)], text=True,
                                      stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        return ""
    return out.strip()


def build_prompt(record):
    """Assemble the worker prompt from the validated task record alone.

    Instructions come first, then the failure being repaired, then journal notes,
    then context excerpts: when the budget binds, the worker keeps what it needs
    to act and loses only breadth.
    """
    from .context import DEFAULT_CONTEXT_CHARACTERS, fit
    task = record["task"]
    workspace = Path(record["workspace"])
    sections = [("instructions",
                 "Implement this coding section directly in the managed worktree. "
                 "Use repository tools to read, search, edit, and run focused diagnostics. "
                 "You may modify only these paths: " + json.dumps(task["files"]) + ". "
                 "Do not commit or change branches.\n" + task["prompt"])]
    if record.get("previous_error"):
        from .engine import previous_attempt_note
        sections.append(("previous failure", previous_attempt_note(record["previous_error"])))
    if record.get("journal"):
        sections.append(("journal", "\nJOURNAL\n" + record["journal"]))
    for name in task.get("context_files", []):
        # safe_path was already applied by the supervisor when it validated the
        # plan; re-resolve here so a tampered record cannot read outside.
        from .engine import safe_path
        sections.append(("context " + name,
                         "\nCONTEXT " + name + "\n" + safe_path(workspace, name).read_text()))
    return fit(sections, record.get("context_characters") or DEFAULT_CONTEXT_CHARACTERS)


def run_attempt(record_path):
    record_path = Path(record_path)
    record = json.loads(record_path.read_text())
    workspace = Path(record["workspace"])
    lock_path = workspace_lock_path(record["home"], workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another owner holds this workspace. Touch nothing.
        return 3
    try:
        record.update(pid=os.getpid(), start_identity=process_identity(os.getpid()),
                      state="registered", heartbeat_at=time.time())
        atomic_json(record_path, record)
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    current = json.loads(record_path.read_text())
                    current["heartbeat_at"] = time.time()
                    atomic_json(record_path, current)
                except (OSError, ValueError):
                    return

        heart = threading.Thread(target=beat, daemon=True)
        heart.start()
        task = record["task"]
        try:
            agent = command(task["provider"], build_prompt(record), task.get("model"),
                            task.get("effort"), sandbox=record.get("sandbox", False),
                            workspace=workspace)
            idle = record.get("worker_idle_timeout_seconds")
            hard = (record.get("worker_max_seconds", 14400) if idle
                    else record.get("worker_timeout_seconds", 180))
            argv = runner_command(task["provider"], agent, workspace, hard,
                                  record.get("unknown_quota_retry_seconds", 1800),
                                  task.get("quota_bucket", "codex"), idle,
                                  task["files"] if idle else ())
            logs = Path(record["log_directory"])
            logs.mkdir(parents=True, exist_ok=True)
            # Written as output arrives so a running attempt can be followed live.
            live = (logs / (record["attempt_id"] + ".jsonl"),
                    logs / (record["attempt_id"] + ".stderr"))
            code, out, err = run_process(argv, workspace,
                                         record.get("provider_runner_timeout_seconds", 86400),
                                         tee=live)
            result = parse(task["provider"], code, out, err)
            payload = {"status": result.status, "response": result.response,
                       "usage": result.usage, "retry_at": result.retry_at,
                       "error": result.error, "estimated_usd": result.estimated_usd,
                       "invoked": result.invoked}
        except (OSError, ValueError) as exc:
            payload = {"status": "error", "response": "", "usage": {}, "retry_at": 0,
                       "error": str(exc), "estimated_usd": 0, "invoked": False}
        finally:
            stop.set()
        # The result carries every identity the supervisor will verify, so a
        # result from a superseded attempt can never be reaped as this one's.
        atomic_json(record["result_path"], {
            "attempt_id": record["attempt_id"], "nonce": record["nonce"],
            "run_id": record["run_id"], "task_id": record["task_id"],
            "base": record["base"], "policy_version": record["policy_version"],
            "finished_at": time.time(), "result": payload})
        record["state"] = "result_written"
        record["heartbeat_at"] = time.time()
        atomic_json(record_path, record)
        return 0
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m loop.worker <attempt-record.json>", file=sys.stderr)
        return 2
    return run_attempt(argv[0])


if __name__ == "__main__":
    raise SystemExit(main())
