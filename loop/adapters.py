"""Official CLI adapters. No credential extraction or private quota endpoints."""
import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class Result:
    status: str
    response: str = ""
    usage: dict = field(default_factory=dict)
    retry_at: float = 0
    error: str = ""
    estimated_usd: float = 0


def run_process(argv, cwd, timeout=180, stdin=None):
    """Bound the whole process group; never interpolate prompts into a shell."""
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        return proc.returncode, out, err
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
        if isinstance(exc, KeyboardInterrupt):
            raise
        return 124, out, err + "\nworker timeout"


def command(provider, prompt, model=None):
    prompt += ("\n\nExecution constraint: do not delegate, spawn, or call any subagent, "
               "teammate, agent, or secondary model. Complete this task in the "
               "current session and return only the requested output format.")
    if provider == "codex":
        argv = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--json",
                "-s", "read-only", "-c", 'approval_policy="never"',
                "-c", 'model_reasoning_effort="low"', prompt]
    elif provider == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "json", "--tools", "",
                "--disallowed-tools", "Agent", "--safe-mode", "--no-session-persistence",
                "--effort", "low"]
    elif provider == "antigravity":
        argv = ["agy", "-p", prompt, "--output-format", "json", "--mode", "plan",
                "--sandbox", "--effort", "low", "--print-timeout", "150s"]
    else:
        raise ValueError("Unknown provider: " + provider)
    if model:
        argv += ["--model", model]
    return argv


def quota_deadline(data, now=None):
    """Both quota windows must be available. Never assume a fixed five hours."""
    now = time.time() if now is None else now
    deadlines = []
    def visit(value):
        if isinstance(value, dict):
            used = value.get("usedPercent", value.get("used_percentage"))
            reset = value.get("resetsAt", value.get("resets_at"))
            if isinstance(used, (int, float)) and used >= 100:
                if isinstance(reset, (int, float)) and reset > now:
                    deadlines.append(reset + 5)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(data)
    return max(deadlines, default=0)


def claude_limits(cwd):
    """Read the latest Claude Code status-line snapshot for a workspace."""
    path = os.path.join(cwd, ".agent-loop", "claude-quota.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("rate_limits"), dict):
        raise ValueError("Claude quota snapshot has no rate_limits object")
    return data


def parse(provider, code, out, err, now=None):
    now = time.time() if now is None else now
    events = []
    for line in out.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
        except ValueError:
            pass
    response, usage, success, errors, cost = "", {}, False, [], 0
    for event in events:
        if provider == "codex":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                response = item.get("text", "")
            if event.get("type") == "turn.completed":
                usage, success = event.get("usage", {}), True
            if event.get("type") in ("error", "turn.failed"):
                errors.append(str(event.get("error", event.get("message", event))))
        else:
            response = event.get("result", event.get("response", response))
            usage = event.get("usage", usage)
            cost = event.get("total_cost_usd", cost)
            if provider == "claude":
                success = event.get("type") == "result" and not event.get("is_error", True)
            else:
                success = event.get("status") == "SUCCESS"
            if event.get("is_error") or event.get("error"):
                errors.append(str(event.get("error", response)))
    deadline = quota_deadline(events, now)
    if code == 0 and success and not errors:
        return Result("ok", response, usage, deadline, estimated_usd=cost)
    message = "\n".join(errors + [err, response])[-4000:]
    lower = message.lower()
    if any(x in lower for x in ("not logged in", "authentication required", "authentication_failed", "please run /login")):
        status = "auth_required"
    elif deadline or any(x in lower for x in ("rate_limit", "rate limit", "quota exhausted", "usage limit", "session limit", "weekly limit", "resource_exhausted")):
        status = "rate_limited"
    elif code == 124 or any(x in lower for x in ("timed out", "connection", "overloaded", "503", "502")):
        status = "transient"
    else:
        status = "error"
    return Result(status, response, usage, deadline, message, cost)


def token_total(provider, usage):
    if isinstance(usage.get("total_tokens"), int):
        return usage["total_tokens"]
    total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    # Codex input already includes cached input; Anthropic reports caches separately.
    if provider == "claude":
        total += usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    return total


def codex_limits(cwd, timeout=20):
    """Read official app-server RPC, without starting an inference turn."""
    proc = subprocess.Popen(["codex", "app-server"], cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    pending = b""
    def send(value):
        proc.stdin.write((json.dumps(value) + "\n").encode())
        proc.stdin.flush()
    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "agent_loop", "version": "0.1.0"}}})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not sel.select(timeout=min(1, max(0, end-time.monotonic()))):
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Codex app-server closed before returning quotas")
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                event = json.loads(line)
                if event.get("id") == 1:
                    if "error" in event:
                        raise RuntimeError(str(event["error"]))
                    send({"method": "initialized"})
                    send({"id": 2, "method": "account/rateLimits/read"})
                elif event.get("id") == 2:
                    if "error" in event:
                        raise RuntimeError(str(event["error"]))
                    return event["result"]
        raise TimeoutError("Codex quota request timed out")
    finally:
        sel.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        proc.stdin.close()
        proc.stdout.close()
