#!/usr/bin/env python3
"""Persist Claude Code status-line rate-limit telemetry for agent-loop."""
import json
import os
import sys
import tempfile


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    limits = data.get("rate_limits")
    cwd = data.get("workspace", {}).get("current_dir") or data.get("cwd")
    if isinstance(limits, dict) and cwd:
        state_dir = os.path.join(cwd, ".agent-loop")
        os.makedirs(state_dir, exist_ok=True)
        target = os.path.join(state_dir, "claude-quota.json")
        fd, temporary = tempfile.mkstemp(prefix="claude-quota.", suffix=".json", dir=state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    model = data.get("model", {}).get("display_name", "Claude")
    windows = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = limits.get(key, {}) if isinstance(limits, dict) else {}
        if isinstance(window, dict) and window.get("used_percentage") is not None:
            windows.append(f"{label}: {window['used_percentage']:.0f}%")
    print(f"[{model}]" + (" | " + " ".join(windows) if windows else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
