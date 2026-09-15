# Agent Loop

A small custom supervisor is a good fit for this workflow. Keep the always-on loop in ordinary code: it costs no model tokens to wait, survives model quota exhaustion, and can resume after a restart. Use a model only to plan, implement a bounded section, or review a milestone.

This is a working local prototype using Python 3.9+ and its standard library. No server, queue service, framework, or API key is required. It invokes the installed `codex`, `claude`, and `agy` terminal clients using their existing authentication.

## Try it

From this folder:

```sh
python3 -m unittest discover -s tests -v
python3 -m loop run examples/smoke.json
python3 -m loop status
python3 -m loop codex-quota
python3 -m loop usage
```

The repository must have an initial commit and a clean working tree before a new milestone starts. The supplied plan asks three providers to implement a tiny Python `clamp` function. Each implementation must pass 11 independent acceptance cases before the supervisor commits it. A final pass checks the entire milestone.

Workers use normal read, search, edit, and command tools inside the isolated managed worktree. Delegation and subagent tools remain disabled. The supervisor verifies that `HEAD` did not move, rejects changes outside each task's `files` allowlist, blocks suspicious truncation of existing files, runs the trusted check, and commits only accepted changes. Failed and interrupted attempts are rolled back before retrying.

Plans that need ignored dependencies in each fresh worktree can define a one-time setup command, such as `"setup": ["npm", "ci", "--prefix", "frontend"]`. Setup must leave tracked and unignored files clean. `setup_timeout_seconds` defaults to 600 seconds.

Generated code lives in `.agent-loop/worktrees/three-provider-smoke`, on branch `loop/three-provider-smoke`. Runtime state, quota observations, raw output, and the prepared PR body live under `.agent-loop/` and are ignored by Git. The same command resumes a saved plan and skips completed sections. Changing a saved plan requires a new plan ID.

## Unattended operation

`python3 -m loop queue add plan.json [--github OWNER/REPO]` puts an objective on a durable
queue; `python3 -m loop service` runs the deterministic service over it, and
`python3 -m loop service --launchd LABEL` prints a `KeepAlive` definition to install
separately. Selecting work, waiting, backing off and reconciling a release make no model
calls, so an idle or quota-blocked service costs nothing.

Held providers recover through periodic non-inference probes rather than waiting for some
other task to succeed. Optional spending caps live in the execution policy as `limits`;
omitting them means measurement without a ceiling. Reservations are taken in one shared
account-global registry (`~/.agent-loop/registry.sqlite`), so two targets driving the same
subscription cannot spend the same allowance twice, and usage that is never confirmed keeps
holding its reservation instead of being recorded as zero.

`python3 -m loop learn` folds new events into scoped lessons without any model call, and
`python3 -m loop lessons` shows their trial status. `python3 -m loop improve` proposes,
evaluates, promotes and rolls back candidate versions of the supervisor itself; enforcement
and accounting surfaces are fenced from improvement workers in code, and a candidate with no
pinned independent fixtures is ineligible rather than deployed.

Workers are stopped only after `worker_idle_timeout_seconds` without any output (default: `worker_timeout_seconds`), under a `worker_max_seconds` hard cap (default 4 hours). A stopped worker's edits to its allowed files are preserved and the next attempt continues from them without consuming a repair attempt; a timeout with no new progress falls back to the normal bounded transient retry. Writes to a task's allowed files also count as activity, so a worker that is silently producing files is not stopped. Worker output is written to `.agent-loop/logs/<run>/<attempt>.jsonl` as it arrives (`tail -f` it to follow a live attempt). When a trusted check rejects an attempt, the worktree is still reset, but the attempt's allowed files are first copied to `.agent-loop/rejected/<run>/<task>/` and the next attempt is pointed at them. Plans whose task files are gitignored are rejected up front, since those files can never be committed.

`--once` performs one scheduler tick and returns, which is useful for external schedulers. The top-level loop owns only durable plan state, validation, commits, and retries. Each model call goes through `scripts/run_provider.py`, which checks that provider's quota and sleeps until a known reset before launching or retrying the CLI. `Ctrl-C` or `SIGTERM` stops the runner and retains progress. A blocked authentication or configuration error exits and requires repair; use `python3 -m loop retry RUN_ID TASK_ID`, then resume.

Plans can opt into `failure_review`. After a trusted check fails or a worker returns a terminal error, Python asks one configured checker provider whether the same immutable task should retry or block. This is advisory and runs only on failure; it cannot edit code, change the plan, or replace the trusted check.

An observed reset can be recorded explicitly:

```sh
python3 -m loop hold claude --until 1790000000 --reason 'Observed weekly reset'
python3 -m loop hold claude --until 0 --reason 'Clear manual hold'
```

The timestamp above is an example, not your account's reset time. The supervisor checks both quota windows and uses the latest exhausted window's reset. A quota error without a machine-readable reset causes a 30-minute retry delay; it does not pretend that this is the actual refresh time.

Claude Code quota telemetry is collected by the project status line into `.agent-loop/claude-quota.json`. Claude supplies the five-hour and seven-day windows after the first response for Claude.ai subscribers; if those fields are unavailable, the supervisor continues safely and falls back to quota-error detection.

`usage` reads live telemetry from all three providers before you commit to a plan: Codex's app-server, Claude's `/usage` command, and Antigravity's official headless `/usage` command. Missing telemetry is reported explicitly rather than treated as available quota.

## Operating a run

```sh
python3 -m loop status --run <run-id>              # compact task table (plain `status` is full JSON)
python3 -m loop logs <run-id> [task-id] --follow   # live output of the latest attempt
python3 -m loop supervisors                        # runs whose supervisor is running, with pid
python3 -m loop stop <run-id>                      # SIGTERM; progress is kept
python3 -m loop amend <plan>.json --budget codex=800000 --set worker_idle_timeout_seconds=900
python3 -m loop run <plan>.json --from-run <earlier-run-id>   # or --base <git-ref>
python3 -m loop clean [--yes] [--include-unpublished]
```

- `amend` applies an edited plan to an existing run (stop its supervisor first). Completed tasks are frozen and tasks can't be removed; settings, budgets, pending tasks and new tasks can change, and tasks parked on a token budget are released. `--budget`/`--set` are written back to the plan file so the next `run` matches.
- `--base`/`--from-run` (or a plan's `base_ref`) start a new run from existing work instead of whatever is checked out; resuming later needs no flag.
- `clean` removes worktrees and saved rejected files for superseded or merged runs (branches are kept). It is a dry run without `--yes`; finished-but-unmerged runs need `--include-unpublished`, because `publish` uses their worktree.

Before a new run starts, preflight rejects checks or commands whose script files aren't committed at the base, and warns when a prompt names a path that won't exist in the worktree (gitignored inputs need absolute paths). The first time a task is attempted, a check that already passes is reported as unable to verify the task; set `"preflight_checks": "skip"` to mark such tasks done without a model call.

A task with `"provider": "command"` and a `"run"` argv executes a trusted host command (a simulator capture, a build, a render) instead of a model: no quota, tokens or prompt, but the same file allowlist, trusted check and commit. Commands are registered by policy like checks, live output goes to the run's logs, and failures retry with backoff until unchanged repeats park the task.

A task's optional `review` (`{"provider": "claude", "model": ..., "instructions": "...", "attach": [...]}`) adds a model quality gate after the trusted check passes and before commit. The reviewer sees the task, the text diff and the absolute paths of changed media (images, video) to open; a decline or an unavailable reviewer fails the attempt with its reasoning, and a reviewer that edits the worktree voids its verdict. It costs one review call per attempt, so it is opt-in.

## Lite keeper loop (trial)

`loop lite` is a much smaller alternative to the plan engine for runs that should keep going
instead of parking:

```sh
python3 -m loop --repo /path/to/repo lite start "Fix the strict SEO gate failures" \
  --supervisors claude,codex,antigravity --workers codex,claude
python3 -m loop --repo /path/to/repo lite start "..." --setup "cd apps/web && npm ci" \
  --catalog models.json --supervisor-model claude=claude-opus-5
python3 -m loop --repo /path/to/repo lite status <id>
python3 -m loop --repo /path/to/repo lite tail <id>
python3 -m loop --repo /path/to/repo lite resume <id>
```

There are three layers:

- **Supervisor:** a cloud model running a read-only, resumable CLI session, using each CLI's working read-only setup: claude `--tools Read,Grep,Glob` (its plan mode made the supervisor wait for approval), codex `sandbox_mode="read-only"`, and agy `--mode plan` (agy's default headless mode denies reads too; its plan mode reads, blocks writes, and still replies). The supervisor role is sent as system/developer instructions (claude `--append-system-prompt`, codex `developer_instructions`), or in the prompt for agy. Each turn it gets the goal, plan, handoff notes and new worker results, and replies with JSON actions: `plan`, `dispatch`, `accept`, `discard`, `note`, `wait`, `done`. Any file change it makes is reverted.
- **Workers:** full coding agents in `.agent-loop/worktrees/lite-<id>` (branch `lite/<id>`). They can edit any file and run builds or installs; only delegation is disabled. When a task's `check` passes, all changes are committed. When it fails, the changes stay uncommitted and the supervisor decides what to do next.
- **Keeper:** Python rules (`loop/keeper.py`), not a model, decide after every turn:
  - **Switch** supervisors when quota or auth runs out.
  - **Wait** until the earliest reset when nothing is available.
  - **Remind**, then reset, a supervisor that edits files or pastes code.
  - **Reset** the session above `--reset-at-tokens` (default 120k).
  - **Rotate** after 4 stagnant turns.

The local Ollama model (`--keeper-model`, default `qwen2.5:7b`) only compresses worker results into handoff notes, with a deterministic fallback when Ollama is down. In testing, qwen2.5:7b chose the right action only 9 times out of 18, but extracted blockers and fix commands reliably.

Workers can only use models from a tiered catalog, and the supervisor picks a tier or an exact model for each dispatch. You can replace the catalog with `--catalog`, a JSON list of `{provider, tier, model, effort?}`.

| Tier | claude | codex | antigravity |
|---|---|---|---|
| light | claude-haiku-4-5-20251001 | gpt-5.6-luna (low) | gemini-3.8-flash-low |
| standard | claude-sonnet-5 | gpt-5.6-terra | gemini-3.8-flash-medium |
| strong | claude-opus-5 | gpt-5.6-sol | gemini-3.8-flash-high |

- **Refused models:** a model outside the catalog (such as `gpt-6-astra` or Gemini Pro) falls back to the standard tier, and the supervisor is told.
- **Retired models:** entries that `agy models` or Codex's model cache no longer list are dropped at startup.
- **History:** each task records its tier, model and outcome per attempt.
- **Codex supervisor:** runs on `gpt-5.6-sol` and moves to `gpt-6-astra` only for the fresh session after the keeper finds it stuck. `--setup` commands, such as dependency installs, run once per worktree without a model call. `lite status` shows estimated spend split into supervisor and worker.

**Keeping workers honest.** Workers optimise for passing checks, and a supervisor that trusts "passed" gets gamed. In a long real run, workers stamped claims with generic reasons, swapped in templated reasons, appended ids to make templates look unique, left stale dates on "rewritten" work, and cited DOIs belonging to unrelated papers, all while the supervisor's checks passed. The keeper now defends against this independently of the supervisor:

- **Gates** (`--gate COMMAND`, or `lite gate <id> COMMAND` mid-run): keeper-owned commands that must pass, in addition to each task's own check, before any commit and before `done`. The supervisor sees them but can't change them. Point gates at scripts kept outside the worktree so workers can't edit them.
- **Independent audit**: after the checks and gates pass, a different provider reviews the diff read-only for templated output, invented or mismatched content, gamed checks, and missing work. A `fail` verdict blocks the commit and returns its problems to the supervisor. An unavailable auditor never blocks progress. Configure it with `audit.sample`, or turn it off with `--no-audit`.
- **Template detection**: a change where one sentence pattern makes up at least 20% of its prose is flagged to the supervisor and the auditor.
- **Evidence, not verdicts**: results include a diff sample, and the supervisor role tells it to spot-check changes before relying on "passed".
- **Operator notes**: `lite note <id> "…"` reaches the supervisor's next turn without stopping the run, and stays in every fresh session's prompt.
- `--detach` runs the keeper in its own session with `keeper.log`, so it outlives the terminal or tool that started it.

All state lives in `.agent-loop/lite/<id>/` (`goal.md`, `plan.json`, `handoff.md`, `supervisor.json`, `journal.jsonl`, `logs/`), so a fresh session or a different provider picks up from files. The run ends only when `done` verifies (every task done or dropped, clean tree, all checks pass), or on Ctrl-C. Supervisor-written checks run on the host, so use this only on repositories you trust.

## Two-pass planning

Use `plan` to check all provider quotas first, have a budget model scout the repository, and have a stronger model turn those notes into a validated plan:

```sh
python3 -m loop --repo /path/to/repo plan "Fix white text on white buttons" \
  --output agent-loop-plans/button-contrast.json \
  --context-provider antigravity --context-model gemini-3.8-flash-low \
  --planner-provider antigravity --planner-model gemini-3.1-pro-high
```

The command records all live usage it can read before planning. Missing telemetry is reported, not treated as zero quota and not allowed to block an otherwise usable planner. It rejects generated plans that name target files which do not exist, then writes only the requested JSON file; inspect it, then run it normally (optionally with `--review`).

## Scope of this prototype

- Durable SQLite state, one supervisor lock per milestone, isolated Git worktrees, checks, section commits, crash reconciliation after a commit, and bounded repair attempts. Milestones with different plan ids run concurrently against the same repository; a short repository-wide lock serializes only the git plumbing that touches shared state, such as adding a worktree or fetching a PR base.
- Official headless adapters for Codex, Claude Code, and Antigravity. Workers edit their isolated worktree with normal coding tools while model delegation remains disabled. Provider sandboxes constrain execution, and the supervisor independently validates Git history, changed paths, suspicious file shrinkage, trusted checks, and commits.
- Automatic Codex quota preflight through its documented app-server API, plus Claude Code status-line quota snapshots. Antigravity exposes usage in another form; the MVP handles its quota errors and manual reset holds.
- Per-provider, per-milestone, and per-section token accounting and admission budgets. `provider_token_budgets` counts only the current plan's spend, so a concurrent milestone cannot exhaust it; a task's optional `token_budget` caps one section. Counts cover this supervisor's completed responses, not everything used by your other sessions. A running task can exceed the remaining local token budget; this is not a hard provider billing cap. Claude's reported dollar figure is an estimate, not proof of a charge.
- One milestone per plan. Completion creates a PR body and a `ready_for_pr` state. Publishing and merging are a separate explicit release command, described in [the design](docs/DESIGN.md).

After configuring `origin` to a chosen GitHub repository, publication is explicit:

```sh
python3 -m loop publish three-provider-smoke --github OWNER/REPO
python3 -m loop publish three-provider-smoke --github OWNER/REPO --merge
```

The first command pushes the milestone branch and creates/reuses its PR. The second additionally attempts a normal merge with an exact head SHA, a protected base branch, and nonempty passing required checks. It returns a waiting state when checks are incomplete; invoke it again after CI completes. No `--admin` or force push is used. The release step does not yet run automatically inside the long-lived task loop.

Run only trusted plans: setup and acceptance-check commands execute locally. Provider sandboxes reduce worker risk, but checks can execute changed application code. A task may not modify the check file it invokes. Do not put secrets in prompts or generated files.

## Optional self-review

`run --review` asks a model to sanity-check the plan itself before the first task runs — whether each task's goal looks real/needed and whether its `check` could plausibly verify its `prompt` — and aborts before touching the repo if it declines. `publish --review` asks a model to compare the finished diff against each task's `prompt` and flag anything fabricated, unrelated, or unnecessary, aborting before the branch is pushed if it declines. Both are optional and off by default; neither replaces or weakens the trusted `check` command, which remains the only thing that actually gates a section being committed. Set `review_provider`/`review_model` in the plan to pick which CLI performs the review (defaults to `claude`).

This does not install a background daemon. macOS cannot run the supervisor while the machine is asleep or powered off. For unattended operation, use `launchd` on an awake Mac or `systemd` on an always-on machine. See [the design and implementation stages](docs/DESIGN.md) and [the test report](docs/TEST-REPORT.md).
