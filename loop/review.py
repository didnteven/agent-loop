"""Optional self-review passes.

Two advisory checkpoints, both off by default:
  - review_plan: before a milestone starts, judge whether each task's goal is
    real and needed, and whether its `check` could plausibly verify its `prompt`.
  - review_pr: before publish, compare the finished diff against each task's
    prompt and flag anything fabricated, unnecessary, or unrelated.

Neither is a trusted `check` — a review call is just another headless model
invocation and can be wrong. It only gates the *optional* --review path;
the underlying `run`/`publish` commands work identically without it.

The verdict is a single sentinel line rather than JSON: the only thing the
engine acts on programmatically is one boolean, so there is nothing for a
structured format to buy here, and a bare marker is far less likely to be
broken by a stray sentence, code fence, or tool-use attempt in the reply.
"""
import json
import re

from .adapters import command, parse, run_process
from .engine import git

SENTINEL = re.compile(r"REVIEW:\s*(APPROVED|DECLINED)")
FAILURE_SENTINEL = re.compile(r"FAILURE:\s*(RETRY|BLOCK)")

VERDICT_INSTRUCTIONS = (
    "Do not use any tool, do not ask a question, do not write files. "
    "End your reply with exactly one line, alone, in this exact form: "
    "\"REVIEW: APPROVED\" or \"REVIEW: DECLINED\". "
    "Before that line, briefly state your reasoning as plain text (empty if approved).\n\n"
)


def _ask(provider, model, prompt, cwd, timeout):
    code, out, err = run_process(command(provider, prompt, model), cwd, timeout)
    result = parse(provider, code, out, err)
    if result.status != "ok":
        raise RuntimeError("Review call failed: " + (result.error or result.response or err)[-2000:])
    text = result.response.strip()
    matches = SENTINEL.findall(text)
    if len(matches) != 1:
        raise RuntimeError("Review did not end with exactly one REVIEW: verdict: " + text[:500])
    verdict = matches[0] == "APPROVED"
    reasoning = SENTINEL.sub("", text).strip()
    return {"approved": verdict, "reasoning": reasoning}


PLAN_REVIEW_PROMPT = (
    "You are sanity-checking a coding task plan before any worker touches the repository. "
    "For each task, judge whether its stated goal is a real, needed change (not fabricated, "
    "not already implemented, not duplicating existing functionality) and whether its `check` "
    "command could plausibly verify the `prompt`'s claim. Do not implement anything; only judge.\n\n"
    + VERDICT_INSTRUCTIONS + "PLAN:\n"
)


def review_plan(provider, model, plan, cwd, timeout=180):
    return _ask(provider, model, PLAN_REVIEW_PROMPT + json.dumps(plan, indent=2), cwd, timeout)


PR_REVIEW_PROMPT = (
    "You are sanity-checking a finished milestone before it becomes a pull request. Each task "
    "below already passed its own trusted `check` command, but that only proves the check's "
    "narrow assertions passed, not that the change is a truthful, necessary response to its "
    "`prompt`, or that nothing unrelated slipped in. Compare each task's prompt against the diff "
    "hunks touching its declared files. Flag anything fabricated, unnecessary, unrelated to the "
    "prompt, or where the diff does not actually do what the prompt asked.\n\n"
    + VERDICT_INSTRUCTIONS
)


def review_pr(provider, model, plan, workspace, base_sha, timeout=180):
    diff = git(workspace, "diff", base_sha, "HEAD")
    tasks = [{"id": t["id"], "prompt": t["prompt"], "files": t["files"]} for t in plan["tasks"]]
    prompt = (PR_REVIEW_PROMPT + "TASKS:\n" + json.dumps(tasks, indent=2)
              + "\n\nDIFF:\n" + diff[:20000])
    return _ask(provider, model, prompt, workspace, timeout)


FAILURE_REVIEW_INSTRUCTIONS = (
    "You are diagnosing a failed coding task. Do not implement, edit files, use tools, ask "
    "questions, delegate, or propose a changed plan. Decide only whether retrying the same "
    "immutable task is reasonable. End your reply with exactly one line, alone, in this exact "
    "form: \"FAILURE: RETRY\" or \"FAILURE: BLOCK\". Before that line, briefly state why.\n\n"
)


def review_failure(provider, model, task, failure, workspace, timeout=180):
    """Return an advisory retry/block recommendation after a worker or check failure."""
    diff = git(workspace, "diff", "HEAD", "--", *task["files"])
    prompt = (FAILURE_REVIEW_INSTRUCTIONS + "TASK:\n" + json.dumps(
        {key: task.get(key) for key in ("id", "files", "prompt", "check")}, indent=2)
        + "\n\nFAILURE:\n" + failure[-4000:] + "\n\nUNCOMMITTED DIFF:\n" + diff[:20000])
    code, out, err = run_process(command(provider, prompt, model), workspace, timeout)
    result = parse(provider, code, out, err)
    if result.status != "ok":
        raise RuntimeError("Failure review call failed: " + (result.error or result.response or err)[-2000:])
    text = result.response.strip()
    matches = FAILURE_SENTINEL.findall(text)
    if len(matches) != 1:
        raise RuntimeError("Failure review did not return exactly one FAILURE verdict: " + text[:500])
    return {"retry": matches[0] == "RETRY", "reasoning": FAILURE_SENTINEL.sub("", text).strip()}
