---
name: agent-loop-plan
description: Author and validate JSON milestone plans for the agent-loop supervisor (loop/engine.py) — the file passed to `python3 -m loop run <plan>.json`. Use when the user asks to create, write, or fix an agent-loop plan, task list, or milestone JSON, or mentions clamp-style acceptance checks, codex/claude/antigravity task providers, or `loop run`.
---

# Agent Loop Plan Authoring

Plans are JSON files consumed by `Engine.tick` (`loop/engine.py`) and validated by `validate_plan`. Write the plan **in the target repo** the code will be generated into — task `files` paths are relative to that repo's worktree.

## Authoring a plan for a different repo than the one you're in

The `loop` module itself doesn't need to be inside the target repo — invocation always takes `--repo <path>`. It's common to be working from the agent-loop checkout (or any other directory) while writing a plan meant for a separate target repo. When that's the case:

1. Ask for (or confirm) the target repo's absolute path if it isn't already clear from context.
2. Read that repo's actual structure before writing `files`/`check` paths — don't guess filenames. List its directory tree and open any files the new task should extend or match conventions with.
3. Verify the target repo is a git repo, clean, and has a commit (`git -C <target-repo> status --porcelain` should be empty).
4. Write the plan JSON **into the target repo** (e.g. `<target-repo>/agent-loop-plans/<id>.json` or its root) — not into agent-loop's own directory — so it travels with the code it describes.
5. Give the run command with an explicit `--repo`:
   ```sh
   python3 -m loop --repo <target-repo> run <target-repo>/<plan>.json
   python3 -m loop --repo <target-repo> status
   ```

## Required top-level fields

```json
{
  "id": "short-lowercase-slug",
  "tasks": [ ... ]
}
```

- `id`: matches `[a-z0-9][a-z0-9-]{0,60}`. Becomes branch `loop/<id>` and worktree `.agent-loop/worktrees/<id>`. Changing an existing plan's content requires a **new** id — the engine hashes the plan and refuses a same-id edit.

## Optional top-level fields

| Field | Default | Purpose |
|---|---|---|
| `description` | — | free text, not used by the engine |
| `max_attempts` | 3 | repair attempts per task before it's `blocked` |
| `worker_timeout_seconds` | 180 | default for `worker_idle_timeout_seconds` (and the auto-tune baseline) |
| `worker_idle_timeout_seconds` | `worker_timeout_seconds` | stop a worker only after this many seconds with **no output**; a worker that keeps streaming keeps running |
| `worker_max_seconds` | 14400 | hard wall-clock cap per model call |

A worker stopped by either timeout is not treated as a failed repair: its edits to the task's allowed `files` are kept (anything else is discarded), the attempt count is not consumed, and the next attempt is told to continue from that work. If a timeout produces no new change since the previous one, it falls back to the normal transient-failure path so a stuck worker still terminates. Don't raise timeouts or split tasks just to survive slow work. Writes to allowed files count as activity, live output is in `.agent-loop/logs/<run>/<attempt>.jsonl`, and a check-rejected attempt's files are saved to `.agent-loop/rejected/<run>/<task>/` for the next attempt.

`files` must not be gitignored in the target repo (check with `git check-ignore -v <path>`); `validate_plan` rejects them because they can never be committed. Also remember gitignored *inputs* (e.g. generated screenshots) don't exist inside the managed worktree — reference them by absolute path in the main checkout.
| `check_timeout_seconds` | 30 | timeout for running each task's `check` |
| `setup` | none | one-time argv command run in the fresh worktree before workers |
| `setup_timeout_seconds` | 600 | timeout for the one-time setup command |
| `unknown_quota_retry_seconds` | 1800 | retry delay when a rate-limit has no machine-readable reset |
| `read_codex_quotas` | true | preflight Codex's app-server quota API before spending a turn |
| `failure_review` | false | on a failed trusted check or terminal worker error, ask one advisory checker whether to retry or block |
| `failure_review_provider` | task's provider | provider for the failure checker; it must be `codex`, `claude`, or `antigravity` |
| `failure_review_model` | none | optional model id for the failure checker |
| `provider_token_budgets` | none | e.g. `{"codex": 100000}` — local admission cap per provider, counted across this plan's completed responses |
| `review_provider` | `claude` | which CLI performs optional self-review (see below), only used if `--review` is passed |
| `review_model` | none | model id for the review call, only used if `--review` is passed |

## Optional self-review (`--review`)

`run --review` runs a model-based sanity check of the plan (are the tasks real/needed, could each `check` plausibly verify its `prompt`) before the first task starts, and aborts before touching the repo if declined. `publish --review` runs a model-based comparison of the finished diff against each task's `prompt`, flagging fabricated/unrelated/unnecessary changes, and aborts before pushing if declined. Both are optional, off by default, and advisory — they never replace the trusted `check` command, which is still the only thing that actually gates a commit. Mention `--review` to the user as an available option when handing back run/publish commands, especially for plans touching anything sensitive.

## Task object (each entry in `tasks`)

```json
{
  "id": "short-lowercase-slug",
  "provider": "codex | claude | antigravity",
  "model": "provider-specific model id",
  "files": ["relative/path/one.py"],
  "prompt": "Implementation instructions for exactly these files.",
  "check": ["python3", "path/to/verify.py", "relative/path/one.py"]
}
```

- `id`: unique within the plan, same slug pattern as plan `id`.
- `provider`: must be exactly one of `codex`, `claude`, `antigravity` — no other values are accepted.
- `files`: non-empty, no duplicates. These are the *only* paths the worker is allowed to write; any other changed/untracked file in the worktree aborts the run with "Unexpected changes in managed worktree." Paths are validated against path traversal (`safe_path`) — no `..`, no absolute paths.
- `check`: a non-empty **argv list** (not a shell string) run inside the task's worktree with cwd = workspace. It must be committed before the run and must not be one of the worker-writable `files`. On failure, the worker's changes are rolled back and the next attempt receives the check output.
- `prompt`: task-specific implementation instructions. Workers use normal repository tools and edit the managed worktree directly; delegation and subagents remain disabled. Do not request a JSON/file-bundle response.

Optional per-task fields:
- `context_files`: optional paths whose current contents (first 30000 chars each) are emphasized in the prompt. Workers can inspect the repository themselves, so use this only when highlighting specific earlier-task output helps.
- `quota_bucket`: which Codex rate-limit bucket to preflight-check (default `"codex"`), only relevant when `provider` is `codex`.
- `effort`: `low`, `medium`, or `high` (default `low`). Antigravity model tier suffixes select matching effort automatically; an explicit conflicting value is rejected.
- `max_file_shrink_fraction`: largest allowed size reduction for an existing file of at least 1000 bytes (default `0.5`). Set `allow_large_deletions: true` only when substantial deletion is explicitly intended.

## Failure review

Set `failure_review` when a plan should use one extra model call only after a trusted check fails or a worker returns a terminal error. The checker receives the task, failure output, and uncommitted diff, then returns `RETRY` or `BLOCK`. It cannot edit files, change the immutable plan, delegate work, or replace the trusted check. Use `failure_review_provider` to choose a provider with available quota.

## Checking provider usage before assigning `provider` per task

Before deciding which provider each task should use, check current usage/quota for all three and prefer the provider(s) with the most headroom. Do this every time a plan is authored, not just on request.

1. **Codex** — 7-day and 5h windows, no auth prompt required:
   ```sh
   python3 -m loop --repo <target-repo> codex-quota
   ```
   Read `rateLimits.secondary.usedPercent` (7-day window) and `.primary.usedPercent` (5h window).
2. **Claude** — session and 7-day usage:
   ```sh
   claude -p "/usage"
   ```
   Read the "Current week (all models)" line for the 7-day figure.
3. **Antigravity** — no official headless CLI is installed by default; the open-source `antigravity-usage` tool (github.com/skainguyen1412/antigravity-usage, MIT) reads quota from the local Antigravity language server if the Antigravity app is running:
   ```sh
   cd /tmp && git clone --depth 1 https://github.com/skainguyen1412/antigravity-usage.git && cd antigravity-usage && npm install
   npx tsx src/index.ts quota
   ```
   If it reports "Antigravity is not running", launch the app first (`open -a "Antigravity"`, wait a few seconds for `language_server` to start) and retry. This is third-party code hitting your local process, not a remote API — confirm with the user before installing/running it the first time in a session.

Rank providers by ascending "used%" on their longest available window (prefer an untouched/lowest-used window over one closer to its cap), then by soonest-resetting window as a tiebreak. Assign tasks in the plan to providers in that order — put the heaviest/most numerous tasks on whichever provider has the most headroom, and avoid a provider whose relevant window is nearly exhausted unless no alternative fits the task (e.g. it needs a model only that provider offers). Note the chosen ranking and the raw numbers in the plan `description` or in your response to the user so the reasoning is visible.

## Task ordering and dependencies

Tasks run **sequentially** in array order within one `tick`. A task can only rely on files from earlier tasks in the same plan (list them in `context_files`); there is no parallel fan-out inside one plan. For independent, unrelated pieces of work, list them as separate tasks with disjoint `files` — order won't matter for those, but they still run one at a time.

## Writing a good `check`

The check is a trusted local script or command, not a prompt hint. Patterns:
- A pre-existing standalone verifier script (see `examples/verify_clamp.py`) invoked as `["python3", "examples/verify_clamp.py", "demo/output.py"]` — it should import/exec the generated file and assert behavior, exiting non-zero with a readable message on failure. Never let a task write its own verifier.
- An existing test command scoped to the new file(s), e.g. `["python3", "-m", "pytest", "tests/test_widget.py", "-x"]`.
- Keep checks fast — they run on every attempt, up to `max_attempts` times per task, and gate the final milestone-wide regression pass too (every task's `check` re-runs once more before `ready_for_pr`).

## Minimal template

```json
{
  "id": "my-milestone",
  "tasks": [
    {
      "id": "my-task",
      "provider": "claude",
      "model": "claude-haiku-4-5-20251001",
      "files": ["path/to/new_file.py"],
      "prompt": "Describe exactly what path/to/new_file.py should contain.",
      "check": ["python3", "path/to/verify.py", "path/to/new_file.py"]
    }
  ]
}
```

Multi-task / multi-provider plans (e.g. comparing providers, or building sequential pieces) follow `examples/smoke.json` in this repo — read it for a worked reference before writing a new plan from scratch.

## Before handing back a plan

1. **Cut a fresh branch from freshly-pulled `main` (or the repo's real default branch) first, always** — before writing the plan file or running anything. `Engine.initialize` bases the milestone worktree on whatever commit is currently checked out in the target repo, not on any named branch. If that happens to be a stale or unrelated branch (e.g. leftover work-in-progress), `publish` will later fail its ancestor-of-base check against `origin/main`, and there is no way to rebase an existing run in place — only a brand-new plan `id` can restart from a different base. Do this every time, even if the currently checked-out branch looks fine:
   ```sh
   git -C <target-repo> fetch origin main
   git -C <target-repo> checkout -b <plan-id>-base origin/main
   ```
2. Confirm the target repo is clean and has at least one commit (`validate_plan`/`initialize` will reject a dirty tree at run time, but check first so the user isn't surprised). If checking out the new branch surfaces an untracked `.agent-loop/` from a prior run, add `.agent-loop/` to `.gitignore` and commit it — an untracked directory still counts as "dirty" to the clean-tree check.
3. Confirm every `files` entry across all tasks is unique repo-wide if tasks touch overlapping areas — the engine only checks per-task uniqueness, not cross-task overlap, but two tasks racing to touch the same file will make the second task's diff check fail.
4. Remind the user of the run command: `python3 -m loop --repo <target-repo> run <plan>.json` (add `--once` for a single scheduler tick), and that `publish` defaults `--base` to `main` — which now matches, because of step 1.
