#!/usr/bin/env python3
"""Single-invocation provider boundary for agent-loop."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loop.adapters import parse, provider_limits, quota_deadline, run_process  # noqa: E402


def quota_reset(provider, repo, timeout, bucket):
    """Return a known exhausted-window reset, or zero when it is unknown/ready."""
    try:
        limits = provider_limits(provider, repo, timeout)
        if provider == "codex":
            limits = limits.get("rateLimitsByLimitId", {}).get(
                bucket, limits.get("rateLimits", {}))
        else:
            limits = limits.get("rate_limits", limits)
        return quota_deadline(limits)
    except (FileNotFoundError, OSError, RuntimeError, TimeoutError, ValueError):
        # Lack of telemetry is not evidence that quota is empty.  Let the CLI
        # speak for itself; a rate-limit response without a reset is returned
        # to the durable orchestrator for a conservative later retry.
        return 0


def watch_signature(repo, names):
    """Modification state of the watched files and their directories."""
    paths = set()
    for name in names:
        path = Path(repo) / name
        paths.add(path)
        paths.add(path.parent)

    def signature():
        state = []
        for path in sorted(paths):
            try:
                st = path.stat()
                state.append((str(path), st.st_mtime_ns, st.st_size))
            except OSError:
                state.append((str(path), None, None))
        return tuple(state)
    return signature


def provider_wait(deadline, reason, invoked):
    print(json.dumps({"agent_loop_result": "provider_wait", "retry_at": deadline,
                      "reason": reason, "invoked": invoked}), flush=True)
    return 75


def main():
    parser = argparse.ArgumentParser(description="Wait for a provider reset, then run one agent command")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--provider", choices=("codex", "claude", "antigravity"), required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--idle-timeout", type=int, default=None)
    parser.add_argument("--watch", action="append", default=[],
                        help="Workspace-relative path whose changes count as worker activity")
    parser.add_argument("--unknown-retry-seconds", type=int, default=1800)
    parser.add_argument("--quota-bucket", default="codex")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("provider command must follow --")
    command = args.command[1:] if args.command[0] == "--" else args.command
    reset = quota_reset(args.provider, args.repo, args.timeout, args.quota_bucket)
    if reset:
        return provider_wait(reset, "Quota unavailable before dispatch", False)
    code, out, err = run_process(command, args.repo, args.timeout,
                                 idle_timeout=args.idle_timeout,
                                 activity=watch_signature(args.repo, args.watch) if args.watch else None,
                                 tee=("/dev/stdout", "/dev/stderr"))
    sys.stdout.flush()
    result = parse(args.provider, code, out, err)
    if result.status == "rate_limited":
        reset = result.retry_at or quota_reset(args.provider, args.repo, args.timeout,
                                               args.quota_bucket)
        reset = reset or time.time() + args.unknown_retry_seconds
        return provider_wait(reset, result.error or "Provider reported quota exhaustion", True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
