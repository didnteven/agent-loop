# Test report — 10 September 2026

## Real terminal tests

All three installed clients returned `LOOP_OK` from a real model invocation. The first sandboxed Claude check incorrectly appeared unauthenticated; the normal terminal invocation outside the sandbox successfully reused its Keychain login. Codex also required the normal terminal environment for its local app-server process.

Antigravity CLI was absent from PATH and an old launcher symlink was broken. The official installer was downloaded, inspected, and run. It installed `agy` 1.1.28 in `/Users/todd/.local/bin` and appended that directory to `.zshrc`. Its normal cached login worked; no credential extraction or new account was required.

Installed versions: Codex CLI 0.153.4, Claude Code 2.1.266, Antigravity CLI 1.1.28.

## Real coding run through this supervisor

Plan: `examples/smoke.json`. Each provider generated its own implementation of `clamp(value, low, high)` using the same specification. A separate verifier tested interior values, both boundaries, out-of-range values, negative numbers, floats, equal bounds, and reversed bounds. Each section passed 11 cases, then the supervisor committed it. All checks passed again at milestone completion.

| Provider | Requested model | Attempts | Observed input + output tokens, including caches | Commit | Result |
| --- | --- | ---: | ---: | --- | --- |
| Codex | `gpt-5.6-luna` | 1 | 12,379 | `107b2d3` | Pass |
| Claude | `claude-haiku-4-5-20251001` | 1 | 4,336 | `5573410` | Pass |
| Antigravity | `gemini-3.8-flash-low` | 1 | 21,755 | `683a0fc` | Pass |

Token figures are each CLI's reported usage normalized by the adapter, not comparable subscription billing weights. They exclude the preliminary connectivity tests and this conversation. Claude reported an estimated `$0.007064` for its coding invocation; this is not a verified account charge. Codex and Antigravity did not report a dollar figure. Their zero value in local accounting means unreported, not necessarily free.

The substantial input overhead for tiny tasks supports using an ordinary timer for the keep-alive. It does not establish a cheapest provider for real work.

Resulting branch: `loop/three-provider-smoke`.

Worktree: `.agent-loop/worktrees/three-provider-smoke`.

Final recorded state: `ready_for_pr`. Prepared body: `.agent-loop/three-provider-smoke-pr.md`. Raw coding responses and stderr: `.agent-loop/logs/three-provider-smoke/`. No remote GitHub repository was selected, pushed, or merged.

## Quotas and lifecycle

The real Codex app-server returned both a 300-minute quota window and a 10,080-minute window with reset timestamps. At preflight it reported 39% and 8% used, respectively. No reset credits were consumed. These are a point-in-time observation, not current quota guarantees.

Automated tests simulate exhausted quotas and time advancement. They verify no additional worker calls during the wait, persisted deadlines across a supervisor restart, and resumption after the reset. A real five-hour exhaustion/reset cycle was not induced or observed. Claude and Antigravity account-wide proactive quota collection was not tested or implemented; their real inference result parsing was tested.

All 19 automated tests passed. The local suite covers false-success responses, missing authentication, token cache accounting, bounded timeouts, file path and symlink rejection, malformed responses, a single supervisor lock, failed-code commit prevention, bounded repair, local token admission budgets, immutable plans, completed-task reuse, crash recovery after commit, milestone regression detection/repair, and preservation of unrelated worktree changes. `git diff --check` passed. Rerunning the completed real plan with `--once` returned `ready_for_pr` without another model invocation.

GitHub release checks use a simulated GitHub response: empty required checks prevent merging; passing required checks use an exact head SHA; an already merged PR is reused without a second push. This is not an end-to-end GitHub test. Live branch protection, CI, review requirements, and merge-queue behavior still need a dedicated test repository.

No long-running service was installed or left active.
