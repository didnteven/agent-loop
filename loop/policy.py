"""Canonical execution policy kept outside model-authored task decisions."""
import hashlib
import json
from pathlib import Path

from .registry import limits_narrow, normalize_limits


DEFAULT_PROTECTED = ("tests/test_loop.py",)


def canonical_policy(plan, repo):
    supplied = plan.get("policy", {})
    if not isinstance(supplied, dict):
        raise ValueError("policy must be an object")
    roots = supplied.get("allowed_roots", ["."])
    protected = list(supplied.get("protected_paths", DEFAULT_PROTECTED))
    trusted_checks = supplied.get("trusted_checks")
    if trusted_checks is None:
        trusted_checks = [task.get("check") for task in plan.get("tasks", [])]
    trusted_commands = supplied.get("trusted_commands")
    if trusted_commands is None:
        trusted_commands = [task["run"] for task in plan.get("tasks", [])
                            if task.get("provider") == "command" and "run" in task]
    models = supplied.get("models", [])
    result = {
        "allowed_roots": roots,
        "protected_paths": sorted(set(protected)),
        "trusted_checks": trusted_checks,
        # An explicitly supplied initial plan is authority for its own declared
        # setup. Generated successors are checked against this frozen value.
        "setup_allowed": supplied.get("setup_allowed", bool(plan.get("setup"))),
        "setup_recipes": supplied.get("setup_recipes", []),
        "models": models,
        # Omitted limits mean monitoring mode, never an invented default cap.
        "limits": normalize_limits(supplied.get("limits")),
    }
    if trusted_commands:
        # Only present when used, so plans without command tasks keep the
        # policy digest their existing runs were frozen with.
        result["trusted_commands"] = trusted_commands
    _validate_shape(result, Path(repo).resolve())
    return result


def policy_digest(policy):
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _relative_path(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(label + " must contain non-empty relative paths")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or any(part in (".git", ".agent-loop") for part in path.parts):
        raise ValueError("Disallowed policy path: " + value)
    return path


def _validate_shape(policy, repo):
    roots = policy["allowed_roots"]
    if not isinstance(roots, list) or not roots:
        raise ValueError("policy.allowed_roots must be a non-empty list")
    for root in roots:
        path = _relative_path(root, "allowed_roots")
        target = (repo / path).resolve()
        if target != repo and repo not in target.parents:
            raise ValueError("Allowed root escapes repository")
    if not isinstance(policy["protected_paths"], list):
        raise ValueError("policy.protected_paths must be a list")
    for name in policy["protected_paths"]:
        _relative_path(name, "protected_paths")
    if not isinstance(policy["setup_allowed"], bool):
        raise ValueError("policy.setup_allowed must be a boolean")
    if not isinstance(policy["trusted_checks"], list) or any(
            not isinstance(check, list) or not check or not all(isinstance(arg, str) for arg in check)
            for check in policy["trusted_checks"]):
        raise ValueError("policy.trusted_checks must contain argv lists")
    commands = policy.get("trusted_commands", [])
    if not isinstance(commands, list) or any(
            not isinstance(item, list) or not item or not all(isinstance(arg, str) for arg in item)
            for item in commands):
        raise ValueError("policy.trusted_commands must contain argv lists")
    recipes = policy["setup_recipes"]
    if not isinstance(recipes, list):
        raise ValueError("policy.setup_recipes must be a list")
    for recipe in recipes:
        if (not isinstance(recipe, dict) or not isinstance(recipe.get("match"), str)
                or not recipe["match"]
                or not isinstance(recipe.get("argv"), list) or not recipe["argv"]
                or not all(isinstance(arg, str) for arg in recipe["argv"])):
            raise ValueError("Each setup recipe needs a match string and an argv list")
        timeout = recipe.get("timeout_seconds", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("Setup recipe timeout_seconds must be a positive integer")
    if not isinstance(policy["limits"], dict):
        raise ValueError("policy.limits must be an object")
    if not isinstance(policy["models"], list):
        raise ValueError("policy.models must be a list")
    for model in policy["models"]:
        if (not isinstance(model, dict) or model.get("provider") not in ("codex", "claude", "antigravity")
                or not isinstance(model.get("model"), str)
                or model.get("effort", "low") not in ("low", "medium", "high")):
            raise ValueError("Invalid configured model")


def path_allowed(name, policy):
    path = _relative_path(name, "task files")
    if any(path == Path(item) or Path(item) in path.parents for item in policy["protected_paths"]):
        return False
    return any(root == Path(".") or path == Path(root) or Path(root) in path.parents
               for root in policy["allowed_roots"])


def validate_against_policy(plan, policy):
    if plan.get("setup") and not policy["setup_allowed"]:
        raise ValueError("Plan setup is not authorized by policy")
    trusted = {json.dumps(check, separators=(",", ":")) for check in policy["trusted_checks"]}
    for task in plan["tasks"]:
        if any(not path_allowed(name, policy) for name in task["files"]):
            raise ValueError("Task file is outside allowed roots or protected")
        if json.dumps(task["check"], separators=(",", ":")) not in trusted:
            raise ValueError("Task check is not registered by the execution policy")
        if task.get("provider") == "command":
            commands = {json.dumps(item, separators=(",", ":"))
                        for item in policy.get("trusted_commands", [])}
            if json.dumps(task.get("run"), separators=(",", ":")) not in commands:
                raise ValueError("Task command is not registered by the execution policy")


def recipe_id(recipe):
    encoded = json.dumps([recipe["match"], recipe["argv"]], separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def matching_recipe(policy, diagnostic):
    """Return the first authorized recipe whose match appears in the diagnostic.

    Only recipes registered by the frozen policy can ever run: a worker or a
    generated plan cannot introduce a new command to execute here.
    """
    lowered = str(diagnostic or "").lower()
    for recipe in policy.get("setup_recipes", []):
        if recipe["match"].lower() in lowered:
            return recipe
    return None


def policy_narrows(candidate, authority):
    if any(recipe not in authority["setup_recipes"] for recipe in candidate["setup_recipes"]):
        return False
    if candidate["setup_allowed"] and not authority["setup_allowed"]:
        return False
    if not set(candidate["protected_paths"]) >= set(authority["protected_paths"]):
        return False
    if not set(map(tuple, candidate["trusted_checks"])) <= set(map(tuple, authority["trusted_checks"])):
        return False
    if not (set(map(tuple, candidate.get("trusted_commands", [])))
            <= set(map(tuple, authority.get("trusted_commands", [])))):
        return False
    if any(not any(aroot == "." or root == aroot or root.startswith(aroot.rstrip("/") + "/")
                   for aroot in authority["allowed_roots"])
           for root in candidate["allowed_roots"]):
        return False
    if not limits_narrow(candidate.get("limits", {}), authority.get("limits", {})):
        return False
    allowed_models = {(m["provider"], m["model"], m.get("effort", "low")) for m in authority["models"]}
    return all((m["provider"], m["model"], m.get("effort", "low")) in allowed_models
               for m in candidate["models"])
