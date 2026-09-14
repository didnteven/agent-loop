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


def _stop_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run_process(argv, cwd, timeout=180, stdin=None, idle_timeout=None, activity=None,
                tee=None):
    """Bound the whole process group; never interpolate prompts into a shell.

    With ``idle_timeout``, a process that keeps producing output is left running
    until the hard ``timeout``; it is stopped only after ``idle_timeout`` seconds
    with no stdout/stderr and, when ``activity`` is given, no change in the value
    it returns (e.g. file modification times). ``tee`` is an optional
    ``(stdout_path, stderr_path)`` pair appended to as output arrives.
    """
    if idle_timeout is None and tee is None:
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

    proc = subprocess.Popen(argv, cwd=cwd,
                            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    if stdin is not None:
        try:
            proc.stdin.write(stdin.encode())
        except BrokenPipeError:
            pass
        proc.stdin.close()
    idle_timeout = float("inf") if idle_timeout is None else idle_timeout
    chunks = {proc.stdout: [], proc.stderr: []}
    sinks = {}
    if tee:
        sinks = {proc.stdout: open(tee[0], "ab"), proc.stderr: open(tee[1], "ab")}
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    sel.register(proc.stderr, selectors.EVENT_READ)
    start = last_output = time.monotonic()
    seen = activity() if activity else None
    reason = ""
    try:
        while sel.get_map():
            now = time.monotonic()
            if now - start >= timeout:
                reason = "worker timeout"
                break
            if now - last_output >= idle_timeout:
                current = activity() if activity else None
                if activity and current != seen:
                    # Silent but still writing files: that is progress too.
                    seen, last_output = current, now
                else:
                    reason = "worker idle timeout: no output for %ds" % idle_timeout
                    break
            wait = min(1.0, timeout - (now - start), idle_timeout - (now - last_output))
            for key, _ in sel.select(timeout=max(0.0, wait)):
                data = os.read(key.fileobj.fileno(), 65536)
                if data:
                    chunks[key.fileobj].append(data)
                    if key.fileobj in sinks:
                        sinks[key.fileobj].write(data)
                        sinks[key.fileobj].flush()
                    last_output = time.monotonic()
                else:
                    sel.unregister(key.fileobj)
    except KeyboardInterrupt:
        _stop_group(proc)
        raise
    finally:
        sel.close()
        for sink in sinks.values():
            sink.close()
    if reason:
        _stop_group(proc)
    else:
        proc.wait()
    out = b"".join(chunks[proc.stdout]).decode(errors="replace")
    err = b"".join(chunks[proc.stderr]).decode(errors="replace")
    proc.stdout.close()
    proc.stderr.close()
    if reason:
        return 124, out, err + "\n" + reason
    return proc.returncode, out, err


def command(provider, prompt, model=None, effort=None, worker=True, sandbox=False,
            workspace=None, scope="files"):
    effort = effort or (model.rsplit("-", 1)[-1]
                        if provider == "antigravity" and model
                        and model.rsplit("-", 1)[-1] in ("low", "medium", "high")
                        else "low")
    if worker and scope == "worktree":
        prompt += ("\n\nExecution constraint: do not delegate, spawn, or call any subagent, "
                   "teammate, agent, or secondary model. Complete this task in the "
                   "current session. You may read and edit any file in this worktree and "
                   "run builds, tests, generators and dependency installs (for example "
                   "`npx playwright install`) as needed. Do not commit or change branches. "
                   "Finish with a concise summary of what you changed and anything that "
                   "still blocks the task.")
    elif worker:
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
        if worker and scope == "worktree":
            # Dependency installs and browser downloads need the network.
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
        if model:
            argv += ["--model", model]
        argv.append(prompt)
    elif provider == "claude" and worker and scope == "worktree":
        # Full tools, including Bash: builds, generators and installs are part of
        # the work. Only delegation stays disabled.
        argv = ["claude", "-p", prompt, "--output-format", "json", "--tools", "default",
                "--disallowed-tools", "Agent", "--no-session-persistence",
                "--permission-mode", "bypassPermissions", "--effort", effort]
    elif provider == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "json", "--tools", "default",
                "--disallowed-tools", "Agent", "--safe-mode", "--no-session-persistence",
                "--restricted", "--permission-mode", "acceptEdits",
                "--permission-prompts", "none", "--effort", effort]
    elif provider == "antigravity":
        argv = ["agy", "-p", prompt, "--output-format", "json", "--mode", "accept-edits",
                # agy's own cutoff would end a working turn and report SUCCESS;
                # the supervisor's idle/hard timeouts decide when to stop instead.
                "--disable-slash-commands", "--effort", effort, "--print-timeout", "24h"]
        if workspace:
            # Without this, agy edits files inside its own project scratch
            # directory and ignores the process working directory entirely, so
            # the managed worktree is never touched and every task fails on a
            # missing file. Binding the workspace is what makes it edit in place.
            argv += ["--add-dir", str(workspace)]
        if sandbox:
            # Terminal restrictions. Measured behaviour: with --sandbox the CLI
            # denies every run_command in headless mode, including the `pwd` the
            # model issues to orient itself, and the worker then produces
            # nothing. It is offered because the caller may want it, but it
            # cannot be combined with auto-approval (the CLI rejects both flags
            # together) and a worker under it will usually fail.
            argv.append("--sandbox")
        elif worker:
            # Parity with the other two adapters, which already run unattended:
            # codex uses approval_policy="never" and claude --permission-prompts
            # none. Headless agy cannot answer a permission prompt, so without
            # this any command it attempts is auto-denied and the task fails.
            # Containment is the managed worktree plus the supervisor's
            # allowlist, history and truncation checks, not this flag.
            argv.append("--dangerously-skip-permissions")
    else:
        raise ValueError("Unknown provider: " + provider)
    if model and provider == "antigravity":
        # agy's -p consumes the very next token as its prompt.
        argv += ["--model", model]
    elif model and provider != "codex":
        prompt_index = argv.index(prompt)
        argv[prompt_index:prompt_index] = ["--model", model]
    return argv


SYSTEM_INSTRUCTIONS = ("claude", "codex")


def supervisor_command(provider, prompt, model=None, effort=None, session_id=None,
                       workspace=None, instructions=None):
    """A read-only, resumable supervisor session. Never a plan/approval mode.

    Plan modes tell the model it is drafting for a human to approve, so it waits
    instead of acting. Read-only comes from the tool set or sandbox instead.
    ``instructions`` is the supervisor role, sent as system/developer
    instructions on every call where the CLI supports it (SYSTEM_INSTRUCTIONS);
    other providers must carry it in the prompt. ``session_id`` resumes the
    provider's own conversation; None starts a fresh one.
    """
    effort = effort or "medium"
    if provider == "claude":
        # Read-only by tool list alone. Not --permission-mode plan: that tells the
        # model it is drafting a plan for user approval, so it never dispatches.
        argv = ["claude", "-p", prompt, "--output-format", "json",
                "--tools", "Read,Grep,Glob", "--effort", effort]
        if instructions:
            argv += ["--append-system-prompt", instructions]
        if model:
            argv += ["--model", model]
        if session_id:
            argv += ["--resume", session_id]
    elif provider == "codex":
        options = ["--json", "--skip-git-repo-check",
                   "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
                   "-c", 'model_reasoning_effort="' + effort + '"',
                   "--disable", "multi_agent", "--disable", "multi_agent_v2"]
        if instructions:
            # A TOML basic string; JSON string escaping is a valid subset of it.
            options += ["-c", "developer_instructions=" + json.dumps(instructions)]
        if model:
            options += ["--model", model]
        if session_id:
            argv = ["codex", "exec", "resume", *options, session_id, prompt]
        else:
            argv = ["codex", "exec", *options, prompt]
    elif provider == "antigravity":
        # No --mode and no permission bypass: headless agy then denies writes and
        # commands on its own (measured), while reads still work.
        argv = ["agy", "-p", prompt, "--output-format", "json",
                "--disable-slash-commands", "--effort", effort, "--print-timeout", "24h"]
        if workspace:
            argv += ["--add-dir", str(workspace)]
        if model:
            argv += ["--model", model]
        if session_id:
            argv += ["--conversation", session_id]
    else:
        raise ValueError("Unknown provider: " + provider)
    return argv


SESSION_KEYS = {"codex": "thread_id", "claude": "session_id", "antigravity": "conversation_id"}


def session_id(provider, out):
    """The provider's resumable conversation id from its JSON output, or None."""
    key = SESSION_KEYS.get(provider)
    found = None
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get(key), str):
            found = event[key]
    if found is None:
        try:
            event = _json_from_output(out)
            found = event.get(key) if isinstance(event.get(key), str) else None
        except ValueError:
            pass
    return found


def context_tokens(provider, usage):
    """Approximate size of the conversation the provider just processed.

    Claude reports per-iteration usage; the last iteration is the live context.
    Codex and Antigravity report turn totals, which over-estimate it, so a reset
    happens early rather than late.
    """
    if not isinstance(usage, dict):
        return 0
    if provider == "claude":
        iterations = usage.get("iterations")
        last = iterations[-1] if isinstance(iterations, list) and iterations else usage
        return sum(last.get(name, 0) or 0 for name in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return usage.get("input_tokens", 0) or 0


def runner_command(provider, argv, workspace, timeout, unknown_retry_seconds=1800,
                   quota_bucket="codex", idle_timeout=None, watch=()):
    """Run one provider through the quota-aware provider boundary.

    The engine deliberately does not launch provider CLIs itself.  This small
    script is the only layer that knows how to wait for a provider reset and
    then re-run its command.
    """
    runner = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts",
                          "run_provider.py")
    idle = ["--idle-timeout", str(idle_timeout)] if idle_timeout else []
    for name in watch:
        idle += ["--watch", name]
    return [sys.executable, runner, "--repo", str(workspace), "--provider", provider,
            "--timeout", str(timeout), *idle, "--unknown-retry-seconds",
            str(unknown_retry_seconds), "--quota-bucket", quota_bucket, "--", *argv]


def usage_fraction(data):
    """The most-consumed quota window, as a fraction in [0, 1].

    Returns None when no window reports a usage percentage: unknown headroom is
    unknown, and must not be read as "plenty left".
    """
    seen = []

    def visit(value):
        if isinstance(value, dict):
            used = value.get("usedPercent", value.get("used_percentage"))
            if isinstance(used, (int, float)):
                seen.append(max(0.0, min(1.0, used / 100.0)))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    # The binding constraint is the window closest to exhaustion.
    return max(seen) if seen else None


def quota_expiry(data, now=None):
    """When the provider's longest quota window resets: unused allowance is lost then.

    Short (five-hour) windows reset for everyone within hours, so comparing them
    says little; the longest window is the allowance that is actually wasted.
    Window length comes from ``windowDurationMins`` when reported, otherwise the
    window with the latest reset is taken as the longest. Returns None if unknown.
    """
    now = time.time() if now is None else now
    if isinstance(data, dict):
        # Codex also lists other limit ids (reserve pools); only the account's own
        # top-level windows describe the allowance this CLI spends.
        data = data.get("rateLimits", data.get("rate_limits", data))
    windows = []

    def visit(value):
        if isinstance(value, dict):
            used = value.get("usedPercent", value.get("used_percentage"))
            reset = value.get("resetsAt", value.get("resets_at"))
            if isinstance(used, (int, float)) and isinstance(reset, (int, float)) and reset > now:
                duration = value.get("windowDurationMins")
                windows.append((duration if isinstance(duration, (int, float)) else -1, reset))
            for key, child in value.items():
                if key != "rateLimitsByLimitId":
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    if not windows:
        return None
    if any(duration > 0 for duration, _ in windows):
        longest = max(duration for duration, _ in windows)
        return min(reset for duration, reset in windows if duration == longest)
    return max(reset for _, reset in windows)


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
    # Only the label's own line: a line without a reset must not borrow the next one's.
    line = value.splitlines()[0] if value else ""
    match = re.search(r"resets\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)",
                      line)
    if not match:
        return None
    month, day, hour, minute, half = match.groups()
    # Claude omits ":00" on the hour ("resets Sep 16 at 5pm").
    parsed = datetime.strptime("%d %s %s %s:%s%s" % (now.year, month, day, hour, minute or "00", half),
                               "%Y %b %d %I:%M%p").replace(tzinfo=now.tzinfo)
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
    """Read Antigravity quota through its official headless ``/usage`` command.

    Recent ``agy`` versions expose the same quota panel through a zero-turn
    JSON command.  Keep the older local utility as a compatibility fallback for
    installations that predate that interface.
    """
    try:
        code, out, err = run_process(
            ["agy", "-p", "/usage", "--output-format", "json", "--print-timeout",
             str(timeout) + "s"], cwd, timeout)
        if code:
            raise RuntimeError((out + err)[-2000:] or "Antigravity usage command failed")
        data = _json_from_output(out)
        windows = []
        command_data = data.get("command", {}).get("data", {})
        for group in command_data.get("groups", []):
            for bucket in group.get("buckets", []):
                remaining = bucket.get("remaining_fraction")
                if not isinstance(remaining, (int, float)):
                    continue
                window = {"usedPercent": round(max(0, min(100, (1 - remaining) * 100)), 6)}
                reset = bucket.get("reset_time")
                if isinstance(reset, str):
                    try:
                        window["resetsAt"] = datetime.fromisoformat(
                            reset.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pass
                windows.append(window)
        if windows:
            return {"rate_limits": {"models": windows}, "source": "agy /usage", "raw": data}
        raise ValueError("Antigravity /usage returned no quota windows")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        pass

    # Compatibility with the pre-1.1.28 development utility.
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


CUTOFF_MARKERS = (
    "print timeout",
    "returning partial output",
    "turn in progress",
    "error_max_turns",
    "max turns reached",
    "reached max turns",
    "turn was interrupted",
)


def provider_cutoff(err, response, events):
    """The marker text when a provider CLI ended a turn on its own limit.

    Only the CLI's own stderr and event types are inspected, never the model's
    reply, which may legitimately quote these phrases.
    """
    texts = [err or ""]
    texts += [str(event.get("subtype", "")) + " " + str(event.get("type", "")) for event in events]
    for text in texts:
        lowered = text.lower()
        for marker in CUTOFF_MARKERS:
            if marker in lowered:
                return marker
    return ""


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
            denied = event.get("denied_actions")
            if isinstance(denied, list) and denied:
                # Antigravity reports SUCCESS even when its sandbox denied every
                # action the worker attempted. A run that was not allowed to act
                # is an environment fault, not a completed turn: reporting it as
                # ok costs another attempt and a review call to rediscover.
                names = sorted({str(item.get("display_name") or item.get("action"))
                                for item in denied if isinstance(item, dict)})
                errors.append("Provider denied worker actions: " + ", ".join(names)
                              + ". Permission denied by the provider sandbox.")
                success = False
            if event.get("is_error") or event.get("error"):
                errors.append(str(event.get("error", response)))
    cutoff = provider_cutoff(err, response, events)
    if cutoff:
        # A CLI's own time/turn limit can end a working turn yet still report
        # success; treat it as a timeout so partial work is continued.
        errors.append("Provider cut off a turn in progress (timed out): " + cutoff)
        success = False
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
