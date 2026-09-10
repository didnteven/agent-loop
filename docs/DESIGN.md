# Design decision — 10 September 2026

## Recommendation

Build a thin custom repository around the official coding CLIs. Use a deterministic supervisor for scheduling, state, quota waits, and Git operations. Antigravity or a small model can be a planner or worker; making a model responsible for the heartbeat adds token use and another quota dependency without improving a timer.

The useful part of the Ralph pattern is the repeated bounded attempt against an externally checked task, with fresh context and persistent progress. The loop must stop doing inference when checks pass, when an account is exhausted, or when a real configuration issue needs attention. The operating-system supervisor can stay alive through those pauses.

No extra orchestration subscription is necessary. Start with the plans already owned. Whether one plan is cheaper than several depends on actual allowance, latency, and the work mix; do not buy or cancel a plan based on token counts from this small test.

## Execution architecture

```text
launchd / systemd (restart process after failure)
  └─ Python supervisor + SQLite (zero inference while idle)
       ├─ approved task plan + checks + model routing
       ├─ provider quota windows and retry deadlines
       ├─ one short worker invocation at a time
       │    ├─ codex exec
       │    ├─ claude -p
       │    └─ agy -p
       └─ milestone worktree → checks → section commits → PR → checked merge
```

Use small models for mechanically scoped changes, data conversion, tests, and documentation. Escalate a section to a stronger model after a concrete failed attempt, or use a stronger model once for architecture or final review. Avoid having three providers re-review every small change. Configure explicit model IDs and benchmark them on representative work; do not silently switch models or billing methods when a plan runs out.

The demo uses Codex Luna, Claude Haiku, and an available Antigravity Flash model. This is a routing experiment, not a claim that these always have the lowest price or sufficient quality for complex changes.

## What can actually be monitored

| Provider | Verified official interface | Prototype behavior | Remaining work |
| --- | --- | --- | --- |
| Codex | `codex exec --json`; `account/rateLimits/read` via app-server | Reads quota windows before dispatch and records token usage after a turn | Map all model-specific quota buckets and provider-credit states |
| Claude Code | `claude -p --output-format json`; status-line `rate_limits` fields | Parses real result envelopes and usage; waits on quota errors or supplied deadlines | Optional status-line collector for five-hour, weekly, and spend windows; verify headless applicability in the installed version |
| Antigravity | `agy -p --output-format json`; interactive `/usage` panel | Parses real results and token totals; waits on quota errors or supplied deadlines | Add supported machine-readable quota telemetry if available; the installed CLI has no documented standalone quota command |

Account allowance and local token totals are different things. Interactive apps and other sessions can consume the same account allowance; only provider telemetry can report that allowance. Missing data must appear as unknown, not 100% available. Honor all exhausted windows; a five-hour refresh does not restore an exhausted weekly allowance.

Do not scrape credentials or reverse-engineer private quota endpoints. Use the official CLI's normal login. Do not rotate accounts, automatically purchase credits, consume paid top-ups, or fall back to an API key as a quota workaround. Billing settings in each provider account still control how usage is charged; this supervisor cannot guarantee that a provider never uses previously enabled extra usage.

## State and recovery

Task states are pending → running → verified/committed → done. Rate limits enter waiting with a persisted deadline. Transient failures use increasing delay; code repair attempts are capped. Authentication/configuration failures become blocked and do not repeatedly call a model.

SQLite stores the immutable plan digest, task attempts, commit IDs, provider holds, observed token counts, and events. Git commit trailers identify the exact plan and section, allowing recovery if the process dies between committing and updating SQLite. File bundles are reapplied on a retry; completed sections are not regenerated.

The current runner is serial and maintains one database per target repository. A later multi-project version should use one global provider-usage registry plus independent worktrees, so two projects cannot independently overspend the same allowance. Parallel work is valuable only for independent sections and needs explicit dependency edges and shared admission control.

## Commit and PR policy

One coherent code section produces one commit after its acceptance checks pass. One milestone groups several section commits into a PR. Keep plan descriptions, acceptance checks, and release policies outside the worker's editable file allowlist.

Release sequence:

1. Rerun all milestone checks and inspect the final diff; use a stronger review agent for risky changes.
2. Push only the milestone branch to the explicitly configured repository.
3. Find or create a PR by repository, head branch, and base branch; use the prepared body file. Reuse the same PR on retries.
4. Wait for required CI checks and reviews at the exact branch SHA. Missing checks are not passing checks.
5. Enable a normal merge only after the repository's branch protection gates apply; never use an admin bypass. Prefer merge commits if retaining individual section commits matters.
6. Confirm the PR is merged before marking the milestone merged and starting a dependent milestone from the refreshed base.

The included `publish` command handles branch push, PR creation/reuse, checks, and exact-head merge when explicitly requested. A real remote was not selected in this conversation, so live GitHub publication is not part of the local smoke test. The current prototype stops at `ready_for_pr` by default. This is not represented as a merged milestone.

## Automation intent

The design goal is that once a plan is authored and started, it runs to completion — implement, check, commit, repeat, publish — without stopping for interactive confirmation at each step. The trusted `check` command in each task *is* the automated review gate: a section is never accepted on a model's say-so, only on a check passing. There is deliberately no additional "please confirm this generated code looks right" prompt inside the supervisor loop itself; that would reintroduce a human bottleneck the design is meant to remove.

This intent has one hard boundary: irreversible, externally visible actions — pushing a branch, opening a PR, merging — are git/GitHub operations, not supervisor state, and remain gated by whatever confirmation layer the calling tool (e.g. Claude Code's own permission system) enforces around `git push`, `gh pr create`, or `gh pr merge`. Agent-loop's own `publish` command does not add its own extra confirmation on top of that; it performs the push/PR/merge sequence directly once invoked, subject to the safety checks in [Commit and PR policy](#commit-and-pr-policy) (ancestor-of-base check, protected branch requirement for `--merge`, exact-head match). If a plan's milestone branch was built off a branch other than the intended PR base, pass the real base explicitly with `--base`; the ancestor check exists to stop a PR from silently landing on the wrong base, not to add a review step.

## Implementation stages

| Stage | Deliverable | Gate |
| --- | --- | --- |
| 1: local prototype | Three real CLI adapters, persisted wait/resume, checks, commits, PR preparation | All three complete a basic coding task; simulated quota and crash tests pass |
| 2: release integration | Exercise publication against a dedicated private test repository, CI and protected base branch | No duplicate PR after restart; failing/missing checks prevent merge; merged state verified |
| 3: long-running service | OS supervisor configuration, heartbeat, global provider registry, notification on real blocks | Survives reboot, a worker crash, and a simulated long quota wait |
| 4: richer agent work | Tool-capable workers in external sandboxes, explicit dependency DAG, planning and review roles | Representative tasks show better total cost per accepted milestone |

Keep stage 1 small. A framework becomes more attractive for distributed workers, several users, shared credentials, complex workflow versions, or a substantial dashboard. Those are not needed to prove this personal workflow.

## Operational limits

- No unattended service is activated by this project setup.
- Locking is per milestone, not per repository: concurrent milestones are isolated by their own worktrees and by a short repository-wide lock around shared git plumbing. It does not coordinate other user-launched agents, and two plans that declare overlapping `files` will still conflict at merge time rather than while running.
- Codex quota reads are live; Claude/Antigravity proactive account telemetry is not implemented.
- The timeout bounds a worker process, but cannot cancel requests already accepted by a remote provider with certainty.
- Crashes before a model response is durably recorded can cause a repeated request and uncounted partial usage. Git commits are reconciled; model billing is not exactly-once.
- A crash during initial worktree creation, before the run row is saved, requires inspecting the preserved worktree before initializing again.
- Code checks must be trusted and meaningful. The worker cannot edit the demo's verifier, but generated code still executes in that verifier's process.
- API-priced estimates do not establish subscription invoices or a universal five-hour token budget.

## Primary sources

- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode): official headless execution and JSON events.
- [Codex app-server](https://learn.chatgpt.com/docs/app-server): official quota windows, reset timestamps, and usage RPCs.
- [Claude Code programmatic execution](https://code.claude.com/docs/en/headless): print mode, output envelopes, permissions, and estimated costs. Fetched page takes precedence over stale search snippets about billing changes.
- [Claude Code status-line fields](https://code.claude.com/docs/en/statusline): optional five-hour, seven-day, and spend-limit telemetry.
- [Antigravity headless mode](https://antigravity.google/docs/cli/headless/): native terminal execution and JSON result contract.
- [Antigravity installation](https://antigravity.google/docs/cli/install/): normal cached-login authentication.
- [Antigravity model quotas](https://antigravity.google/docs/cli/commands/usage/): interactive quota panel; not a headless quota API.

Local `--help` and real invocation results determine which flags work in the installed clients. These interfaces can change independently of this repository.
