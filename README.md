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

`--once` performs one scheduler tick and returns, which is useful for external schedulers. The top-level loop owns only durable plan state, validation, commits, and retries. Each model call goes through `scripts/run_provider.py`, which checks that provider's quota and sleeps until a known reset before launching or retrying the CLI. `Ctrl-C` or `SIGTERM` stops the runner and retains progress. A blocked authentication or configuration error exits and requires repair; use `python3 -m loop retry RUN_ID TASK_ID`, then resume.

Plans can opt into `failure_review`. After a trusted check fails or a worker returns a terminal error, Python asks one configured checker provider whether the same immutable task should retry or block. This is advisory and runs only on failure; it cannot edit code, change the plan, or replace the trusted check.

An observed reset can be recorded explicitly:

```sh
python3 -m loop hold claude --until 1790000000 --reason 'Observed weekly reset'
python3 -m loop hold claude --until 0 --reason 'Clear manual hold'
```

The timestamp above is an example, not your account's reset time. The supervisor checks both quota windows and uses the latest exhausted window's reset. A quota error without a machine-readable reset causes a 30-minute retry delay; it does not pretend that this is the actual refresh time.

Claude Code quota telemetry is collected by the project status line into `.agent-loop/claude-quota.json`. Claude supplies the five-hour and seven-day windows after the first response for Claude.ai subscribers; if those fields are unavailable, the supervisor continues safely and falls back to quota-error detection.

`usage` reads live telemetry from all three providers before you commit to a plan: Codex's app-server, Claude's `/usage` command, and the local `antigravity-usage` utility. Install the latter once and set `AGENT_LOOP_ANTIGRAVITY_USAGE_DIR` if it is not at `/tmp/antigravity-usage`; missing telemetry is reported explicitly rather than treated as available quota.

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
