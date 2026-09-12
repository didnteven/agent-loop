# Roadmap — unattended execution and measured self-improvement — 12 September 2026

This document specifies the remaining work to turn the bounded milestone runner into an
unattended job system that resolves ordinary plan and code questions itself, records its
decisions, and improves future runs from measured outcomes. It extends [DESIGN.md](DESIGN.md)
stages 3 and 4 and schedules stage 5. Where the documents differ, this roadmap supersedes the
older requirements for mandatory repair caps, manual plan repair, and operator-driven
improvement. The implementation status is tracked in ROADMAP-NOTES.md; this document specifies the target behavior.

## Intended behavior

Once given an objective and a configured execution policy, the service selects work, authors
and validates plans, implements changes, checks them, repairs failures, publishes under the
configured release policy, records outcomes, and learns. Ordinary uncertainty must lead to a
reasoned choice and a decision record, never a request for human confirmation.

Strict token budgets are **optional**. The default is mandatory usage measurement and waste
prevention: keep productive work moving, change an ineffective strategy, and stop paying for
repeated attempts that have no new evidence. Provider quota limits still apply. This mode
does not promise a fixed maximum token bill.

The supervisor remains deterministic Python. Scheduling, idle waits, accounting, failure
deduplication, and routine lesson extraction make no model calls. One implementation worker
is the normal task path; planning and exceptional repair calls are separately measured.
Worker sub-agents are disabled because their additional context and coordination can duplicate
work. Enforce this through available provider tool restrictions as well as prompt instructions;
verify each supported provider's behavior. The initial unattended mode does not enable worker
delegation.

## What already exists

Present in `loop/engine.py`, `loop/adapters.py`, and `scripts/run_provider.py`:

- SQLite state with WAL and per-milestone locking;
- a supervisor-owned worktree per milestone and declared one-time `setup`;
- trusted checks, file allowlists, and a commit per accepted task;
- commit-marker reconciliation after a crash;
- provider-boundary quota polling and reset waiting;
- traversal, symlink, truncation, and managed-history checks;
- immutable plan digests and one implementation worker on the ordinary task path.

These are useful foundations. They do not yet constitute a daemon, an autonomous replanner,
or an applied learning loop.

## Deployment and state

One installation orchestrates many target repositories. The **target repository** contains
the product being changed; **this repository** contains the orchestrator. Worker changes to
either happen in managed worktrees, and executing supervisor code comes from a pinned,
separate installation.

### Fix cross-repository initialization first

1. `Engine.initialize()` must call `validate_plan(plan, self.repo)`. Its current default root
   is the process working directory, which can be the agent-loop checkout instead of the
   target. `planner.validate_generated_plan()` already passes the target root.
2. Creating `<target>/.agent-loop/` currently makes an otherwise clean target appear dirty
   unless it already ignores that directory. Add the exclusion through Git's resolved
   `info/exclude` path, including support for linked worktrees; do not assume `.git` is a
   directory or change the target's committed `.gitignore`.
3. Recover initialization interrupted between worktree creation and database insertion by
   verifying recorded intent, branch, base SHA, and workspace ownership. Adopt a matching
   workspace automatically; isolate a conflicting artifact without deleting user work.

### Separate telemetry from accounting

- **Target-local:** plans and lineage, tasks, attempts, decisions, events, worktrees, results,
  and logs.
- **Account-global:** provider holds, health, cross-target lessons, and aggregate usage in
  `~/.agent-loop/registry.sqlite`. Key provider state by provider, account identity, and quota
  bucket without storing credentials.
- **Disposable telemetry:** health estimates and summaries can be rebuilt. Missing telemetry
  is unknown; it cannot skip checks or assert quota is available.
- **Durable accounting:** attempt usage, unresolved reservations, and configured limits cannot
  be discarded as a cache. With a cap enabled, reserve allowance atomically before dispatch,
  then reconcile actual usage. Concurrent targets share admission control.

In default monitoring mode, telemetry failure records degraded monitoring and uses conservative
routing and concurrency. Under an explicit cap, unresolved accounting waits for automatic
recovery; it must not silently reopen allowance. A registry problem must not stop reaping
workers, preserving results, or checking already completed work.

## Autonomous decisions and plan repair

### Standing execution policy

Resolve scope once through configuration and the original objective. The policy describes
allowed repositories and path roots, protected paths and checks, dependency sources and setup
operations, provider/model choices, optional limits, and release destinations and behavior.
Workers and learned lessons cannot expand this policy.

Within that scope, choose ordinary code and plan details automatically. Prefer explicit
requirements, then established repository conventions, then the simplest reversible approach
that satisfies the objective. Inspect relevant code before guessing. Use a stronger planning
call only when available evidence does not support a workable choice. Do not ask a person
to select a library, file layout, repair strategy, or implementation detail covered by policy.

Record decisions durably as:

```text
decision_id, objective_id, plan_id, task_id, question, alternatives,
chosen_option, reason, evidence_refs, policy_version, validation_result
```

Keep explanations short. Include material assumptions and decisions in the milestone report
and PR body.

**Question detection.** A worker reply is classified `asked_question` when the attempt produced
no diff in the allowlisted files and the response text ends with a question mark or contains a
request for confirmation (`should I`, `do you want`, `which option`, `please confirm`). This is
a distinct outcome from `no_change`. On the first occurrence, re-dispatch once with the decision
rule above and the relevant evidence appended. On the second occurrence with the same failure
fingerprint, change strategy (stronger model or successor plan); never wait for a person.

**Plan review is advisory.** A declined `--review` verdict does not end the run. Feed its
reasoning into one bounded plan-repair call, validate the result, record both verdicts as a
decision, and proceed with the best validated plan. Failure review remains advisory in the same
way: a negative verdict selects the next strategy, it does not block.

**Park, never exit.** The current runner exits with code 2 on any `blocked` task, which ends
unattended execution. Replace it with `parked` at the task level: a parked task is skipped by
the scheduler, its reason and wake condition are persisted, and the run continues with other
eligible tasks. `blocked` remains only for environment faults that make the worktree unusable
(branch changed, unexpected history). This change lands in step 1, before any other autonomy
work, so the first unattended runs do not stop on the first exhausted task.

### Repair plans without mutating started plans

Allow automatic decomposition, dependency-edge correction, new files, and task allowlist
adjustments within standing policy. Replace `validate_generated_plan()`'s blanket rejection
of new files with validation of allowed roots, parent paths, symlink containment, and protected
surfaces, and delete its docstring sentence calling new files "an explicit human decision". New files need a trusted verification path; a worker must not manufacture or weaken
its own acceptance gate. Preserve the current check/allowlist overlap rejection and extend
protection to the check's imported helpers and configuration, not just its argv text.

Create a validated successor plan with a new id, parent id, reason, and objective identity.
Keep the original digest immutable. Carry accepted commits and cumulative usage forward;
revalidate carried work where dependencies or integration changed. A new plan id must never
reset waste detection, failed-strategy history, or an explicitly configured lifetime cap.

Bound replan cycles by material progress and strategy novelty. Renaming tasks, changing prompt
wording, or splitting a failed task into equivalent children is not a new approach. Use an
objective-level failure fingerprint to prevent endless reauthoring.

## Worker lifecycle and scheduling

### Detached, recoverable workers

Move prompt and provider argv assembly into the worker boundary. The supervisor passes a
validated task record, journal reference, attempt identity, and execution policy. It does
not require a model to create or interpret scheduling decisions.

Persist launch intent before spawning. Record an attempt UUID, PID, process start identity,
workspace, base SHA, lease, heartbeat, and result path. A PID or `kill -0` alone cannot prove
ownership or progress. Workers write durable usage events and an atomic final result with
the same identity. Reap each result once; reject stale or mismatched results.

Define recovery for crashes before spawn, after spawn but before PID recording, after result
write, and after commit but before task-state update. Reconcile an existing worker before
launching another. Only terminate verified owned process groups; bounded runtime and stalled
heartbeats trigger recovery. Local reconciliation cannot guarantee exactly-once remote billing.

The provider boundary must report every invocation and quota wait durably. The current
`run_provider.py` retries known quota failures internally and forwards only its final result;
that hides intermediate usage and delays scheduler fallback. Report a wait to the scheduler
and release the execution slot instead: the runner exits with a distinct `provider_wait` result
carrying the deadline, and the engine records a hold without counting an attempt and without
invoking failure review. One layer owns each retry, and every new invocation passes admission
and waste checks. A runner wait misclassified as a crash would add inference calls, so the
result type must be explicit, not inferred from an exit code.

### Start with simple isolation

Add validated `depends_on` edges, cycle detection, fair selection of eligible work, and
separate waiting tasks from runnable tasks. Preserve sequential semantics for existing plans
without explicit dependency information.

Initially allow **one active worker per milestone worktree**, with bounded concurrency across
isolated milestones and targets. A waiting task releases its slot so another independent task
can run. Never start multiple workers in the same worktree, even with disjoint allowlists:
cleanup, checks, Git staging, and commits affect shared state.

Parallel tasks within a milestone are a later optimization requiring separate task worktrees,
pinned dependency commits, serialized integration, conflict repair, and checks on the combined
result. Enable it only after measurements show a benefit. Scheduler-managed independent jobs
are distinct from worker-created sub-agents, which remain disabled.

## Failure handling without routine human decisions

Use separate provider statuses (`ok`, `rate_limited`, `transient`, `error`, `auth`) and task
outcomes such as `check_failed`, `needs_setup`, or `scope_mismatch`. A provider can return `ok`
while its code fails a check. Fallback rules must distinguish these cases explicitly.

| Situation | Automatic next move |
| --- | --- |
| Ambiguous plan or code choice | Inspect evidence, choose within policy, record the assumption, implement and check |
| Worker replies with only a question | Classify `asked_question`; answer once with the decision rule and evidence; change strategy on repeat |
| Plan review declined | One bounded repair call with the reviewer's reasoning; proceed with the best validated plan and record both verdicts |
| Invalid generated plan | Return precise validation errors to a bounded repair call; detect repeated invalid output |
| Check failed | Diagnose the failure class and select a materially different repair strategy |
| Milestone regression | Identify likely causal changes and create a repair task; do not assume the failing test's original task caused it |
| Narrow allowlist or required new file | Create a validated successor plan within allowed roots |
| Missing dependency | Run policy-allowed setup in the managed environment; record the dependency and setup result |
| No change | Run trusted checks and verify objective satisfaction; record a justified already-satisfied outcome or change strategy |
| Transient failure or quota wait | Use an eligible fallback or persist a deadline and release the slot |
| Credential failure | Try an authorized alternative; otherwise persist an `auth` hold whose wake condition is a periodic non-inference probe (`codex login status`, `claude auth status`, or the provider's equivalent) at a learned interval, default 15 minutes, with no upper bound on waiting |
| No useful strategy remains | Attempt meaningful replanning once for the same evidence, then park without repeated inference |
| Action outside standing policy | Choose an in-policy alternative; if none satisfies the objective, record inability and continue other work |

An already-satisfied task records its verified SHA and evidence without manufacturing a commit.
An optional failure-review call advises repair or strategy selection; a negative verdict does
not introduce human approval. Deterministic diagnosis comes first. Do not call a review model
again for an unchanged failure fingerprint.

### Escalation follows evidence

Track provider, model, effort, failure class, fingerprint, relevant repository state, and
attempted approach. A retry on the same model is useful only with new diagnostics or a concrete
correction. Skip directly to setup repair, replanning, or a stronger model when warranted;
do not pay for every effort tier mechanically.

Cold-start default: the first attempt of any task uses the cheapest configured model at `low`
effort unless the plan sets a model explicitly. Escalation happens only on a classified failure,
never on attempt count alone. The fixed `max_attempts` counter is retired only after the failure
fingerprint (step 3) exists, so there is no window with neither control.

Provisional default: two consecutive occurrences of the same failure with no material progress
trigger a strategy change; repeated failure after meaningful replanning parks that objective.
These are deterministic anti-loop controls, not lifetime token caps. Calibrate them against
representative runs. Progress means accepted work, an objectively improved diagnostic result,
or a resolved prerequisite, not a worker's claim or a larger diff.

`parked_no_progress` is an explicit unsuccessful outcome, not a hidden question for a human
or a completed objective. Continue other work. Reconsider only on relevant new evidence:
dependency changes, a matching validated lesson, a newly available configured model, or a
changed environment. Time passing alone must not restart a known futile coding cycle.

Waits carry concrete wake conditions and persisted deadlines. Auth recovery must not rely on
another task succeeding when all tasks are waiting. Quota and setup recovery return work to
`pending`; they never bypass acceptance checks.

## Token efficiency and waste monitoring

### Default: measure and intervene without a hard spending ceiling

Record every model invocation: planning/scouting, implementation, repair, review, improvement,
and provider retries. Track input, output, cache usage where exposed, estimated monetary cost
where available, duration, provider/model/effort, objective and attempt identity, and outcome.
Unknown usage remains unknown. Preserve reservations or conservative estimates after a crash;
do not report missing results as zero tokens. Use provider-specific accounting to avoid
double-counting cached tokens.

Optimize **total tokens per accepted task and per accepted milestone**, compared within similar
task classes and subject to completion quality. Include failed attempts and planning overhead.
Also report first-attempt acceptance, repair/replan count, repeated fingerprints, context size,
no-progress tokens, and completion rate. Fewer tokens achieved by abandoning more tasks is
not an improvement. Subscription allowance, token totals, and monetary cost are distinct.

| Waste signal | Default intervention |
| --- | --- |
| Same failure and approach recur without new evidence | Suppress duplicate invocation; change strategy or park |
| Growing context or journal | Retrieve relevant excerpts and compact deterministic notes before dispatch |
| Model review repeats an unchanged diagnosis | Reuse the recorded diagnosis |
| Model routinely succeeds only after an expensive fallback | Route comparable future tasks to the effective strategy earlier |
| Task token use is unusually high relative to comparable successful work | Assess progress deterministically; continue productive work or repair scope/strategy |
| Replanning or learning spends more than its measured benefit | Reduce cadence, deduplicate, and suspend ineffective improvements |
| Worker tries to delegate | Deny the tool, record the event, and use one worker |

Warnings alone are insufficient: repeated-waste signals must change behavior. A large but
productive task should not stop merely for exceeding an arbitrary token number. Persist
progress and usage during long attempts; enforce per-invocation timeouts and supported tool
limits so waste control does not depend solely on a worker eventually returning.

### Bounded context

The current 30,000-character limit per context file does not bound the whole prompt. Add a
combined model-aware context allowance covering task instructions, excerpts, decisions, failure
summaries, and lessons, with space reserved for output. This per-call context bound is separate
from an optional cumulative spending cap.

Keep full logs on disk. Supply a compact journal with failed strategies, useful evidence,
completed setup, and decisions; retrieve relevant code excerpts and a small number of applicable
lessons. Do not replay transcripts or use a model to summarize every tick. Reuse repository
scouting while its referenced content is unchanged. Keep one context mechanism based on the
existing task prompt and context references.

### Optional strict limits

Retain explicitly configured per-task/objective lifetime limits and rolling account/provider
limits; omission means monitoring mode, not an invented default cap. A rolling limit yields
`waiting` until allowance returns. A lifetime limit yields a recorded limit-reached outcome
and does not reset daily or through successor plans. Continue unrelated eligible work.

Under configured caps, atomic reservations precede all model calls, including planning and
improvement. Reconcile reservations exactly once and retain uncertain usage across recovery.
Enforce provider-side request limits where supported; otherwise report possible in-flight
overshoot rather than claiming an exact bound that the CLI cannot enforce.

## Online tuning and routing

Aggregate health by account/bucket, provider, model, effort, and broad task class. Preserve
rolling samples or histogram data sufficient to recompute duration quantiles and recovery
times; lifetime counts and a stored p90 alone are insufficient.

Use minimum sample counts, age decay, floors, ceilings, and deterministic cold-start defaults.
Learn unknown-quota probe delays from recovery observations, worker timeouts from execution
duration, and transient backoff from recent recovery times. Honor supplied quota reset
deadlines. Separate provider waiting from execution, and include timeouts as censored
observations so short timeouts do not train themselves to become shorter.

Routing selects among configured providers/models with an acceptable success rate, preferring
lower expected total tokens to acceptance, then considering monetary cost and availability.
Quota constrains eligibility; plentiful quota alone does not make a model efficient. Missing
telemetry is unknown and permits a conservative bounded probe when policy allows. Record the
selection reason. Do not introduce undeclared models, billing methods, or accounts.

Compare tuning changes with a stable baseline, limit changes per observation period, and
revert degraded settings automatically. A poor setting does not necessarily self-correct
without this mechanism.

## Applied learning and repository self-improvement

### Learn incrementally and use the result

Schedule `loop learn` after completed attempts and on a low-frequency service cadence. It
updates learning tables from new events using per-target cursors and unique event identities;
it makes no model calls and does not rescan all transcripts every cycle. Inspect referenced
log excerpts only when structured events are insufficient.

Fingerprint errors by removing incidental timestamps, temporary roots, and volatile IDs.
Preserve semantic differences such as error codes, test names, and relevant relative paths.
Store example references to audit mistaken grouping. Reprocessing an event cannot count as
another occurrence.

Use typed lessons for setup recipes, failed strategies, routing, planning context, and adapter
defects. Retrieve lessons matching repository characteristics, task class, provider, model,
or failure. Treat logs and lessons as evidence, never authority to alter policy or checks.
Start with at least three independent occurrences across two independent objectives for generalized changes;
successor retries of one failure do not qualify as independent corroboration.

```text
lessons(id, fingerprint, signal_kind, scope, occurrences, first_seen, last_seen,
        example_event_ids, proposed_action, confidence, status, policy_version,
        proposed_plan_id, baseline_metrics, observed_metrics, resolution_note)
```

Track `observed → candidate → trial → active`, with `rejected`, `retired`, and `rolled_back`
outcomes. A trial uses all assigned comparable tasks, including failures and parks, with
known invocation accounting and a frozen baseline. At 10 acceptances or 14 days, evaluate
only with at least 10 terminal tasks: total workload tokens per acceptance must not increase
and completion rate must not drop by more than 5 percentage points. Insufficient evidence
never promotes; retire inconclusive trials at the configured maximum observation period.
Record each lesson's application and subsequent result. Successful behavior and
failed strategies both matter. Reconsider rejected lessons only with materially new evidence
or a relevant version change. Disable lessons that increase failures or waste.

### Improve code automatically when evidence warrants it

`loop improve` is scheduled, not dependent on an operator reading reports. Prefer a validated
configuration or context lesson when that resolves the problem. Generate a code-improvement
milestone only for recurring evidence needing code changes. Deduplicate proposals, allow one
improvement trial at a time, and prioritize requested target work. Include all improvement
calls in usage and waste assessment.

Signals are hypotheses, not diagnoses: out-of-scope edits may indicate a bad allowlist or a
noncompliant worker; timeouts may indicate slow tasks or broken adapters. Verify causes before
editing defaults. Do not relax truncation or acceptance protections just to lower failure rates.

Execute changes in an isolated worktree driven by a pinned installation. Existing immutable
checks plus an independent regression fixture must demonstrate the intended fix. New tests
can be added outside protected acceptance files, but candidate-authored tests cannot be the
sole promotion evidence. Fence `safe_path`, `validate_worker_changes`,
`validate_managed_history`, `validate_plan`, their enforcement call paths, release policy,
accounting limits, and the independent test/evaluation harness from improvement workers.
Retain protection of `tests/test_loop.py`. Enforce these boundaries in code, not only prompts.

### Automatic evaluation, promotion, and rollback

Build a separate versioned installation. Evaluate it against the pinned baseline on held-out
representative fixtures: completion quality, regression rate, recovery behavior, and total
tokens per accepted milestone. Use offline replay where sufficient and bounded real trials
only when necessary. Predefine comparison thresholds; missing evidence means no promotion.
Keep the current version and continue work without asking about an inconclusive candidate.

Where standing release policy authorizes publication and merge, create/reuse the PR, require
checks at the exact candidate SHA and all repository-required gates, and merge normally.
Never bypass branch protection. Repositories requiring human review cannot provide fully
automatic merging until their policy is configured accordingly. Do not label PR creation as
completed deployment. Local-only mode can evaluate and promote a versioned local build under
its configured policy without requiring a remote PR.

Start a canary service after candidate gates pass. Assign each run to a fixed supervisor
version; existing runs finish on that version. Preserve compatible state formats or an
independently tested migration and rollback path. Route new runs to the candidate after a
successful canary. On regression, route new work back to the retained baseline, reconcile
candidate-owned workers, and record a rejected or rolled-back lesson. Never rewrite executing
supervisor files or roll back by deleting target work.

## Long-running service and release lifecycle

Add a durable objective queue and a launchd `KeepAlive` service with heartbeat and crash
backoff. Recovery scans runnable work, owned workers, unprocessed results, pending releases,
and wake conditions. Deterministic ticks and waits consume no inference. Sleep when no work
is eligible; do not invent tasks to remain busy.

The lifecycle is explicit:

```text
objective → scout/plan → validate/repair → schedule → implement/check/repair
          → integrate/regression-check → publish/merge if configured
          → observe outcome → learn/apply → next queued objective
```

`ready_for_pr` is intermediate when automatic release is configured. Persist push, PR identity,
expected SHA, CI deadlines, merge confirmation, and refreshed base state. Reuse the PR on retry.
Route actionable CI failures into repair, invalidate approvals/check results after head
changes, and start dependent milestones only from the confirmed intended base. Represent local
completion, published PR, merged work, and deployed improvement distinctly.

Credential outages, unavailable dependencies, external permissions, or optional hard limits
can prevent completion. Persist the exact reason and an applicable automatic wake condition;
continue other work. No ordinary plan or code uncertainty should become `needs_decision`.
An unsuccessful objective stays visibly unsuccessful even while the service is healthy.

## Delivery sequence

| Step | Deliverable | Acceptance gate |
| --- | --- | --- |
| 1 | Cross-repository validation, exclusion, initialization recovery, park-and-continue | Fresh target and linked worktree initialize; interrupted initialization recovers; a parked task does not stop other tasks or exit the process |
| 2 | Standing autonomy policy, decisions, question detection, advisory reviews, successor-plan validation | Ambiguous choice, worker question, declined review, and required new file are handled and documented without user input |
| 3 | Invocation accounting, waste fingerprints, `provider_wait` runner result, optional cap reservations | Every call type is attributed; duplicate failures are suppressed; a runner wait is not an attempt; capped admission is atomic; `max_attempts` retired |
| 4 | Detached workers, worker-owned prompts, durable intermediate results | Crash injection preserves ownership and rejects stale results |
| 5 | Dependency scheduler, automatic setup/repair, evidence-based escalation | Waiting tasks do not stop independent work; replanning cannot reset history |
| 6 | Global provider recovery, bounded context, measured routing/tuning | Quota/auth waits recover without another successful task; context is bounded; quality holds |
| 7 | Objective queue, launchd recovery, automatic configured release | Reboot resumes work and reuses PRs; dependent milestones use confirmed base state |
| 8 | Incremental learning, scoped retrieval, trial/retirement feedback | A lesson improves a later run; ineffective lessons are disabled |
| 9 | Isolated code improvement, evaluation, canary, promotion/rollback | Eligible improvement becomes active automatically; a bad candidate rolls back without losing target work |
| 10 | Optional parallel tasks in separate worktrees | Integration remains correct and measured benefit justifies added complexity |

Steps 1–7 deliver unattended task execution; steps 8–9 deliver applied self-learning. Code
self-improvement is scheduled after isolation and evaluation prerequisites, rather than being
indefinitely deferred until someone chooses to activate it.

## End-to-end verification

Use simulated providers and a fake clock for recovery and failure scenarios. Use a small
representative real workload to measure efficiency; mocks cannot establish token savings.
Compare the same workload and acceptance criteria with the current runner, counting planning,
failed attempts, reviews, and learning overhead.

- An implementation question is resolved, checked, and explained without user input.
- An invalid plan, missing file, or allowed dependency triggers repair; accepted work and
  cumulative usage survive successor plans.
- A repeated failure changes strategy and then parks if no useful approach remains, with no
  daily automatic replay. Relevant new evidence can resume it.
- Large productive work continues with budgets omitted. Optional rolling limits wait and
  resume; optional lifetime limits persist across restarts and successor plans.
- Simultaneous targets cannot reserve the same capped allowance. Missing usage is visible,
  and intermediate provider retries, reviews, and planning are included.
- Workers cannot invoke sub-agents. Journals and lesson retrieval stay within context bounds.
- A hung worker, reused PID, stale result, or supervisor crash cannot cause duplicate local
  acceptance or indefinite occupation of every execution slot.
- All credentials being unavailable causes monitored waiting; recovery needs no successful
  task elsewhere and uses non-inference health checks where supported.
- Learning does not duplicate occurrences, and applied lessons change later behavior with
  measured results. Failure grouping preserves distinct underlying causes.
- Release reuses PRs and checks the exact SHA. Candidate evaluation and canary failure retain
  or restore the baseline while preserving target work.
- Completion quality holds while total tokens per accepted milestone improve or remain within
  predefined acceptable overhead for demonstrated recovery gains.

## Limits

No fixed token ceiling is promised in default monitoring mode. No system can guarantee every
objective completes, credentials repair themselves, or remote model billing is exactly-once.
Anti-loop controls must prevent repeated futile inference independently of budgets.

Learned estimates are uncertain with a small corpus. Keep conservative defaults, evaluate
changes, preserve negative results, and roll back poor behavior. Token efficiency is an
observed workload result, not a claim established by adding a lessons table.

This roadmap activates no service and publishes no changes. Implementation must consume the
configured execution and release policy. Ordinary decisions within that policy are automatic;
policy and trusted acceptance boundaries remain outside worker control.

## Appendix: implementation contract

This corrected contract supersedes the earlier steps 1–5 appendix. Implement in order,
with a passing full suite at each coherent change. The standard-library gate is
`python3 -m unittest discover -s tests -q`; `python3 -m pytest tests -q` is equivalent
when pytest is installed. No new runtime dependencies are required.

The approved original objective and separately supplied execution policy are authority.
Generated plans and repairs are proposals. Never accept a generated policy as authority.
An explicitly supplied initial plan is trusted input within installation policy; its policy
is frozen before planning/review/repair and successors may only narrow it. Store canonical
policy JSON and its SHA256 version independently of generated plan content.

### State contracts

| Entity | States and transitions |
| --- | --- |
| Objective | queued → planning → running → locally_complete → published → merged; running → waiting/parked/unsuccessful; only relevant evidence wakes parked work |
| Task | pending → running → done; running → pending/waiting/parked; done requires a verified SHA, including already-satisfied work; dependencies must be done before dispatch |
| Attempt | intended → registered → running → result_written → reaped; uncertain ownership → recovering; verified dead ownership → stale; stale results are never accepted |
| Reservation | reserved → reconciled or uncertain; reconciliation is conditional and idempotent; uncertain usage retains allowance until recovered |
| Release | pending → pushed → pr_open → waiting_for_checks → merge_pending → merged; changed head invalidates prior evidence |

`blocked` denotes a worktree environment fault, not a code question. A foreground `run`
command may return 3 for a fully parked/unsuccessful run and 2 for an environment fault.
The service persists both outcomes and continues other objectives; it never exits because
one objective parks. A waiting deadline is distinct from a parked evidence condition.
Existing plans retain implicit sequential dependencies. Explicit `depends_on: []` declares
independence. In mixed plans, omitted edges depend on the preceding task. Descendants of a
parked task do not run and the aggregate run reports parked rather than busy-looping.

### Step 1: initialization, dependencies, and parking

Files: `loop/engine.py`, `loop/__main__.py`, `tests/test_roadmap.py`.

1. Pass the target root to validation. Resolve Git's `info/exclude` using
   `git rev-parse --git-path info/exclude`; lock the exclusion file while appending
   `/.agent-loop/`. Do this before `Engine.__init__` creates local state. Support relative
   Git paths and linked worktrees without editing committed ignore files.
2. Persist an atomic, fsynced intent outside the future worktree, in
   `.agent-loop/intents/<run-id>.json`, before worktree creation. Record run id, complete
   plan, digest, branch, base SHA, target common Git directory, workspace, and nonce.
   A matching registered worktree must have the recorded branch, exact base HEAD, clean
   state, matching common Git directory and expected path. Adopt it and insert run/tasks
   in one transaction. Clear intent only after that transaction commits. On restart after
   commit, clear matching leftover intent. Test before/after every write and Git operation.
3. Never rename an unverified workspace or delete user work/branches. Preserve conflicting
   artifacts in place and allocate a fresh unique workspace and branch, recording the
   conflict in the new durable intent. Recover a branch created before checkout only if
   intent proves ownership and no worktree uses it. Otherwise allocate fresh names.
4. Add validated dependency edges, unknown-id/self-edge/cycle rejection, and eligibility
   before skip-and-continue. Keep one active worker per workspace. Persist task park reason,
   wake kind and deadline. Keep the existing retry bound until step 3 covers all failures.
   Park exhausted tasks and continue eligible independent tasks. Quota waits release slots.
5. Partial completion remains unsuccessful. `partial_pr` may prepare a clearly marked local
   report for the accepted subset after all accepted checks pass; it does not authorize
   remote partial publication. A complete run checks all tasks before `ready_for_pr`.

Gates: fresh target/linked worktree exclusion; target-root validation; all initialization
crash boundaries; conflict preservation including branch registration; explicit independent
work continues after park/wait; implicit dependents never run; all parked has no busy loop;
legacy status assertions migrated without removing their acceptance/rollback assertions.

### Step 2: authoritative policy, decisions, and successor plans

Files: engine, planner, review, adapters, CLI, new policy module, roadmap tests.

1. Store decisions with objective/run/task identity, question, alternatives, choice, reason,
   evidence, policy version and validation outcome; include material decisions in reports.
   Validate canonical allowed roots, protected paths, configured models, setup argv recipes,
   and checks against the separately frozen policy. Protect checks across all tasks, plus
   explicitly declared helper/configuration paths. A generated plan may use only trusted
   checks registered by the original policy; an argv hash alone cannot protect its imports.
   Require a declared trusted acceptance surface for autonomous generated plans. Production
   files under test need not be immutable; test/evaluation helpers and configuration do.
2. Permit new files beneath allowed roots using safe_path and parent/symlink containment.
   Preserve check/allowlist overlap enforcement. Validate before spawning or running setup.
3. Classify question-only replies using the documented heuristic; this is diagnostic evidence,
   not proof that the objective failed. Run trusted checks for no-diff output and record an
   already-satisfied SHA when objective-specific checks establish satisfaction. Otherwise
   answer once with the decision rule, then change strategy/replan within policy.
4. Plan reviews are advisory: validate the original first; on decline try one repair; validate
   repair against the original frozen policy/checks; fall back only to a validated original.
   Store verdict and validation evidence. Failure review never forces human approval.
5. Successors use a new bounded-length id and the same stable objective id, policy and failure
   history. Freeze the parent's accepted HEAD as successor base, create a separate workspace
   at that SHA, copy only identical accepted tasks whose checks still pass, and activate the
   successor and mark the parent superseded in one transaction. Never copy counters as new
   usage events. Unchanged accepted SHAs must remain ancestors. Changed task contracts become
   pending. Persist successor intent before Git operations and reconcile interrupted activation.
   Running parent workers must be reaped before succession. Regression tasks also use successors.

Gates: policy expansion/check substitution rejected; helper/config protection; decisions in
reports; question/no-change checks; declined review fallback; new file containment; accepted
commits actually present after succession; restart at each succession boundary; shared history.

### Step 3: all-call accounting and objective-level waste control

Files: engine, planner, review, adapters, provider runner, new accounting module, tests.

1. Every provider CLI invocation has a UUID and durable intent before dispatch, including
   scout/plan/repair/review/improve calls. Store actual provider usage components without
   double-counting cache tokens. Missing usage is NULL, never zero. Separate CLI invocations
   from internal model requests; do not claim internal request visibility a provider lacks.
2. Account-global registry admission uses `BEGIN IMMEDIATE` on one shared SQLite database,
   keyed by provider/account/quota bucket and stable objective identity. All cap checks and
   reservation insertions happen in that transaction. Reserve a configured conservative
   whole-invocation allowance covering input, output and CLI tool turns, not a character-count
   claim of an exact token bound. Honor provider-side limits where supported; otherwise
   disclose in-flight overshoot. Unknown reservations do not expire with rolling windows.
   Reconcile a UUID once. Target result reaping works during registry failure; retry accounting
   from a durable local outbox. Tests use an isolated registry path.
3. Normalize diagnostic roots/timestamps while preserving relative paths, error codes and
   test identities; retain full examples. Key history by objective, semantic task identity,
   failure fingerprint, and strategy (provider/model/effort plus concrete approach identity).
   Task renames/splits do not create novelty. Track progress evidence independently of unrelated
   accepted tasks. Every unsuccessful local path, including setup/no-change/worker-stale,
   participates in deterministic retry control. Deduplicate review of unchanged evidence.
4. After two unchanged failures select an untried configured strategy or one material replan.
   Persist replan-attempted per evidence before calling the planner. Invalid or equivalent
   output consumes that opportunity. If no novel strategy remains, park. Remove max_attempts
   only after tests cover every retry path; retain legacy explicitly configured smaller bounds
   as policy limits. Transient/auth/quota waits use non-inference recovery, not coding retries.
5. Provider runner makes at most one CLI invocation. Preflight quota waits return a structured
   provider_wait envelope (exit 75), deadline and invoked=false. Post-invocation quota results
   include invoked=true and usage: exclude them from coding-failure counts but retain invocation
   accounting. No sleeping or internal retries. Parse the envelope explicitly and validate it.

Gates: simultaneous targets contend for the same allowance; planning before a run is accounted;
crash/NULL accounting preserved; every call kind attributed; pre/post-dispatch waits differ;
all retry paths terminate on unchanged evidence; renamed successors cannot reset history.

### Step 4: detached workers and ownership

Files: engine, new worker module, CLI, adapters/provider runner, tests.

Persist attempt UUID, workspace/base/policy identity and result path before spawn. A worker
must acquire an exclusive workspace execution lock and durably register PID, process-start
identity and nonce before invoking a provider. It holds ownership through provider exit and
result persistence. Supervisor recovery never restores a workspace until ownership is proven
quiescent. Unknown ownership enters recovering; it must not trigger replacement dispatch.

Use a launch handshake, heartbeat, bounded lease and atomic fsynced result containing all
identities. The worker launcher and provider descendants must be reconciled as an owned group;
a reused PID or orphan provider cannot authorize cleanup. Reaping verifies identity, lease,
base and policy, then uses existing checked commit reconciliation. Preserve pre-attempt file
snapshots for validation after restart. Poll completion independently of the maximum lease.
Only the supervisor accepts/commits. Tests exercise real child processes and injected crashes
before spawn, before registration, after registration, after result, and after commit.

### Step 5: automatic setup and regression repair

Classify raw check diagnostics before hashing. Run only frozen policy-approved setup commands
and recipes, with timeouts and workspace validation. Persist recipe identity/environment
version before execution; an unchanged failed recipe is not retried. Repeated missing-dependency
results count toward waste control even when setup itself returns zero. No matching authorized
recipe parks with an evidence/environment wake condition rather than asking a person.

Diagnose milestone regressions against accepted commits and create a validated successor repair
plan. Candidate files may include likely causal changes, but intersect them with policy and
exclude protected acceptance surfaces; handle an empty candidate set explicitly. Preserve the
original accepted record, revalidate the combined result, and bound recurring regression repair
with objective history. A repair cannot append tasks to a started immutable plan.

### Steps 6–9: service and applied-learning contracts

6. Add bounded total context, account/bucket quota/auth health probes and deterministic routing.
   Persist probe deadlines independently of task success. Keep raw samples and censored timeout
   observations. Limit tuning deltas and revert against a fixed baseline. Test all-provider
   outages, unknown telemetry, context bounds and recovery with a fake clock.
7. Add objective queue, deterministic service ticks, per-run pinned installations and launchd
   templates. Persist release intent/PR/head/check state and reconcile before retry. Require
   separately configured release destinations, exact-SHA gates and confirmed merge base for
   dependencies. Local completion, PR and deployment are different outcomes. Test reboot,
   fairness, no-work sleep, PR reuse, changed head and unavailable registry. Installing/starting
   the service or publishing requires actual execution/release configuration, not this roadmap.
8. Incrementally consume unique events with transactional cursors. Scope lessons and record
   each application and outcome. Trial windows include all assigned tasks, failures, parks and
   unknown usage, not only successes. Require at least 10 terminal comparable tasks and known
   accounting; evaluate at 10 acceptances or 14 days, and retire inconclusive trials after a
   configured maximum observation period. Insufficient evidence never promotes. Compare total
   workload tokens per acceptance and completion rate with a frozen comparable baseline.
9. Generate at most one code-improvement trial from corroborated evidence. Use an independently
   pinned evaluator/fixture manifest outside worker write roots, versioned installations and
   immutable enforcement surfaces. Candidate-authored tests supplement independent gates.
   Require declared quality/token thresholds, migration downgrade tests and canary evidence.
   Persist routing/version assignment; rollback routes new work to baseline and reconciles old
   workers without deleting target work. Missing independent fixtures/configuration records an
   ineligible candidate, not a successful deployment. Test failed migration, canary regression,
   incomplete evidence and active-worker rollback with simulated providers.

Step 10 remains optional and requires measured evidence before enabling task-level parallelism.

### Implementation rules

- Preserve existing user edits. Make coherent changes with full-suite gates; do not commit
  unrelated pre-existing edits. Use ROADMAP-NOTES.md for implementation choices and progress.
- Existing assertions for superseded blocked/review behavior may be updated narrowly. Preserve
  path, history, truncation, check-integrity and rollback coverage. Add new tests separately.
- Enforcement functions may change only to implement explicitly specified invariants, with
  regression coverage; never weaken a boundary merely to pass tests. Do not skip required work
  because an obsolete instruction prohibits its necessary migration.
- Prefer clear ownership and transactional invariants over minimizing columns/functions.
- No provider inference, service activation, or publication is needed for simulated acceptance
  tests. Real efficiency claims require a separately measured representative workload.
