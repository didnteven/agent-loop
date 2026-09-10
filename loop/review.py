"""Optional self-review passes.

Two advisory checkpoints, both off by default:
  - review_plan: before a milestone starts, judge whether each task's goal is
    real and needed, and whether its `check` could plausibly verify its `prompt`.
  - review_pr: before publish, compare the finished diff against each task's
    prompt and flag anything fabricated, unnecessary, or unrelated.

Neither is a trusted `check` — a review call is just another headless model
invocation and can be wrong. It only gates the *optional* --review path;
the underlying `run`/`publish` commands work identically without it.
"""
import json

from .adapters import command, parse, run_process
from .engine import git


def _ask(provider, model, prompt, cwd, timeout):
    code, out, err = run_process(command(provider, prompt, model), cwd, timeout)
    result = parse(provider, code, out, err)
    if result.status != "ok":
        raise RuntimeError("Review call failed: " + (result.error or result.response or err)[-2000:])
    text = result.response.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("Review did not return a JSON object: " + text[:500])
    verdict = json.loads(text[start:end + 1])
    if "approved" not in verdict:
        raise RuntimeError("Review response missing 'approved'")
    verdict["approved"] = bool(verdict["approved"])
    verdict["concerns"] = list(verdict.get("concerns") or [])
    return verdict


PLAN_REVIEW_PROMPT = (
    "You are sanity-checking a coding task plan before any worker touches the repository. "
    "For each task, judge whether its stated goal is a real, needed change (not fabricated, "
    "not already implemented, not duplicating existing functionality) and whether its `check` "
    "command could plausibly verify the `prompt`'s claim. Do not implement anything; only judge. "
    "Do not use any tool, do not ask a question, do not write files. Your entire reply must be "
    "exactly one JSON object and nothing else: {\"approved\": bool, \"concerns\": [str, ...]}. "
    "concerns must be empty if approved is true. The first character of your reply must be '{'.\n\nPLAN:\n"
)


def review_plan(provider, model, plan, cwd, timeout=180):
    return _ask(provider, model, PLAN_REVIEW_PROMPT + json.dumps(plan, indent=2), cwd, timeout)


PR_REVIEW_PROMPT = (
    "You are sanity-checking a finished milestone before it becomes a pull request. Each task "
    "below already passed its own trusted `check` command, but that only proves the check's "
    "narrow assertions passed, not that the change is a truthful, necessary response to its "
    "`prompt`, or that nothing unrelated slipped in. Compare each task's prompt against the diff "
    "hunks touching its declared files. Flag anything fabricated, unnecessary, unrelated to the "
    "prompt, or where the diff does not actually do what the prompt asked. "
    "Do not use any tool, do not ask a question, do not write files. Your entire reply must be "
    "exactly one JSON object and nothing else: {\"approved\": bool, \"concerns\": [str, ...]}. "
    "concerns must be empty if approved is true. The first character of your reply must be '{'.\n\n"
)


def review_pr(provider, model, plan, workspace, base_sha, timeout=180):
    diff = git(workspace, "diff", base_sha, "HEAD")
    tasks = [{"id": t["id"], "prompt": t["prompt"], "files": t["files"]} for t in plan["tasks"]]
    prompt = (PR_REVIEW_PROMPT + "TASKS:\n" + json.dumps(tasks, indent=2)
              + "\n\nDIFF:\n" + diff[:20000])
    return _ask(provider, model, prompt, workspace, timeout)
