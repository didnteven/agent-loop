#!/usr/bin/env python3
"""A provider CLI stand-in: writes the requested file and prints codex-shaped JSON.

Used by the detached-worker tests so a real child process, a real workspace
lock, and a real result file are exercised rather than an injected callable.
"""
import json
import os
import sys
import time
from pathlib import Path


def main():
    workspace = Path(os.getcwd())
    name = os.environ.get("FAKE_PROVIDER_FILE", "answer.py")
    content = os.environ.get("FAKE_PROVIDER_CONTENT", "value = 42\n")
    hold = float(os.environ.get("FAKE_PROVIDER_HOLD_SECONDS", "0"))
    if hold:
        (workspace / ".fake-provider-started").write_text(str(time.time()))
        time.sleep(hold)
    (workspace / name).write_text(content)
    print(json.dumps({"item": {"type": "agent_message", "text": "Implemented"}}))
    print(json.dumps({"type": "turn.completed",
                      "usage": {"input_tokens": 11, "output_tokens": 5}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
