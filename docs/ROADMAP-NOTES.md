# Roadmap implementation notes

The roadmap contract was corrected before implementation. Existing working-tree edits are
preserved. Standard-library unittest discovery is the gate because pytest is unavailable.

Implementation progress and validation are recorded below; unimplemented roadmap behavior
is not a claim about the running system.

## Step 3 — all-call accounting and waste control (implemented)

- `loop/registry.py` holds account-global admission in one shared SQLite database
  (`~/.agent-loop/registry.sqlite`, overridable with `AGENT_LOOP_REGISTRY`). Reservations
  are taken inside `BEGIN IMMEDIATE`, keyed by provider, account and quota bucket, so
  concurrent targets contend for the same allowance.
- Optional caps live in the frozen policy as `limits` with `account`, `objective` and
  `provider:<name>` scopes, each optionally carrying `rolling` and `lifetime_tokens`.
  Omission is monitoring mode. A successor may tighten a cap, never loosen or drop one.
- Unreconciled reservations keep holding their conservative allowance: unknown usage is
  never reported as zero. Reconciliation is exactly-once; a registry outage queues it in a
  local outbox (`.agent-loop/outbox`) that is replayed at the next tick.
- Admission precedes the attempt, so a refused invocation consumes no retry history. A
  rolling limit yields `waiting` with a deadline; a lifetime limit parks with `wake_kind`
  `policy`.
- Every unsuccessful path now records a fingerprint occurrence, including transient and
  auth results. Unchanged transient failures terminate after `transient_attempts`
  (default 5) instead of retrying forever.
- With `auto_replan`, exhausted coding strategies buy exactly one material replan. The
  opportunity is persisted against the failure evidence *before* the planner call, so a
  crash or a renamed successor cannot reset it, and a repaired plan with the same task
  shape is rejected as equivalent. `tick` returns `superseded`; the CLI continues on the
  validated successor.

## Step 4 — detached workers and ownership (implemented)

- `loop/worker.py` is the worker boundary: it assembles the prompt and provider argv,
  runs the quota-aware runner, and writes an atomic result carrying every identity.
- Ownership is an exclusive flock on the workspace, held from before the provider starts
  until after the result is durable. Recovery asks the lock, not the process table; the
  recorded process-start identity is used only to refuse a reused PID before terminating
  an over-lease process group.
- Launch intent is fsynced before the spawn. A launch handshake distinguishes "not yet
  registered" from "dead", so the supervisor never races a replacement worker into a
  worktree whose ownership is unresolved.
- Results are verified against attempt id, nonce, run, task, base SHA and policy version;
  a mismatch or a moved worktree is rejected as stale. A reaped result flows through the
  normal validation and commit path and does not consume a second attempt.
- Pre-attempt file sizes are persisted with the attempt, so truncation validation still
  works after a supervisor restart.

## Step 5 — automatic setup and regression repair (implemented)

- `loop/diagnosis.py` classifies raw diagnostics before they are fingerprinted, so failures
  group by cause rather than by wording.
- A `missing_dependency` classification runs at most one matching recipe from the frozen
  policy's `setup_recipes`, recording recipe identity and environment version before
  execution. The same recipe is never retried in an unchanged environment, a recipe that
  dirties the worktree fails, and repeated missing-dependency results still count toward
  waste control. With no authorized recipe the task parks with an `environment` wake
  condition rather than asking a person.
- A milestone regression is diagnosed against every accepted commit, not just the failing
  task's own. Candidate files are intersected with policy and exclude the trusted
  acceptance surface; an empty candidate set is explicit. The repair lands as a validated
  successor plan carrying the original check, so accepted work stays accepted and no task
  is appended to a started immutable plan.

## Step 6 — global recovery, bounded context, measured routing (implemented)

- `loop/context.py` bounds the whole prompt, not each context file: sections are served in
  priority order out of one budget with output space reserved, and trimming is visible in
  the prompt. The engine and the worker boundary share it.
- `loop/health.py` keeps raw samples rather than summaries, decays them by age, and treats a
  timeout as a censored observation that can only raise a learned timeout. Routing prefers
  measured tokens per acceptance; an unmeasured route is unknown, never preferred; quota
  constrains eligibility only. Tuning is bounded per observation period against a frozen
  baseline and can be reverted.
- Held providers recover through periodic non-inference probes (`codex login status` and
  friends) with learned intervals, so waiting ends without another task succeeding first.
  An unknown probe result reschedules rather than assuming recovery.

## Step 7 — objective queue and service lifecycle (implemented)

- `loop/service.py` holds a durable objective queue, deterministic ticks, fair selection,
  crash backoff, a heartbeat file, and a launchd `KeepAlive` template. An idle service
  sleeps; it never invents work.
- Release identity (repository, base, PR number, URL, head SHA) is persisted, and a changed
  head invalidates recorded check evidence. Local completion, a published PR, and a merged
  PR are distinct objective states. A parked objective stays visibly unsuccessful while the
  rest of the queue continues.
- `loop queue add|list` and `loop service [--once|--launchd LABEL]` expose this. Installing
  or starting the service is a separate, explicit act.

## Step 8 — incremental learning (implemented)

- `loop/learning.py` consumes new events through a durable per-target cursor and makes no
  model calls. Occurrences are keyed by event identity, so reprocessing cannot inflate
  evidence, and generalization needs at least three occurrences across two independent
  objectives — successor retries of one failure do not corroborate each other.
- Trials are one at a time against a baseline frozen before they start, include failures and
  parks, and require at least ten terminal tasks with complete accounting. Unknown usage
  blocks promotion; insufficient evidence retires rather than promotes. Lessons are
  retrievable by scope and can be disabled.
- Scheduled on the service cadence and available as `loop learn` / `loop lessons`.

## Step 9 — isolated improvement, evaluation, canary, rollback (implemented)

- `loop/improve.py` fences the enforcement and accounting surfaces in code: the fenced file
  list plus the source of `safe_path`, `validate_worker_changes`, `validate_managed_history`
  and `validate_plan`. A candidate that changes any of them is ineligible, whatever its
  fixtures say.
- Evaluation runs the pinned manifest at `tests/fixtures/manifest.json`, which is itself
  fenced. A missing manifest is an ineligible candidate, never a successful deployment.
- At most one improvement trial is in flight, proposals are deduplicated, and a cheaper
  validated configuration or routing lesson is preferred over a code change.
- Runs are pinned to a supervisor version and finish on it. Rollback routes new work to the
  retained baseline, records a rolled-back lesson, and reports which runs the candidate
  still owns; it never deletes target work.

## Not implemented

- Step 10 (task-level parallelism in separate worktrees) remains optional and unstarted.
- Efficiency claims are unmeasured: the roadmap requires a representative real workload,
  and every test here uses simulated providers.
- The improvement worker that would *author* a candidate is not wired to a provider; the
  proposal, fencing, evaluation, canary and rollback machinery around it is.
