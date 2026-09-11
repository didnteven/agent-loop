---
name: agent-loop-run
description: Start, monitor, and recover the repository's Python agent-loop supervisor for a saved JSON plan. Use when the user asks to run a plan, keep it running, check progress, or recover a failed task.
---

# Run the Python supervisor

Use the Python supervisor as the watcher and orchestrator. Do not use Claude Code `/loop` or another model driven heartbeat; those consume model session usage while idle.

Run from the repository root:

```sh
python3 -m loop run <plan>.json
```

The command stays alive, persists progress under `.agent-loop/`, waits locally when a provider quota is exhausted, and resumes completed work after restart. Use `--once` only when an external scheduler or service manager will invoke it repeatedly:

```sh
python3 -m loop run <plan>.json --once
```

Check progress without starting workers:

```sh
python3 -m loop status
```

When a task is waiting, leave the Python process running. When it is blocked by authentication or configuration, inspect the recorded error, fix the provider setup, then reset that task and rerun:

```sh
python3 -m loop retry <run-id> <task-id>
python3 -m loop run <plan>.json
```

When the plan reaches `ready_for_pr`, stop treating it as an active watcher. Publishing and merging are separate explicit operations:

```sh
python3 -m loop publish <run-id> --github OWNER/REPOSITORY
```

For unattended operation on macOS, use `launchd` to keep the Python process alive and restart it after failure. Do not launch a Claude `/loop` schedule for this purpose. The machine must be awake for local processes to run.
