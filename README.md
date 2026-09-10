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
```

The repository must have an initial commit and a clean working tree before a new milestone starts. The supplied plan asks three providers to implement a tiny Python `clamp` function. Each implementation must pass 11 independent acceptance cases before the supervisor commits it. A final pass checks the entire milestone.

Generated code lives in `.agent-loop/worktrees/three-provider-smoke`, on branch `loop/three-provider-smoke`. Runtime state, quota observations, raw output, and the prepared PR body live under `.agent-loop/` and are ignored by Git. The same command resumes a saved plan and skips completed sections. Changing a saved plan requires a new plan ID.

`--once` performs one scheduler tick and returns, which is useful for external schedulers. Without it, quota waits continue without model calls. `Ctrl-C` or `SIGTERM` stops the runner and retains progress. A blocked authentication or configuration error waits for repair; stop the runner, fix the issue, use `python3 -m loop retry RUN_ID TASK_ID`, and resume.

An observed reset can be recorded explicitly:

```sh
python3 -m loop hold claude --until 1790000000 --reason 'Observed weekly reset'
python3 -m loop hold claude --until 0 --reason 'Clear manual hold'
```

The timestamp above is an example, not your account's reset time. The supervisor checks both quota windows and uses the latest exhausted window's reset. A quota error without a machine-readable reset causes a 30-minute retry delay; it does not pretend that this is the actual refresh time.

## Scope of this prototype

- Durable SQLite state, one supervisor lock per milestone, isolated Git worktrees, checks, section commits, crash reconciliation after a commit, and bounded repair attempts. Milestones with different plan ids run concurrently against the same repository; a short repository-wide lock serializes only the git plumbing that touches shared state, such as adding a worktree or fetching a PR base.
- Official headless adapters for Codex, Claude Code, and Antigravity. Workers return a JSON file bundle; the supervisor validates the file paths, applies the code, runs checks, and commits. This intentionally small first version does not give workers an unrestricted shell.
- Automatic Codex quota preflight through its documented app-server API. Claude and Antigravity expose usage in other forms; the MVP handles their quota errors and manual reset holds. It does not claim to read their full account allowance automatically.
- Per-provider, per-milestone, and per-section token accounting and admission budgets. `provider_token_budgets` counts only the current plan's spend, so a concurrent milestone cannot exhaust it; a task's optional `token_budget` caps one section. Counts cover this supervisor's completed responses, not everything used by your other sessions. A running task can exceed the remaining local token budget; this is not a hard provider billing cap. Claude's reported dollar figure is an estimate, not proof of a charge.
- One milestone per plan. Completion creates a PR body and a `ready_for_pr` state. Publishing and merging are a separate explicit release command, described in [the design](docs/DESIGN.md).

After configuring `origin` to a chosen GitHub repository, publication is explicit:

```sh
python3 -m loop publish three-provider-smoke --github OWNER/REPO
python3 -m loop publish three-provider-smoke --github OWNER/REPO --merge
```

The first command pushes the milestone branch and creates/reuses its PR. The second additionally attempts a normal merge with an exact head SHA, a protected base branch, and nonempty passing required checks. It returns a waiting state when checks are incomplete; invoke it again after CI completes. No `--admin` or force push is used. The release step does not yet run automatically inside the long-lived task loop.

Run only trusted plans: the acceptance-check commands execute locally. Path restrictions protect file application; they are not an OS sandbox for the acceptance tests. Do not put secrets in prompts or in generated files.

## Optional self-review

`run --review` asks a model to sanity-check the plan itself before the first task runs — whether each task's goal looks real/needed and whether its `check` could plausibly verify its `prompt` — and aborts before touching the repo if it declines. `publish --review` asks a model to compare the finished diff against each task's `prompt` and flag anything fabricated, unrelated, or unnecessary, aborting before the branch is pushed if it declines. Both are optional and off by default; neither replaces or weakens the trusted `check` command, which remains the only thing that actually gates a section being committed. Set `review_provider`/`review_model` in the plan to pick which CLI performs the review (defaults to `claude`).

This does not install a background daemon. macOS cannot run the supervisor while the machine is asleep or powered off. For unattended operation, use `launchd` on an awake Mac or `systemd` on an always-on machine. See [the design and implementation stages](docs/DESIGN.md) and [the test report](docs/TEST-REPORT.md).
