"""Two-pass plan authoring: cheap repository context, then deliberate planning."""
import json
import re
from pathlib import Path

from .adapters import command, parse, provider_limits, run_process
from .engine import validate_plan
from .policy import canonical_policy, policy_narrows


CONTEXT_PROMPT = """You are a low-cost repository scout. Do not edit files, commit, or delegate.
Inspect the repository with read-only tools and return concise factual context for another agent:
project language/framework, relevant directories and files, existing tests/check scripts, current
branch state, and likely files for the requested change. Do not propose a full implementation.

REQUEST:
"""

PLANNER_PROMPT = """You are the senior planning agent for an automated coding loop. Do not edit files,
commit, or delegate. Using the repository scout notes and the request, produce one complete JSON
plan for the agent-loop supervisor. The JSON must contain only a short lowercase `id` and a
`tasks` array plus optional documented plan fields. Each task needs a unique id, provider chosen
from codex/claude/antigravity, optional explicit model/effort, a non-empty relative `files` list,
a precise implementation prompt, and a non-empty trusted `check` argv list that already exists
or is a command against existing tests. Do not invent checks that the worker will create. Keep
task file allowlists disjoint and split work into coherent sequential tasks. Return JSON only,
without markdown fences or commentary.

REQUEST:
"""


def _json_object(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        value, _ = decoder.raw_decode(text[text.find("{"):])
    if not isinstance(value, dict):
        raise ValueError("Planner did not return a JSON object")
    return value


def ask(provider, model, prompt, cwd, timeout=180, effort="low", *, engine=None,
        run_id="planning", task_id="plan", kind="plan"):
    invocation_id = (engine.begin_invocation(run_id, task_id, 1, kind, provider, model, effort)
                     if engine else None)
    code, out, err = run_process(command(provider, prompt, model, effort, worker=False), cwd, timeout)
    result = parse(provider, code, out, err)
    if invocation_id:
        engine.finish_invocation(invocation_id, result)
    if result.status != "ok":
        raise RuntimeError((result.error or result.response or err)[-4000:])
    return result.response, result.usage


def validate_generated_plan(plan, repo, authority_policy=None):
    """Validate generated paths and policy against the target repository."""
    validate_plan(plan, repo)
    if authority_policy is not None and not policy_narrows(canonical_policy(plan, repo), authority_policy):
        raise ValueError("Generated plan expands the frozen execution policy")


REPAIR_PROMPT = """Repair this coding plan using the review and validation evidence below.
Keep its id. Do not expand the supplied frozen policy, substitute trusted checks, or add a
provider/model outside policy.models. Resolve ordinary implementation choices yourself.
Return one JSON object only.\n\n"""


def repair_plan(plan, reasoning, repo, validation_errors="", engine=None):
    authority = canonical_policy(plan, repo)
    provider = plan.get("review_provider", "codex")
    model = plan.get("review_model")
    prompt = (REPAIR_PROMPT + "FROZEN POLICY:\n" + json.dumps(authority, indent=2)
              + "\n\nORIGINAL PLAN:\n" + json.dumps(plan, indent=2)
              + "\n\nREVIEW:\n" + reasoning[-6000:]
              + "\n\nVALIDATION ERRORS:\n" + validation_errors[-4000:])
    raw, _ = ask(provider, model, prompt, Path(repo),
                 plan.get("worker_timeout_seconds", 180), "high", engine=engine,
                 run_id=plan["id"], task_id="plan", kind="repair")
    repaired = _json_object(raw)
    repaired["id"] = plan["id"]
    repaired["policy"] = authority
    validate_generated_plan(repaired, Path(repo), authority)
    return repaired


def live_provider_usage(repo, timeout=30):
    """Return non-sensitive live quota windows without making telemetry a gate.

    A provider's local status command may be unavailable even though another
    provider is ready to plan.  The caller needs that fact for provider choice,
    not an artificial all-or-nothing failure before any useful work can start.
    """
    usage = {}
    for provider in ("codex", "claude", "antigravity"):
        try:
            limits = provider_limits(provider, repo, timeout)
            usage[provider] = {"status": "ok", "limits": limits.get(
                "rateLimits", limits.get("rate_limits", {}))}
        except (FileNotFoundError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
            usage[provider] = {"status": "unknown", "error": str(exc)}
    return usage


def create_plan(repo, request, *, context_provider="antigravity",
                context_model="gemini-3.8-flash-low",
                planner_provider="antigravity", planner_model="gemini-3.1-pro-high",
                timeout=180, usage_timeout=30, engine=None):
    repo = Path(repo).resolve()
    usage = live_provider_usage(repo, usage_timeout)
    def invoke(provider, model, prompt, effort, task_id):
        if engine is None:
            return ask(provider, model, prompt, repo, timeout, effort)
        return ask(provider, model, prompt, repo, timeout, effort, engine=engine,
                   run_id="planning", task_id=task_id, kind="plan")
    context, context_usage = invoke(context_provider, context_model,
                                    CONTEXT_PROMPT + request + "\n\nLIVE QUOTA WINDOWS:\n"
                                    + json.dumps(usage, indent=2), "low", "scout")
    planner_prompt = (PLANNER_PROMPT + request + "\n\nLIVE QUOTA WINDOWS:\n"
                      + json.dumps(usage, indent=2) + "\n\nREPOSITORY SCOUT NOTES:\n"
                      + context[:30000])
    raw_plan, planner_usage = invoke(planner_provider, planner_model, planner_prompt,
                                     "high", "author")
    plan = _json_object(raw_plan)
    validate_generated_plan(plan, repo)
    plan.setdefault("description", "Two-pass plan: budget context scout followed by senior planner")
    plan["planning"] = {
        "context_provider": context_provider,
        "context_model": context_model,
        "planner_provider": planner_provider,
        "planner_model": planner_model,
        "usage_checked": True,
        "usage_unknown": [name for name, report in usage.items()
                          if report.get("status") != "ok"],
        "context_tokens": sum(v for v in context_usage.values() if isinstance(v, int)),
        "planner_tokens": sum(v for v in planner_usage.values() if isinstance(v, int)),
    }
    return plan
