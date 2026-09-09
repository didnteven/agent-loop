"""Explicit GitHub release step. Uses normal branch protection; never admin bypass."""
import json
import re
import subprocess
from pathlib import Path

from .adapters import run_process
from .engine import git


def gh(repo, *args):
    return subprocess.check_output(["gh", *args, "--repo", repo], text=True).strip()


def checks_pass(checks):
    # An empty required-check list is not evidence that validation ran.
    return bool(checks) and all(check.get("bucket") == "pass" for check in checks)


def validate_remote(url, repository):
    if url.rstrip("/").removesuffix(".git") not in (
            "https://github.com/" + repository, "git@github.com:" + repository,
            "ssh://git@github.com/" + repository):
        raise ValueError("origin does not match the explicitly selected GitHub repository")


def publish(engine, run_id, repository, base="main", merge=False):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Use OWNER/REPOSITORY")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", base) or ".." in base:
        raise ValueError("Invalid base branch")
    run = engine.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError("Unknown milestone")
    tasks = engine.db.execute("SELECT * FROM tasks WHERE run_id=?", (run_id,)).fetchall()
    if not tasks or any(task["status"] != "done" for task in tasks):
        raise ValueError("All sections must be completed before publication")
    plan = json.loads(run["plan"])
    workspace = Path(run["workspace"])
    if git(workspace, "status", "--porcelain"):
        raise ValueError("Managed worktree is dirty")
    sha = git(workspace, "rev-parse", "HEAD")
    last_id = plan["tasks"][-1]["id"]
    expected = next(task["sha"] for task in tasks if task["id"] == last_id)
    if sha != expected:
        raise ValueError("Branch tip differs from the verified milestone commit")
    validate_remote(git(engine.repo, "remote", "get-url", "origin"), repository)
    prs = json.loads(gh(repository, "pr", "list", "--head", run["branch"], "--base", base,
                        "--state", "all", "--json", "number,state,headRefOid,url"))
    if len(prs) > 1:
        raise ValueError("Multiple matching PRs; resolve ambiguity before publishing")
    if prs and prs[0]["state"] == "MERGED":
        if prs[0]["headRefOid"] != sha:
            raise ValueError("Merged PR does not match this milestone")
        return {"state": "merged", "url": prs[0]["url"]}
    if prs and prs[0]["state"] != "OPEN":
        raise ValueError("Matching PR is closed; do not silently recreate it")
    for task in plan["tasks"]:
        code, out, err = run_process(task["check"], workspace, plan.get("check_timeout_seconds", 30))
        if code:
            raise ValueError("Release verification failed: " + (out+err)[-3000:])
    if git(workspace, "status", "--porcelain"):
        raise ValueError("Release checks changed tracked or unignored files")
    git(engine.repo, "fetch", "origin", base)
    git(workspace, "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD")
    git(workspace, "push", "origin", "HEAD:refs/heads/" + run["branch"])
    engine.prepare_pr(plan, run)
    if not prs:
        gh(repository, "pr", "create", "--head", run["branch"], "--base", base,
           "--title", plan.get("title", run_id), "--body-file", str(engine.home / (run_id+"-pr.md")))
    pr = json.loads(gh(repository, "pr", "view", run["branch"], "--json", "number,state,headRefOid,url"))
    if pr["headRefOid"] != sha:
        raise ValueError("PR head changed; refusing to merge")
    if not merge:
        return {"state": "pr_open", "url": pr["url"]}
    protected = subprocess.check_output(
        ["gh", "api", "repos/"+repository+"/branches/"+base, "--jq", ".protected"], text=True).strip()
    if protected != "true":
        raise ValueError("Automatic merging requires a protected base branch")
    check_run = subprocess.run(
        ["gh", "pr", "checks", str(pr["number"]), "--repo", repository, "--required", "--json", "name,bucket,state"],
        text=True, capture_output=True)
    try:
        checks = json.loads(check_run.stdout)
    except ValueError:
        raise ValueError("Unable to read required checks: " + check_run.stderr[-2000:])
    if check_run.returncode or not checks_pass(checks):
        return {"state": "waiting_for_checks", "url": pr["url"], "checks": checks}
    gh(repository, "pr", "merge", str(pr["number"]), "--merge", "--match-head-commit", sha)
    observed = json.loads(gh(repository, "pr", "view", str(pr["number"]), "--json", "state,url,headRefOid"))
    return {"state": "merged" if observed["state"] == "MERGED" else "merge_pending", "url": observed["url"]}
