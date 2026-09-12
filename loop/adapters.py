"""Official CLI adapters. No credential extraction or private quota endpoints."""
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass, field


@dataclass
class Result:
    status: str
    response: str = ""
    usage: dict = field(default_factory=dict)
    retry_at: float = 0
    error: str = ""
    estimated_usd: float = 0
    invoked: bool = True


def run_process(argv, cwd, timeout=180, stdin=None):
    """Bound the whole process group; never interpolate prompts into a shell."""
    proc = subprocess.Popen(argv, cwd=cwd,
                            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
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


def command(provider, prompt, model=None, effort=None, worker=True):
    effort = effort or (model.rsplit("-", 1)[-1]
                        if provider == "antigravity" and model
                        and model.rsplit("-", 1)[-1] in ("low", "medium", "high")
                        else "low")
    if worker:
        prompt += ("\n\nExecution constraint: do not delegate, spawn, or call any subagent, "
                   "teammate, agent, or secondary model. Complete this task in the "
                   "current session. Use your repository tools to inspect and edit the "
                   "managed worktree directly. Do not commit, change branches, or edit "
                   "outside the allowed files. Finish with a concise summary.")
    else:
        prompt += ("\n\nExecution constraint: do not delegate, spawn, or call any subagent, "
                   "teammate, agent, or secondary model. Do not edit files, commit, or "
                   "change branches. Return only the requested response.")
    if provider == "codex":
        argv = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--json",
                "--disable", "multi_agent", "--disable", "multi_agent_v2",
                "-s", "workspace-write", "-c", 'approval_policy="never"',
                "-c", 'model_reasoning_effort="' + effort + '"']
        if model:
            argv += ["--model", model]
        argv.append(prompt)
    elif provider == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "json", "--tools", "default",
                "--disallowed-tools", "Agent", "--safe-mode", "--no-session-persistence",
                "--restricted", "--permission-mode", "acceptEdits",
                "--permission-prompts", "none", "--effort", effort]
    elif provider == "antigravity":
        argv = ["agy", "-p", prompt, "--output-format", "json", "--mode", "accept-edits",
                "--sandbox", "--disable-slash-commands", "--effort", effort,
                "--print-timeout", "150s"]
    else:
        raise ValueError("Unknown provider: " + provider)
    if model and provider == "antigravity":
        # agy's -p consumes the very next token as its prompt.
        argv += ["--model", model]
    elif model and provider != "codex":
        prompt_index = argv.index(prompt)
        argv[prompt_index:prompt_index] = ["--model", model]
    return argv


def runner_command(provider, argv, workspace, timeout, unknown_retry_seconds=1800,
                   quota_bucket="codex"):
    """Run one provider through the quota-aware provider boundary.

    The engine deliberately does not launch provider CLIs itself.  This small
    script is the only layer that knows how to wait for a provider reset and
    then re-run its command.
    """
    runner = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts",
                          "run_provider.py")
    return [sys.executable, runner, "--repo", str(workspace), "--provider", provider,
            "--timeout", str(timeout), "--unknown-retry-seconds",
            str(unknown_retry_seconds), "--quota-bucket", quota_bucket, "--", *argv]


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


def _json_from_output(output):
    """Decode a JSON object even when a CLI adds harmless log lines."""
    # Some CLIs print a label or ANSI-colored status before pretty JSON.
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    decoder = json.JSONDecoder()
    candidates = [index for index, char in enumerate(cleaned) if char == "{"]
    for index in candidates:
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Provider did not return a JSON object")


def _claude_usage_snapshot(cwd, timeout=30):
    code, out, err = run_process(
        ["claude", "-p", "/usage", "--output-format", "json", "--no-session-persistence"],
        cwd, timeout)
    if code:
        raise RuntimeError((out + err)[-2000:] or "Claude usage command failed")
    return _json_from_output(out)


def _parse_claude_reset(value, now=None):
    """Parse Claude's human-readable local reset into a Unix timestamp."""
    now = datetime.now().astimezone() if now is None else datetime.fromtimestamp(now).astimezone()
    match = re.search(r"resets\s+([A-Z][a-z]{2}\s+\d{1,2}\s+at\s+\d{1,2}:\d{2}\s*[ap]m)", value)
    if not match:
        return None
    parsed = datetime.strptime(match.group(1), "%b %d at %I:%M%p").replace(
        year=now.year, tzinfo=now.tzinfo)
    if parsed.timestamp() < now.timestamp() - 86400:
        parsed = parsed.replace(year=now.year + 1)
    return parsed.timestamp()


def claude_limits(cwd, timeout=30):
    """Read Claude's official headless /usage data, with a snapshot fallback."""
    path = os.path.join(cwd, ".agent-loop", "claude-quota.json")
    try:
        data = _claude_usage_snapshot(cwd, timeout)
        text = str(data.get("result", ""))
        windows = {}
        for label, key in (("Current session", "primary"), ("Current week", "secondary")):
            label_pattern = re.escape(label) + (r"(?:\s+\(all models\))?" if label == "Current week" else "")
            match = re.search(label_pattern + r":\s*(\d+)% used", text)
            if match:
                window = {"usedPercent": int(match.group(1))}
                reset = _parse_claude_reset(text[text.find(label):])
                if reset:
                    window["resetsAt"] = reset
                windows[key] = window
        if not windows:
            raise ValueError("Claude /usage returned no recognizable rate-limit windows")
        return {"rate_limits": windows, "source": "claude /usage", "raw": data}
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        if not os.path.exists(path):
            raise
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("rate_limits"), dict):
            raise ValueError("Claude quota snapshot has no rate_limits object")
        return data


def antigravity_limits(cwd, timeout=30):
    """Read Antigravity quota through the installed local utility's JSON interface."""
    utility = os.environ.get("AGENT_LOOP_ANTIGRAVITY_USAGE_DIR", "/tmp/antigravity-usage")
    runner = os.path.join(utility, "node_modules", ".bin", "tsx")
    entrypoint = os.path.join(utility, "src", "index.ts")
    if not os.path.isfile(runner) or not os.path.isfile(entrypoint):
        raise FileNotFoundError(
            "Antigravity quota utility is not installed; set AGENT_LOOP_ANTIGRAVITY_USAGE_DIR")
    code, out, err = run_process(
        [runner, entrypoint, "quota", "--json", "--method", "local"], cwd, timeout)
    if code:
        raise RuntimeError((out + err)[-2000:] or "Antigravity quota command failed")
    data = _json_from_output(out)
    models = data.get("models", [])
    windows = []
    for model in models:
        remaining = model.get("remainingPercentage")
        if not isinstance(remaining, (int, float)):
            continue
        window = {"usedPercent": max(0, min(100, (1 - remaining) * 100))}
        reset = model.get("resetTime")
        if isinstance(reset, str):
            try:
                window["resetsAt"] = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        windows.append(window)
    if not windows:
        raise ValueError("Antigravity quota returned no model windows")
    return {"rate_limits": {"models": windows}, "source": "antigravity-usage", "raw": data}


AUTH_PROBES = {
    "codex": ["codex", "login", "status"],
    "claude": ["claude", "auth", "status"],
    "antigravity": ["agy", "auth", "status"],
}


def auth_probe(provider, cwd, timeout=30):
    """Ask the CLI whether credentials work, without starting an inference turn.

    Returns True (authenticated), False (not authenticated) or None (the probe
    itself is unavailable, which is unknown rather than either answer).
    """
    argv = AUTH_PROBES.get(provider)
    if not argv:
        return None
    try:
        code, out, err = run_process(argv, cwd, timeout)
    except (OSError, ValueError):
        return None
    text = (out + err).lower()
    if any(phrase in text for phrase in ("unknown command", "unrecognized", "usage:",
                                         "no such command")):
        return None
    if code == 0 and not any(phrase in text for phrase in (
            "not logged in", "logged out", "no credentials", "please run")):
        return True
    if any(phrase in text for phrase in ("not logged in", "logged out", "no credentials",
                                         "authentication required", "please run")):
        return False
    return None


def provider_limits(provider, cwd, timeout=30):
    if provider == "codex":
        return codex_limits(cwd, timeout)
    if provider == "claude":
        return claude_limits(cwd, timeout)
    if provider == "antigravity":
        return antigravity_limits(cwd, timeout)
    raise ValueError("Unknown provider: " + provider)


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
    waits = [event for event in events if event.get("agent_loop_result") == "provider_wait"]
    if code == 75 and len(waits) == 1:
        wait = waits[0]
        retry_at = wait.get("retry_at")
        if not isinstance(retry_at, (int, float)) or retry_at <= now:
            return Result("error", error="Invalid provider_wait envelope")
        return Result("provider_wait", response, usage, retry_at,
                      str(wait.get("reason", "Provider unavailable")), cost,
                      bool(wait.get("invoked", False)))
    deadline = quota_deadline(events, now)
    if code == 0 and success and not errors:
        return Result("ok", response, usage, deadline, estimated_usd=cost)
    message = "\n".join(errors + [err, response])[-4000:]
    lower = message.lower()
    if any(x in lower for x in ("not logged in", "authentication required", "authentication_failed", "please run /login")):
        status = "auth_required"
    elif deadline or any(x in lower for x in ("rate_limit", "rate limit", "quota exhausted", "usage limit", "session limit", "weekly limit", "resource_exhausted")):
        status = "rate_limited"
    elif code == 124 or any(x in lower for x in (
        "timed out", "connection", "stream was interrupted", "broken pipe", "overloaded", "503", "502")):
        status = "transient"
    else:
        status = "error"
    return Result(status, response, usage, deadline, message, cost)


def asked_question(text):
    tail = str(text or "").strip()[-400:]
    return bool(tail) and (tail.endswith("?") or bool(re.search(
        r"(?i)\b(should i|do you want|which (one|option)|please confirm|let me know)\b", tail)))


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
    proc = subprocess.Popen(["codex", "app-server", "--stdio"], cwd=cwd, stdin=subprocess.PIPE,
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
