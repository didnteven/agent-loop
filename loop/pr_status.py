"""Which pull request, if any, each agent's branch has, at one GitHub call per repo.

A repo is asked when an agent is first seen on a branch, when one of its agents
has finished a turn since the last answer, or when an open PR's answer is older
than OPEN_RECHECK_SECONDS. Merged and closed answers are final. Whether a branch
was pushed cannot be told locally (an agent may push HEAD:<name> and the branch is
pruned after a merge), so it is not used as a gate.
"""
import json
import subprocess
import time
from pathlib import Path

from .storage import atomic_json

OPEN_RECHECK_SECONDS = 600
FINAL = ('MERGED', 'CLOSED')


def _git(worktree, *args):
    return subprocess.run(['git', '-C', str(worktree), *args], capture_output=True, text=True, timeout=30)


def current_branch(worktree):
    try:
        return _git(worktree, 'branch', '--show-current').stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def github_repo(repo):
    try:
        url = _git(repo, 'remote', 'get-url', 'origin').stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    for prefix in ('git@github.com:', 'https://github.com/', 'ssh://git@github.com/'):
        if url.startswith(prefix):
            return url[len(prefix):].removesuffix('.git').strip('/')
    return None


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def turn_activity(run_dir, state):
    # Interactive turns report through the activity hooks; background turns only leave
    # their logs, so a finished background turn counts from its newest log write.
    activity = read(Path(run_dir) / 'activity.json') or {}
    if state.get('attempt') or state.get('state') in ('working', 'verifying', 'commit_pending'):
        return activity or {'state': 'working', 'at': 0}
    try:
        logged = max((p.stat().st_mtime for p in (Path(run_dir) / 'logs').iterdir()), default=0)
    except OSError:
        logged = 0
    if logged > activity.get('at', 0):
        return {'state': 'idle', 'at': logged}
    return activity


def list_prs(repository):
    out = subprocess.run(['gh', 'pr', 'list', '--repo', repository, '--author', '@me', '--state', 'all',
                          '--limit', '100', '--json', 'number,state,isDraft,url,headRefName'],
                         capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(out.stderr.strip() or 'gh pr list failed')
    return json.loads(out.stdout)


def due(agent, now):
    """Whether this agent's answer could have changed since it was saved."""
    saved = agent['saved']
    if saved and saved.get('branch') == agent['branch']:
        if saved.get('state') in FINAL:
            return False
        activity = agent.get('activity') or {}
        if activity.get('at', 0) > saved.get('checked_at', 0) and activity.get('state') != 'working':
            return True
        if saved.get('state') in ('OPEN', 'DRAFT'):
            return now - saved.get('checked_at', 0) >= OPEN_RECHECK_SECONDS
        return False
    return True


def refresh(agents, lister=list_prs, now=None):
    """agents: dicts with repo, worktree, path (pr.json), activity. Returns the gh calls made."""
    now = now or time.time()
    by_repo = {}
    for agent in agents:
        branch = current_branch(agent['worktree'])
        if not branch or branch in ('main', 'master'):
            continue
        agent = {**agent, 'branch': branch, 'saved': read(agent['path'])}
        if not due(agent, now):
            continue
        by_repo.setdefault(agent['repo'], []).append(agent)
    calls = 0
    for repo, waiting in by_repo.items():
        repository = github_repo(repo)
        if not repository:
            continue
        try:
            prs = lister(repository)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            continue
        calls += 1
        for agent in waiting:
            matches = [p for p in prs if p['headRefName'] == agent['branch']]
            pr = max(matches, key=lambda p: p['number']) if matches else None
            atomic_json(agent['path'], {
                'branch': agent['branch'], 'checked_at': now,
                **({'number': pr['number'], 'url': pr['url'],
                    'state': 'DRAFT' if pr['state'] == 'OPEN' and pr.get('isDraft') else pr['state']}
                   if pr else {'state': None})})
    return calls
