"""Loopback-only fleet control and persistent, ownership-locked local PTYs."""
import argparse
import asyncio
import base64
import binascii
import re
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import secrets
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import tempfile
import time
import uuid

from aiohttp import web, WSMsgType

from .storage import atomic_json
from . import deploy
from .models import catalog, remember
from .native import agent_terminal_command, discover_session, terminal_config, store_session, saved_session
from .usage import record as record_usage
from .usage import agent_usage, combine as combine_usage
from .supervise import Fleet, EFFORTS
from .worker import workspace_lock_path, legacy_workspace_lock_path, terminal_owner_path, process_identity, require_unreserved_workspace
from .tmux_session import TmuxSession
from . import pr_status

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'web'
APP_KEY = web.AppKey('controller', object)
COOKIE = 'agent_loop_local'
MAX_BUFFER = 256 * 1024


def ui_revision():
    digest = hashlib.sha256()
    for name in ('index.html', 'styles.css', 'app.js'):
        digest.update((ASSETS / name).read_bytes())
    return digest.hexdigest()[:10]


def locked(path):
    handle = Path(path).open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def lock_held(path):
    try:
        locked(path).close()
        return False
    except BlockingIOError:
        return True


def tail(path, limit=65536):
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read(limit).decode(errors='replace')
    except FileNotFoundError:
        return ''


class Terminal:
    def __init__(self, run, shell, mode='shell', owner_home=None, use_tmux=False):
        self.mode = mode
        self.run = run
        self.session_id = None
        self.provider = None
        self.native_config = None
        self.opened_at = time.time()
        self.session_watch = None
        self.observed_sessions = set()
        self.locks = ExitStack()
        self.buffer = bytearray()
        self.total = 0
        self.event = asyncio.Event()
        self.ws = None
        self.exited = False
        self.closed = False
        self.last_seen = time.monotonic()
        self.master = None
        self.process = None
        self.tmux = None
        try:
            keeper_lock = self.locks.enter_context(locked(run.path('keeper.lock')))
            lock_path = workspace_lock_path(run.dir, run.worktree)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            workspace_lock = self.locks.enter_context(locked(lock_path))
            legacy_path = legacy_workspace_lock_path(run.dir, run.worktree)
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_lock = workspace_lock if legacy_path == lock_path else self.locks.enter_context(locked(legacy_path))
            state = run.state()
            if state['attempt'] or state['state'] in ('working', 'verifying', 'commit_pending'):
                raise ValueError('Resume the manager to reconcile this task before opening a shell')
            argv = [shell, '-i']
            if mode != 'shell':
                self.native_config = terminal_config(run)
                argv, self.session_id, self.provider = agent_terminal_command(run, self.native_config)
            agent_argv = argv
            marker = terminal_owner_path(run.dir, run.worktree)
            recovering = False
            if use_tmux and mode != 'shell':
                if marker.exists():
                    self.tmux = TmuxSession.recover(run.dir, run.worktree)
                    recovering = True
                else:
                    self.tmux = TmuxSession.start(run.dir, run.worktree, argv, env={
                        'COLORTERM': 'truecolor',
                        'AGENT_LOOP_DEPLOY_COMMAND': str(ROOT / 'scripts' / 'merge-deploy')})
                argv = self.tmux.attach_command()
            else:
                require_unreserved_workspace(run.dir, run.worktree)
            self.master, slave = pty.openpty()
            self.resize(100, 28)
            try:
                # The server owns the locks; cleanup terminates the whole shell group.
                self.process = subprocess.Popen(
                    [sys.executable, str(ROOT / 'loop' / 'terminal_child.py'), *argv],
                    cwd=run.worktree, env={**os.environ, 'TERM': 'xterm-256color',
                                           'COLORTERM': 'truecolor', 'AGENT_LOOP_DEPLOY_COMMAND': str(ROOT / 'scripts' / 'merge-deploy')},
                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                    close_fds=True, pass_fds=tuple(dict.fromkeys((keeper_lock.fileno(), workspace_lock.fileno(), legacy_lock.fileno()))))
            finally:
                os.close(slave)
            if mode != 'shell':
                previous = state.get('interactive_session')
                if recovering and previous:
                    self.session_id = previous.get('session_id') or self.session_id
                    self.opened_at = previous.get('opened_at', self.opened_at)
                if previous and not recovering:
                    record_usage(state, 'worker', previous.get('provider', 'codex'), {}, 'interactive:' + previous['id'], model=state.get('model'), now=time.time())
                state['interactive_session'] = {'id': previous['id'] if recovering and previous else uuid.uuid4().hex, 'session_id': self.session_id,
                                                 'provider': self.provider, 'opened_at': self.opened_at, 'server_home': str(owner_home) if owner_home else None,
                                                 'pid': self.process.pid, 'start_identity': process_identity(self.process.pid),
                                                 'backend': 'tmux' if self.tmux else 'direct'}
                if self.session_id and self.provider == state['provider']:
                    state['session_id'] = self.session_id
                    state.setdefault('sessions', {})[self.provider] = self.session_id
                run.save_state(state)
                if self.session_id:
                    self.observed_sessions.add(self.session_id)
                    store_session(run, self.provider, self.session_id, self.native_config, fresh=self.provider == 'claude' and '--session-id' in agent_argv)
                if self.provider == 'codex':
                    self.session_watch = asyncio.create_task(self.watch_session())
            os.set_blocking(self.master, False)
            asyncio.get_running_loop().add_reader(self.master, self.read)
            self.reaper = asyncio.create_task(self.reap())
        except BaseException:
            if self.master is not None:
                os.close(self.master)
            self.locks.close()
            raise

    async def capture_session(self):
        if self.provider != 'codex':
            return
        session = await asyncio.to_thread(discover_session, self.run.worktree, self.opened_at, set(self.observed_sessions))
        if session:
            self.observed_sessions.add(session)
            self.session_id = session
            store_session(self.run, self.provider, session, self.native_config)
            state = self.run.state()
            if state.get('interactive_session'):
                state['interactive_session']['session_id'] = session
                if self.provider == state['provider']:
                    state['session_id'] = session
                    state.setdefault('sessions', {})[self.provider] = session
                self.run.save_state(state)

    async def watch_session(self):
        while self.process.poll() is None:
            try:
                await self.capture_session()
            except (OSError, ValueError):
                pass
            await asyncio.sleep(.5)

    def resize(self, cols, rows):
        if type(cols) is not int or type(rows) is not int or not 2 <= cols <= 500 or not 2 <= rows <= 300:
            raise ValueError('Invalid terminal dimensions')
        if self.master is not None:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            if self.tmux and self.process and self.process.poll() is None:
                try:
                    os.kill(self.process.pid, signal.SIGWINCH)
                except ProcessLookupError:
                    pass

    def read(self):
        try:
            chunk = os.read(self.master, 16384)
            if not chunk:
                asyncio.get_running_loop().remove_reader(self.master)
                return
        except BlockingIOError:
            return
        except OSError:
            asyncio.get_running_loop().remove_reader(self.master)
            return
        self.buffer.extend(chunk)
        self.total += len(chunk)
        del self.buffer[:-MAX_BUFFER]
        self.event.set()

    async def reap(self):
        await asyncio.to_thread(self.process.wait)
        if self.session_watch:
            await asyncio.gather(self.session_watch, return_exceptions=True)
        try:
            await self.capture_session()
        except (OSError, ValueError):
            pass
        self.read()
        self.exited = True
        self.event.set()
        self.locks.close()

    async def output(self, ws):
        offset = self.total - len(self.buffer)
        while not ws.closed:
            self.event.clear()
            beginning = self.total - len(self.buffer)
            if offset < beginning:
                await ws.close(code=1009, message=b'Terminal output exceeded replay buffer')
                return
            if self.total > offset:
                end = self.total
                await ws.send_bytes(bytes(self.buffer[offset - beginning:]))
                offset = end
            if self.exited and offset >= self.total:
                await ws.send_json({'type': 'exit', 'code': self.process.returncode})
                await ws.close()
                return
            await self.event.wait()

    async def close(self, *, detach=False):
        if self.closed:
            return
        self.closed = True
        if self.tmux and not detach:
            await asyncio.to_thread(self.tmux.stop)
        # Never signal a reaped shell's old PID: it may now name another process group.
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Some tmux clients leave the original process group while
                # exiting; the unreaped child PID still names this process.
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(asyncio.shield(self.reaper), 2)
            except asyncio.TimeoutError:
                if self.process.poll() is None:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        try:
                            self.process.kill()
                        except ProcessLookupError:
                            pass
        await self.reaper
        if self.ws and not self.ws.closed:
            await self.ws.close()
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        self.locks.close()


class Controller:
    def __init__(self, home, use_tmux=False):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.index = self.home / 'fleets.json'
        self.project_index = self.home / 'projects.json'
        self.token = secrets.token_urlsafe(32)
        self.mutation = asyncio.Lock()
        self.processes = {}
        self.stopping = False
        self.restart_after = 0
        self.primary_index = self.home / 'supervisor.json'
        self.snapshot_epoch = uuid.uuid4().hex
        self.snapshot_revision = 0
        self.snapshot_body = None
        self.server_commit = deploy.git(ROOT, 'rev-parse', 'HEAD')
        self.terminals = {}
        self.use_tmux = use_tmux
        self.shell = os.environ.get('SHELL', '/bin/zsh' if sys.platform == 'darwin' else '/bin/bash')
        self.registry_lock = locked(self.home / 'web.lock')

    def entries(self):
        return json.loads(self.index.read_text()) if self.index.exists() else []

    def supervisor(self):
        if self.primary_index.exists():
            ident = json.loads(self.primary_index.read_text())['id']
            if self.entry(ident):
                return self.entry(ident)
        home = self.home / 'supervisor'
        fleet = Fleet(home)
        fleet.initialize()
        spec = json.loads(fleet.manifest.read_text())
        seen = {t['run_id'] for t in spec['tasks']}
        ids = {t['id'] for t in spec['tasks']}
        for entry in self.entries():
            if Path(entry['home']).resolve() == home:
                atomic_json(self.primary_index, {'id': entry['id']})
                return entry
            old = Fleet(entry['home'])
            try:
                previous = json.loads(old.manifest.read_text())
                for source in previous['tasks']:
                    if source['run_id'] in seen:
                        continue
                    task = dict(source)
                    base, counter = task['id'], 2
                    while task['id'] in ids:
                        task['id'] = base[:50] + '-' + str(counter)
                        counter += 1
                    ids.add(task['id']); seen.add(task['run_id'])
                    spec['tasks'].append(task)
                if self.running(entry):
                    (old.home / 'stop').touch()  # Detached owned worker turns remain recoverable.
            except (OSError, ValueError, KeyError):
                continue
        atomic_json(fleet.manifest, spec)
        entry = self.register(home, 'Local supervisor')
        atomic_json(self.primary_index, {'id': entry['id']})
        return entry

    def supervisor_status(self):
        entry = self.supervisor()
        result = next(e for e in self.status() if e['id'] == entry['id'])
        spec = json.loads(Fleet(entry['home']).manifest.read_text())
        enabled = {t['id']: t.get('enabled', True) for t in spec['tasks']}
        run_ids = {t['id']: t['run_id'] for t in spec['tasks']}
        shared_path = Path(entry['home']) / 'supervisor-state.json'
        shared = json.loads(shared_path.read_text()) if shared_path.exists() else {}
        for task in result['tasks']:
            task['enabled'] = enabled[task['id']]
            reason = task.get('wait_reason', '').lower()
            quota_wait = task['state'] == 'waiting' and task['retry_at'] and (
                task.get('last_provider_status') in ('rate_limited', 'provider_wait')
                or any(h.get('until', 0) > time.time() for h in task.get('provider_holds', {}).values())
                or any(word in reason for word in ('quota', 'usage limit', 'usage reset')))
            task['signal'] = ('cleanup failed' if task.get('delete_error') else 'deleting' if task.get('delete_requested') else 'interactive' if task['terminal_open'] and task['terminal_mode'] != 'shell' else
                              'handoff' if task['terminal_requested'] else ('stopping' if task['attempt'] else 'removed') if not task['enabled'] else (
                'supervisor' if result['running'] and shared.get('busy') == run_ids[task['id']] else
                'quota' if quota_wait else 'retrying' if task['state'] == 'waiting' and task['retry_at'] else
                'blocked' if task.get('needs_help') or task['state'] == 'waiting' else task['state']))
            if task['signal'] == 'interactive' and task.get('activity'):
                task['signal'] = {'working': 'working', 'idle': 'idle', 'needs_you': 'needs you'}.get(task['activity']['state'], 'interactive')
        agent_totals = combine_usage(t['usage']['agent'] for t in result['tasks'])
        supervisor_totals = combine_usage(t['usage']['supervisor'] for t in result['tasks'])
        result['usage'] = {'agents': agent_totals, 'supervisor': supervisor_totals,
                           'total': combine_usage([agent_totals, supervisor_totals])}
        result['ui_revision'] = ui_revision()
        result['server_commit'] = self.server_commit
        result['deployment'] = deploy.public_status(ROOT)
        result.update(keeper=spec.get('keeper', {'enabled': False}), max_parallel=spec['max_parallel'],
                      desired_running=spec.get('desired_running', False),
                      supervisor_busy=shared.get('busy') if result['running'] else None)
        body = json.dumps(result, sort_keys=True)
        if body != self.snapshot_body:
            self.snapshot_revision += 1
            self.snapshot_body = body
        result.update(snapshot_epoch=self.snapshot_epoch, snapshot_revision=self.snapshot_revision)
        return result

    async def recover_native(self, app):
        # A crashed server loses the PTY master. Only terminate a recorded process
        # belonging to this registry, with an exact process-start identity match.
        for entry in self.entries():
            try:
                runs = list(Fleet(entry['home']).runs())
            except (OSError, ValueError):
                continue
            for task, run in runs:
                try:
                    saved = run.state().get('interactive_session') or {}
                except (OSError, ValueError):
                    continue
                if not task.get('terminal_requested') or saved.get('server_home') != str(self.home):
                    continue
                pid, identity = saved.get('pid'), saved.get('start_identity')
                if type(pid) is not int or pid <= 1 or not identity:
                    continue
                current = await asyncio.to_thread(process_identity, pid)
                if current == identity:
                    try:
                        if os.getpgid(pid) == pid:
                            os.killpg(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    async def finish_deletions(self, entry):
        """Called under mutation; a running manager acknowledges each turn boundary."""
        fleet = Fleet(entry['home'])
        spec = json.loads(fleet.manifest.read_text())
        for task in list(spec['tasks']):
            token = task.get('delete_requested')
            if not token or task.get('delete_error'):
                continue
            ready = fleet.home / 'deletions' / (token + '.ready')
            running = self.running(entry)
            if running and not ready.exists():
                continue
            run = self.run_for(entry['id'], task['id'])
            try:
                with ExitStack() as locks:
                    if not running:
                        locks.enter_context(locked(fleet.home / 'fleet.lock'))
                    locks.enter_context(locked(run.path('keeper.lock')))
                    lock_path = workspace_lock_path(run.dir, run.worktree)
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    locks.enter_context(locked(lock_path))
                    legacy_path = legacy_workspace_lock_path(run.dir, run.worktree)
                    legacy_path.parent.mkdir(parents=True, exist_ok=True)
                    if legacy_path != lock_path:
                        locks.enter_context(locked(legacy_path))
                    require_unreserved_workspace(run.dir, run.worktree)
                    if run.state().get('attempt'):
                        continue
                    # Git checks ownership; never recursively delete a supplied worktree path.
                    if run.worktree.exists():
                        await asyncio.to_thread(deploy.git, run.repo, 'worktree', 'remove', '--force', str(run.worktree))
                    if deploy.git(run.repo, 'branch', '--list', run.branch):
                        await asyncio.to_thread(deploy.git, run.repo, 'branch', '-D', run.branch)
                    # Keep the ready receipt outside the run: a manager may hold an older snapshot.
                    shutil.rmtree(run.dir)
                    spec['tasks'] = [t for t in spec['tasks'] if t['id'] != task['id']]
                    atomic_json(fleet.manifest, spec)
            except BlockingIOError:
                continue  # An external keeper/worker still owns this agent; retry next sweep.
            except (OSError, ValueError, RuntimeError) as exc:
                task['delete_error'] = str(exc)
                atomic_json(fleet.manifest, spec)

    async def guardian(self, app):
        async def watch():
            while not self.stopping:
                try:
                    async with self.mutation:
                        if self.primary_index.exists():
                            entry = self.supervisor()
                            fleet = Fleet(entry['home'])
                            await self.finish_deletions(entry)
                            spec = json.loads(fleet.manifest.read_text())
                            if spec.get('desired_running') and not self.running(entry):
                                if (fleet.home / 'stop').exists():
                                    atomic_json(fleet.manifest, {**spec, 'desired_running': False})
                                elif time.monotonic() >= self.restart_after:
                                    self.restart_after = time.monotonic() + 15
                                    self.resume(entry['id'])
                except (OSError, ValueError, RuntimeError):
                    pass  # Ownership uncertainty never permits a competing worker.
                await asyncio.sleep(2)
        self.guardian_task = asyncio.create_task(watch())
        self.pr_task = asyncio.create_task(self.watch_prs())

    def pr_agents(self):
        if not self.primary_index.exists():
            return []
        agents = []
        for task, run in Fleet(self.supervisor()['home']).runs():
            if task.get('delete_requested') or not run.worktree.exists():
                continue
            agents.append({'repo': task['repo'], 'worktree': run.worktree, 'path': run.dir / 'pr.json',
                           'activity': pr_status.turn_activity(run.dir, run.state())})
        return agents

    async def watch_prs(self):
        # Cheap local checks every 30s; GitHub is asked only when pr_status says an answer may have changed.
        while not self.stopping:
            try:
                await asyncio.to_thread(lambda: pr_status.refresh(self.pr_agents()))
            except Exception:
                pass
            await asyncio.sleep(30)

    def projects(self):
        if not self.project_index.exists():
            # Existing CLI/UI fleets populate the picker on the first library visit.
            projects = {}
            for entry in self.entries():
                try:
                    for task in json.loads(Fleet(entry['home']).manifest.read_text())['tasks']:
                        path = str(Path(task['repo']).resolve())
                        projects[path] = {'path': path, 'name': Path(path).name}
                except (OSError, ValueError, KeyError):
                    continue
            atomic_json(self.project_index, {'projects': list(projects.values()), 'folders': []})
        return json.loads(self.project_index.read_text())

    def save_projects(self, paths):
        library = self.projects()
        known = {p['path']: p for p in library['projects']}
        previous = len(known)
        for path in paths:
            path = str(Path(path).resolve())
            known.setdefault(path, {'path': path, 'name': Path(path).name})
        library['projects'] = sorted(known.values(), key=lambda p: (p['name'].casefold(), p['path']))
        atomic_json(self.project_index, library)
        return len(known) - previous

    def repository_root(self, path):
        if not isinstance(path, str) or not path.strip():
            raise ValueError('Enter a repository path')
        folder = Path(path).expanduser().resolve()
        result = subprocess.run(['git', '-C', str(folder), 'rev-parse', '--show-toplevel'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise ValueError('No working-tree Git repository found at ' + str(folder))
        return str(Path(result.stdout.strip()).resolve())

    def scan_projects(self, folder, depth):
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError('Enter a folder to scan')
        if type(depth) is not int or not 0 <= depth <= 12:
            raise ValueError('Scan depth must be between 0 and 12')
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('Scan folder does not exist')
        ignored = {'.git', '.agent-loop', 'node_modules', '.venv', 'venv', '__pycache__',
                   'vendor', 'build', 'dist', 'target', '.Trash', 'Library'}
        found, visited, truncated = [], 0, False
        for directory, dirs, _ in os.walk(root, followlinks=False):
            visited += 1
            if visited > 10000:
                truncated = True
                break
            current = Path(directory)
            level = len(current.relative_to(root).parts)
            dirs[:] = sorted(d for d in dirs if d not in ignored and not (current / d).is_symlink())
            if (current / '.git').exists():
                try:
                    canonical = self.repository_root(str(current))
                    if canonical == str(current):
                        found.append(canonical)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                dirs[:] = []  # Do not collect dependencies or managed worktrees inside a repo.
            elif level >= depth:
                dirs[:] = []
        return found, truncated, str(root)

    def entry(self, ident):
        return next((e for e in self.entries() if e['id'] == ident), None)

    def fleet(self, ident):
        entry = self.entry(ident)
        if not entry:
            raise web.HTTPNotFound(text='Unknown fleet')
        return Fleet(entry['home'])

    def run_for(self, fleet_id, task_id):
        for task, run in self.fleet(fleet_id).runs():
            if task['id'] == task_id:
                return run
        raise web.HTTPNotFound(text='Unknown task')

    def running(self, entry):
        proc = self.processes.get(entry['id'])
        return bool(proc and proc.poll() is None) or lock_held(Path(entry['home']) / 'fleet.lock')

    def status(self):
        results = []
        for entry in self.entries():
            try:
                fleet = Fleet(entry['home'])
                tasks = fleet.status()
                task_specs = {task['id']: task for task in json.loads(fleet.manifest.read_text())['tasks']}
                goals = {ident: task['goal'] for ident, task in task_specs.items()}
                runs = {task['id']: run for task, run in fleet.runs()}
                for task in tasks:
                    task['goal'] = goals.get(task['id'], '')
                    config = runs[task['id']].config
                    task['full_access'] = config.get('full_access', True)
                    task['allow_subagents'] = config.get('allow_subagents', True)
                    task['worker_effort'] = config.get('worker_effort') or 'medium'
                    task['usage'] = agent_usage(task, config)
                    task.pop('token_usage', None)
                    terminal = self.terminals.get((entry['id'], task['id']))
                    task['terminal_open'] = bool(terminal and not terminal.exited)
                    task['terminal_mode'] = terminal.mode if task['terminal_open'] else None
                    task['terminal_backend'] = ('tmux' if self.use_tmux or terminal_owner_path(runs[task['id']].dir, runs[task['id']].worktree).exists() else 'direct')
                    task['delete_requested'] = bool(task_specs[task['id']].get('delete_requested'))
                    task['delete_error'] = task_specs[task['id']].get('delete_error')
                    task['terminal_requested'] = bool(task_specs[task['id']].get('terminal_requested'))
                    native = terminal_config(runs[task['id']])
                    task['native_config'] = {**native, 'command_text': shlex.join(native.get('command', [])),
                                              'resume_text': shlex.join(native.get('resume_command', []))}
                    task['native_provider'] = native['provider']
                    active = task.get('interactive_session') or {}
                    task['native_session_id'] = (active.get('session_id') if active.get('provider', 'codex') == native['provider'] else None) or saved_session(runs[task['id']], native['provider'], native).get('session_id')
                    marker = terminal_owner_path(runs[task['id']].dir, runs[task['id']].worktree)
                    task['terminal_recovery_required'] = False
                    if marker.exists():
                        try:
                            session = TmuxSession.recover(runs[task['id']].dir, runs[task['id']].worktree)
                            task['terminal_recovery_required'] = not session.alive()
                        except (OSError, RuntimeError, ValueError):
                            task['terminal_recovery_required'] = True
                    task['native_ready'] = bool(task['terminal_requested'] and not task['attempt']
                        and task['state'] not in ('working', 'verifying', 'commit_pending')
                        and not task['terminal_recovery_required']
                        and not lock_held(runs[task['id']].path('keeper.lock')))
                results.append({**entry, 'running': self.running(entry), 'tasks': tasks,
                                'supervisor': json.loads(fleet.manifest.read_text())['supervisor']})
            except (OSError, ValueError, RuntimeError) as exc:
                results.append({**entry, 'tasks': [], 'error': str(exc), 'running': False})
        return results

    def register(self, home, name):
        home = str(Path(home).expanduser().resolve())
        entries = self.entries()
        if any(e['home'] == home for e in entries):
            raise ValueError('This fleet is already registered')
        Fleet(home).status()  # Validate a saved fleet before trusting its registry entry.
        entry = {'id': uuid.uuid4().hex[:12], 'name': str(name or Path(home).name)[:100], 'home': home}
        atomic_json(self.index, entries + [entry])
        tasks = json.loads(Fleet(home).manifest.read_text())['tasks']
        self.save_projects([task['repo'] for task in tasks])
        return entry

    def resume(self, ident):
        entry = self.entry(ident)
        if not entry:
            raise ValueError('Unknown fleet')
        if self.running(entry):
            raise ValueError('The manager is already running')
        if any(key[0] == ident and not terminal.exited and terminal.mode == 'shell' for key, terminal in self.terminals.items()):
            raise ValueError('Close the task terminal before resuming workers')
        fleet = Fleet(entry['home'])
        # Refuse takeover of an externally opened keeper or workspace as well.
        for task, run in fleet.runs():
            if task.get('terminal_requested') or task.get('delete_requested'):
                continue
            if lock_held(run.path('keeper.lock')):
                raise ValueError('A task keeper or terminal already owns this run')
        with (fleet.home / 'manager.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, '-m', 'loop', 'supervise', 'resume', str(fleet.home)],
                                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True)
        self.processes[ident] = proc
        import threading
        threading.Thread(target=proc.wait, daemon=True).start()

    async def shutdown(self, app):
        self.stopping = True
        if hasattr(self, 'guardian_task'):
            self.guardian_task.cancel()
            await asyncio.gather(self.guardian_task, return_exceptions=True)
        for terminal in self.terminals.values():
            await terminal.close(detach=True)

    async def cleanup(self, app):
        for terminal in self.terminals.values():
            await terminal.close(detach=True)
        self.registry_lock.close()
        # Managers and workers are deliberately detached from the browser server.


@web.middleware
async def boundary(request, handler):
    controller = request.app[APP_KEY]
    host = request.host
    expected_port = request.transport.get_extra_info('sockname')[1]
    if host not in (f'127.0.0.1:{expected_port}', f'localhost:{expected_port}'):
        raise web.HTTPForbidden(text='Only localhost hosts are accepted')
    origin = request.headers.get('Origin')
    if origin and origin != f'http://{host}':
        raise web.HTTPForbidden(text='Cross-origin requests are blocked')
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        raise web.HTTPForbidden(text='Cross-site requests are blocked')
    if request.path.startswith('/api/'):
        if not secrets.compare_digest(request.cookies.get(COOKIE, ''), controller.token):
            raise web.HTTPUnauthorized(text='Open the local app first')
        if request.method != 'GET' and request.headers.get('X-Loop-Request') != '1':
            raise web.HTTPForbidden(text='Missing local request header')
    try:
        response = await handler(request)
    except (ValueError, OSError, RuntimeError) as exc:
        return web.json_response({'error': str(exc)}, status=409 if isinstance(exc, BlockingIOError) else 400)
    if not isinstance(response, web.WebSocketResponse):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
    return response


async def index(request):
    revision = ui_revision()
    html = (ASSETS / 'index.html').read_text()
    html = html.replace('href="/styles.css"', f'href="/styles.css?v={revision}"')
    html = html.replace('src="/app.js"', f'src="/app.js?v={revision}"')
    html = html.replace('</head>', f'<meta name="loop-ui-revision" content="{revision}"></head>')
    response = web.Response(text=html, content_type='text/html')
    response.set_cookie(COOKIE, request.app[APP_KEY].token, httponly=True, samesite='Strict')
    return response


async def state(request):
    return web.json_response({'fleets': await asyncio.to_thread(request.app[APP_KEY].status)})


async def object_body(request):
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError('Request body must be a JSON object')
    return body


async def goal_template(request):
    from .supervise import GOAL_TEMPLATE
    try:
        template = GOAL_TEMPLATE.read_text()
    except OSError:
        template = ''
    return web.json_response({'template': template if '{instructions}' in template else ''})


async def models(request):
    c = request.app[APP_KEY]
    async with c.mutation:
        if request.method == 'POST':
            body = await object_body(request)
            result = await asyncio.to_thread(remember, c.home, body.get('provider'), body.get('model'))
        else:
            result = await asyncio.to_thread(catalog, c.home)
    return web.json_response({'providers': result})


async def supervisor_state(request):
    c = request.app[APP_KEY]
    async with c.mutation:
        result = await asyncio.to_thread(c.supervisor_status)
    return web.json_response({'supervisor': result})


async def supervisor_settings(request):
    c = request.app[APP_KEY]
    body = await object_body(request)
    async with c.mutation:
        fleet = c.fleet(c.supervisor()['id'])
        spec = json.loads(fleet.manifest.read_text())
        proposed = {**spec, 'supervisor': body.get('supervisor', spec['supervisor']),
                    'max_parallel': body.get('max_parallel', spec['max_parallel'])}
        keeper = body.get('keeper', spec.get('keeper', {'enabled': False}))
        if (not isinstance(keeper, dict) or type(keeper.get('enabled')) is not bool
                or not isinstance(keeper.get('model', ''), str) or not keeper.get('model', '').strip()):
            raise ValueError('Keeper settings need enabled and a local model name')
        # Existing imported runs retain their original worktree identities.
        checked = fleet.normalized({**proposed, 'tasks': []}, fleet.home, allow_empty=True)
        atomic_json(fleet.manifest, {**spec, 'supervisor': checked['supervisor'],
                                    'max_parallel': checked['max_parallel'], 'keeper': keeper})
    return web.json_response({'ok': True})


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def save_attachments(home, tasks):
    """Write uploaded files under the supervisor home and reference them in each goal."""
    result = []
    for task in tasks:
        if not isinstance(task, dict) or not task.get('attachments'):
            result.append({k: v for k, v in task.items() if k != 'attachments'} if isinstance(task, dict) else task)
            continue
        files = task['attachments']
        if not isinstance(files, list) or len(files) > 20:
            raise ValueError('attachments must be a list of at most 20 files')
        ident = task.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,60}', ident):
            raise ValueError('Task ids must be unique lowercase slugs')
        folder = Path(home) / 'attachments' / ident
        folder.mkdir(parents=True, exist_ok=True)
        saved = []
        for index, item in enumerate(files):
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('data'), str):
                raise ValueError('Each attachment needs a name and base64 data')
            name = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(item['name']).name).lstrip('.') or 'file'
            try:
                data = base64.b64decode(item['data'], validate=True)
            except (binascii.Error, ValueError):
                raise ValueError('Attachment ' + name + ' is not valid base64')
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise ValueError('Attachment ' + name + ' is larger than 20 MB')
            path = folder / (str(index + 1) + '-' + name)
            path.write_bytes(data)
            saved.append(str(path))
        goal = str(task.get('goal', '')).rstrip() + '\n\nAttached files (read them as needed):\n' + '\n'.join('- ' + p for p in saved)
        result.append({**{k: v for k, v in task.items() if k != 'attachments'}, 'goal': goal})
    return result


def search_agents(fleet, query):
    """Case-insensitive search of each agent's goal, follow-up notes and recent conversation."""
    needle = query.lower()
    hits = {}

    def snippet(text, where):
        at = text.lower().find(needle)
        if at < 0:
            return None
        piece = text[max(0, at - 40):at + len(query) + 60].replace('\\n', ' ').replace('\n', ' ')
        return {'where': where, 'text': piece.strip()}

    for task, run in fleet.runs():
        found = snippet(str(task.get('goal', '')), 'goal')
        if not found:
            try:
                found = snippet(run.path('inbox.jsonl').read_text(), 'message')
            except OSError:
                pass
        if not found:
            for path in sorted(run.path('logs').glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:6]:
                found = snippet(tail(path, 200000), 'conversation')
                if found:
                    break
        if found:
            hits[task['id']] = found
    return hits


async def supervisor_search(request):
    c = request.app[APP_KEY]
    query = request.query.get('q', '').strip()
    if len(query) < 2 or len(query) > 200:
        return web.json_response({'matches': {}})
    fleet = c.fleet(c.supervisor()['id'])
    return web.json_response({'matches': await asyncio.to_thread(search_agents, fleet, query)})


async def supervisor_agents(request):
    c = request.app[APP_KEY]
    body = await object_body(request)
    async with c.mutation:
        entry = c.supervisor()
        fleet = c.fleet(entry['id'])
        tasks = body.get('tasks')
        if not isinstance(tasks, list) or not tasks:
            raise ValueError('Add at least one agent')
        tasks = await asyncio.to_thread(save_attachments, fleet.home, tasks)
        added = await asyncio.to_thread(fleet.add_agents, tasks)
        c.save_projects([t['repo'] for t in added])
        start_error = None
        if body.get('start') and not c.running(entry):
            spec = json.loads(fleet.manifest.read_text())
            atomic_json(fleet.manifest, {**spec, 'desired_running': True})
            (fleet.home / 'stop').unlink(missing_ok=True)
            try:
                c.resume(entry['id'])
            except (OSError, ValueError, RuntimeError) as exc:
                atomic_json(fleet.manifest, {**spec, 'desired_running': False})
                start_error = str(exc)
    return web.json_response({'added': [t['id'] for t in added], 'start_error': start_error}, status=201)


async def supervisor_agent_action(request):
    c = request.app[APP_KEY]
    async with c.mutation:
        fleet = c.fleet(c.supervisor()['id'])
        if request.match_info['op'] == 'remove':
            spec = json.loads(fleet.manifest.read_text())
            task = next((t for t in spec['tasks'] if t['id'] == request.match_info['task']), {})
            if task.get('terminal_requested'):
                raise ValueError('Return the interactive terminal before removing this agent')
        fleet.enable_agent(request.match_info['task'], request.match_info['op'] == 'restore')
    return web.json_response({'ok': True, 'applies': 'current turn finishes; no new turn is launched'})


async def supervisor_agent_delete(request):
    c = request.app[APP_KEY]
    body = await object_body(request)
    if body.get('confirm') is not True:
        raise ValueError('Confirm permanent deletion and cleanup')
    async with c.mutation:
        entry = c.supervisor()
        fleet = c.fleet(entry['id'])
        spec = json.loads(fleet.manifest.read_text())
        task = next((t for t in spec['tasks'] if t['id'] == request.match_info['task']), None)
        if task is None:
            raise ValueError('Unknown agent')
        run = c.run_for(entry['id'], task['id'])
        if terminal_owner_path(run.dir, run.worktree).exists():
            raise ValueError('Return and reconcile the interactive terminal before deleting this agent')
        terminal = c.terminals.get((entry['id'], task['id']))
        if terminal:
            await terminal.close()
            c.terminals.pop((entry['id'], task['id']), None)
        task.update(enabled=False, terminal_requested=False,
                    delete_requested=task.get('delete_requested') or uuid.uuid4().hex)
        task.pop('delete_error', None)
        atomic_json(fleet.manifest, spec)
        await c.finish_deletions(entry)
        remaining = json.loads(fleet.manifest.read_text())
        pending = next((t for t in remaining['tasks'] if t['id'] == task['id']), None)
        queued = pending is not None
        cleanup_error = pending.get('delete_error') if pending else None
        if queued and run.state().get('attempt') and not c.running(entry):
            # Reconcile a detached turn before cleaning its workspace.
            atomic_json(fleet.manifest, {**remaining, 'desired_running': True})
            (fleet.home / 'stop').unlink(missing_ok=True)
            c.resume(entry['id'])
    return web.json_response({'ok': True, 'queued': queued, 'cleanup_error': cleanup_error}, status=202 if queued else 200)


async def supervisor_action(request):
    c = request.app[APP_KEY]
    async with c.mutation:
        entry = c.supervisor()
        fleet = c.fleet(entry['id'])
        spec = json.loads(fleet.manifest.read_text())
        start = request.match_info['op'] == 'resume'
        atomic_json(fleet.manifest, {**spec, 'desired_running': start})
        if start:
            (fleet.home / 'stop').unlink(missing_ok=True)
            try:
                c.resume(entry['id'])
            except (OSError, ValueError, RuntimeError):
                atomic_json(fleet.manifest, {**spec, 'desired_running': False})
                raise
        else:
            (fleet.home / 'stop').touch()
    return web.json_response({'ok': True})


async def supervisor_import(request):
    c = request.app[APP_KEY]
    body = await object_body(request)
    if not isinstance(body.get('home'), str) or not body['home'].strip():
        raise ValueError('Enter a saved run directory')
    source = Fleet(Path(body['home']).expanduser())
    source.status()
    if lock_held(source.home / 'fleet.lock'):
        raise ValueError('Stop the source manager before importing its agents')
    async with c.mutation:
        fleet = c.fleet(c.supervisor()['id'])
        spec = json.loads(fleet.manifest.read_text())
        known = {t['run_id'] for t in spec['tasks']}
        ids = {t['id'] for t in spec['tasks']}
        count = 0
        for original in json.loads(source.manifest.read_text())['tasks']:
            if original['run_id'] in known:
                continue
            task = dict(original)
            base, counter = task['id'], 2
            while task['id'] in ids:
                task['id'] = base[:50] + '-' + str(counter); counter += 1
            ids.add(task['id']); known.add(task['run_id']); count += 1
            spec['tasks'].append(task)
        atomic_json(fleet.manifest, spec)
        c.save_projects([t['repo'] for t in spec['tasks']])
    return web.json_response({'added': count})


async def supervisor_events(request):
    c = request.app[APP_KEY]
    async with c.mutation:
        c.supervisor()
    response = web.StreamResponse(headers={'Content-Type': 'text/event-stream',
                                           'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
    await response.prepare(request)
    await response.write(b'retry: 2000\n\n')
    previous = None
    try:
        while not c.stopping:
            async with c.mutation:
                payload = json.dumps(await asyncio.to_thread(c.supervisor_status))
            if payload != previous:
                await response.write(('event: state\ndata: ' + payload + '\n\n').encode())
                previous = payload
            await response.write(b'event: tick\ndata: {}\n\n')
            await asyncio.sleep(1)
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    return response


async def projects(request):
    c = request.app[APP_KEY]
    async with c.mutation:
        if request.method == 'GET':
            library = c.projects()
            return web.json_response({**library, 'projects': [
                {**p, 'available': Path(p['path']).is_dir()} for p in library['projects']]})
        body = await object_body(request)
        action = body.get('action', 'add')
        truncated = False
        if action == 'add':
            paths = body.get('paths', [])
            if not isinstance(paths, list) or not paths or len(paths) > 500:
                raise ValueError('Enter between 1 and 500 repository paths')
            roots = [await asyncio.to_thread(c.repository_root, path) for path in paths]
            added = c.save_projects(roots)
        elif action in ('scan', 'rescan'):
            folders = ([{'path': body.get('folder'), 'depth': body.get('depth', 4)}]
                       if action == 'scan' else c.projects()['folders'])
            added = 0
            for folder in folders:
                found, partial, root = await asyncio.to_thread(c.scan_projects, folder['path'], folder['depth'])
                added += c.save_projects(found)
                truncated = truncated or partial
                library = c.projects()
                library['folders'] = [f for f in library['folders'] if f['path'] != root] + [
                    {'path': root, 'depth': folder['depth']}]
                atomic_json(c.project_index, library)
        elif action == 'remove':
            library = c.projects()
            path = body.get('path')
            if not isinstance(path, str) or not path.strip():
                raise ValueError('Enter a project path to remove')
            path = str(Path(path).expanduser().resolve())
            library['projects'] = [p for p in library['projects'] if p['path'] != path]
            atomic_json(c.project_index, library)
            added = 0
        else:
            raise ValueError('Unknown project library action')
    return web.json_response({'added': added, 'truncated': truncated})


async def create(request):
    c = request.app[APP_KEY]
    body = await object_body(request)
    if not isinstance(body.get('manifest'), dict):
        raise ValueError('A JSON manifest object is required')
    async with c.mutation:
        ident = uuid.uuid4().hex[:12]
        home = c.home / 'fleets' / ident
        home.mkdir(parents=True)
        source = home / 'input.json'
        atomic_json(source, body['manifest'])
        await asyncio.to_thread(Fleet(home).create, source)
        entry = c.register(home, body.get('name', 'New fleet'))
        if body.get('start', False):
            c.resume(entry['id'])
    return web.json_response(entry, status=201)


async def import_fleet(request):
    c = request.app[APP_KEY]
    body = await object_body(request)
    if not isinstance(body.get('home'), str) or not body['home'].strip():
        raise ValueError('A saved fleet directory is required')
    async with c.mutation:
        entry = c.register(body['home'], body.get('name'))
    return web.json_response(entry, status=201)


async def action(request):
    c = request.app[APP_KEY]
    ident = request.match_info['fleet']
    op = request.match_info['op']
    async with c.mutation:
        fleet = c.fleet(ident)
        if op == 'resume':
            c.resume(ident)
        elif op == 'stop':
            (fleet.home / 'stop').touch()
        else:
            raise web.HTTPNotFound()
    return web.json_response({'ok': True})


async def note(request):
    c = request.app[APP_KEY]
    body = await object_body(request)
    text = body.get('text', '')
    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
        raise ValueError('Enter a note of up to 10,000 characters')
    started, start_error = False, None
    async with c.mutation:
        ident = request.match_info['fleet']
        fleet = c.fleet(ident)
        fleet.note(request.match_info['task'], text)
        spec = json.loads(fleet.manifest.read_text())
        atomic_json(fleet.manifest, {**spec, 'desired_running': True})
        (fleet.home / 'stop').unlink(missing_ok=True)
        if not c.running(c.entry(ident)):
            try:
                c.resume(ident)
                started = True
            except (OSError, ValueError, RuntimeError) as exc:
                # Preserve the accepted message even if a shell currently owns the run.
                atomic_json(fleet.manifest, {**spec, 'desired_running': False})
                start_error = str(exc)
    return web.json_response({'ok': True, 'queued': True, 'started': started, 'start_error': start_error})


async def task_settings(request):
    c = request.app[APP_KEY]
    body = await object_body(request)
    if 'allow_subagents' in body and type(body['allow_subagents']) is not bool:
        raise ValueError('allow_subagents must be a boolean')
    if 'full_access' in body and type(body['full_access']) is not bool:
        raise ValueError('full_access must be a boolean')
    if 'worker_effort' in body and body['worker_effort'] not in EFFORTS:
        raise ValueError('Unsupported worker effort')
    if 'model' in body and (not isinstance(body['model'], str) or len(body['model']) > 200):
        raise ValueError('model must be a model ID of at most 200 characters')
    async with c.mutation:
        run = c.run_for(request.match_info['fleet'], request.match_info['task'])
        config = run.load('config.json', {})
        if 'full_access' in body:
            config['full_access'] = body['full_access']
        if 'worker_effort' in body:
            config['worker_effort'] = body['worker_effort']
        if 'allow_subagents' in body:
            config['allow_subagents'] = body['allow_subagents']
        if 'model' in body:
            # Refuse concurrent state changes; stop scheduling before changing a model.
            state = run.state()
            if c.running(c.entry(request.match_info['fleet'])) or state.get('attempt') or lock_held(run.path('keeper.lock')):
                raise ValueError('Stop scheduling and wait for the current turn before changing the model')
            model = body['model'].strip() or None
            provider = state['provider']
            config.setdefault('worker_models', {})[provider] = model
            if provider == config.get('workers', ['claude'])[0]:
                config['worker_model'] = model
            state.update(model=model, session_id=None, retry_at=0, wait_reason='', needs_help=False,
                         state='ready' if state['state'] != 'complete' else 'complete')
            state.setdefault('sessions', {}).pop(provider, None)
            run.save_state(state)
            if model:
                remember(c.home, provider, model)
        run.save('config.json', config)
    return web.json_response({'ok': True, 'applies': 'next worker turn'})


async def logs(request):
    c = request.app[APP_KEY]
    if request.match_info.get('task'):
        run = c.run_for(request.match_info['fleet'], request.match_info['task'])
        paths = sorted(run.path('logs').glob('*.jsonl'), key=lambda p: p.stat().st_mtime)
        latest = paths[-1] if paths else None
        turns = []
        for path in paths[-6:]:
            attempt = run.load('attempts/' + path.stem.removeprefix('objective-') + '.json', {})
            turns.append({'id': path.stem, 'at': attempt.get('created_at', path.stat().st_mtime),
                          'output': tail(path, 48000), 'stderr': tail(path.with_suffix('.stderr'), 4000)})
        notes = []
        state = run.state()
        try:
            for index, line in enumerate(run.path('inbox.jsonl').read_text().splitlines()):
                try:
                    note = json.loads(line)
                    notes.append({'text': note.get('text', ''), 'at': note.get('at', 0),
                                  'delivery': ('delivered' if index < state.get('delivered_inbox', 0) else
                                               'saved' if 'delivered_inbox' not in state and index < state.get('inbox_seen', 0) else 'queued')})
                except ValueError:
                    continue
        except FileNotFoundError:
            pass
        return web.json_response({'output': tail(latest) if latest else '',
                                  'stderr': tail(latest.with_suffix('.stderr')) if latest else '',
                                  'journal': tail(run.path('journal.jsonl'), 20000),
                                  'turns': turns, 'notes': notes[-30:]})
    return web.json_response({'output': tail(c.fleet(request.match_info['fleet']).home / 'manager.log')})


def change_git(worktree, *args, limit=1024 * 1024, diff=False):
    """Bound captured output, disable repository diff drivers and never mutate Git."""
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                ['git', '-C', str(worktree), '--no-pager', *args],
                stdout=output, stderr=subprocess.PIPE, timeout=20,
                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_LITERAL_PATHSPECS': '1'})
        except subprocess.TimeoutExpired as exc:
            raise ValueError('Reading changes timed out; refresh to try again') from exc
        if result.returncode not in ((0, 1) if diff else (0,)):
            raise ValueError(result.stderr.decode(errors='replace').strip() or 'Cannot read worktree changes')
        output.seek(0)
        data = output.read(limit + 1)
    return data[:limit], len(data) > limit


def worktree_changes(run, path=None):
    base = run.state().get('base')
    if not base:
        raise ValueError('This agent has no saved starting commit')
    raw, truncated = change_git(run.worktree, 'diff', '--no-ext-diff', '--no-textconv',
                                '--no-renames', '--numstat', '-z', base, '--')
    if truncated:
        raise ValueError('Changed file list is too large to display')
    files = []
    for record in raw.split(b'\0'):
        if not record:
            continue
        added, removed, name = record.split(b'\t', 2)
        files.append({'path': os.fsdecode(name), 'added': int(added) if added != b'-' else None,
                      'removed': int(removed) if removed != b'-' else None, 'status': 'Changed'})
    raw, truncated = change_git(run.worktree, 'ls-files', '--others', '--exclude-standard', '-z')
    if truncated:
        raise ValueError('New file list is too large to display')
    files.extend({'path': os.fsdecode(name), 'added': None, 'removed': None, 'status': 'New'}
                 for name in raw.split(b'\0') if name)
    files.sort(key=lambda item: item['path'])
    if path is None:
        return {'base': base, 'files': files}
    item = next((item for item in files if item['path'] == path), None)
    if item is None:
        raise ValueError('File is no longer in this agent’s changed file list')
    options = ('--no-ext-diff', '--no-textconv', '--no-color', '--no-renames')
    if item['status'] == 'New':
        args = ('diff', *options, '--no-index', '--', '/dev/null', str(run.worktree / path))
    else:
        args = ('diff', *options, base, '--', path)
    raw, truncated = change_git(run.worktree, *args, limit=256 * 1024, diff=True)
    return {'path': path, 'diff': raw.decode(errors='replace'), 'truncated': truncated}


async def changes(request):
    run = request.app[APP_KEY].run_for(request.match_info['fleet'], request.match_info['task'])
    return web.json_response(await asyncio.to_thread(worktree_changes, run, request.query.get('path')))


async def native_action(request):
    c = request.app[APP_KEY]
    key = (request.match_info['fleet'], request.match_info['task'])
    start_error = None
    body = await object_body(request)
    async with c.mutation:
        fleet = c.fleet(key[0])
        spec = json.loads(fleet.manifest.read_text())
        task = next((t for t in spec['tasks'] if t['id'] == key[1]), None)
        if not task:
            raise ValueError('Unknown agent')
        if task.get('delete_requested'):
            raise ValueError('This agent is queued for deletion')
        run = c.run_for(*key)
        terminal = c.terminals.get(key)
        if request.match_info['op'] in ('request', 'session'):
            if not run.worktree.is_dir():
                raise ValueError('This agent worktree is missing; restore it before reopening its CLI')
            config = terminal_config(run, body)
            old_config = terminal_config(run)
            if task.get('terminal_requested') and config != old_config:
                raise ValueError('Return the terminal before changing its program')
            run.save('native-config.json', config)
            session = body.get('session_id')
            if session is not None:
                if not isinstance(session, str) or not session.strip() or len(session) > 300 or '\x00' in session:
                    raise ValueError('Enter the exact session ID')
                if config['provider'] == 'custom' and not config.get('resume_command'):
                    raise ValueError('Set a resume command with {session_id} before saving a custom chat')
                store_session(run, config['provider'], session.strip(), config, manual=True)
                if terminal and terminal.mode != 'shell':
                    terminal.session_id = session.strip()
                    terminal.observed_sessions.add(session.strip())
                    state = run.state()
                    state['interactive_session']['session_id'] = session.strip()
                    if config['provider'] == state['provider']:
                        state['session_id'] = session.strip()
                        state.setdefault('sessions', {})[config['provider']] = session.strip()
                    run.save_state(state)
            if request.match_info['op'] == 'session':
                return web.json_response({'ok': True})
            argv, _, _ = agent_terminal_command(run, config)
            if not shutil.which(argv[0]):
                raise ValueError('CLI executable not found: ' + argv[0])
            if not task.get('enabled', True):
                raise ValueError('Restore this agent before opening its terminal')
            if terminal and not terminal.exited and terminal.mode == 'shell':
                raise ValueError('Close the worktree shell before opening an agent CLI')
            task['terminal_requested'] = True
            state = run.state()
            reconcile = bool(state.get('attempt') or state['state'] in ('working', 'verifying', 'commit_pending'))
            if reconcile:
                spec['desired_running'] = True
                (fleet.home / 'stop').unlink(missing_ok=True)
            atomic_json(fleet.manifest, spec)
            if reconcile and not c.running(c.entry(key[0])):
                c.resume(key[0])  # Drain/reconcile the existing turn before yielding its locks.
        else:
            opened = run.state().get('interactive_session')
            marker = terminal_owner_path(run.dir, run.worktree)
            if marker.exists():
                if terminal and terminal.mode == 'shell':
                    raise ValueError('This agent has a worktree shell open')
                session = TmuxSession.recover(run.dir, run.worktree)
                if body.get('verified_no_writers') is not True:
                    if terminal:
                        await terminal.close()
                        c.terminals.pop(key, None)
                    else:
                        await asyncio.to_thread(session.stop)
                    return web.json_response({'ok': True, 'needs_verification': True,
                                              'worktree': str(run.worktree)})
                with ExitStack() as locks:
                    locks.enter_context(locked(run.path('keeper.lock')))
                    for path in dict.fromkeys((workspace_lock_path(run.dir, run.worktree), legacy_workspace_lock_path(run.dir, run.worktree))):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        locks.enter_context(locked(path))
                    session.reconcile(verified_no_writers=True)
                terminal = None
            if terminal:
                if terminal.mode == 'shell':
                    raise ValueError('This agent has a worktree shell open')
                await terminal.close()
                c.terminals.pop(key, None)
            if opened:
                state = run.state()
                state.pop('interactive_session', None)
                record_usage(state, 'worker', opened.get('provider', 'codex'), {}, 'interactive:' + opened['id'],
                             model=state.get('model'), now=time.time())
                run.save_state(state)
                run.note('The operator returned the interactive agent terminal. Inspect and verify the current worktree; preserve manual changes and finish the objective.')
            task['terminal_requested'] = False
            if opened:
                spec['desired_running'] = True
                (fleet.home / 'stop').unlink(missing_ok=True)
            atomic_json(fleet.manifest, spec)
            if opened and not c.running(c.entry(key[0])):
                try:
                    c.resume(key[0])
                except (OSError, ValueError, RuntimeError) as exc:
                    atomic_json(fleet.manifest, {**spec, 'desired_running': False})
                    start_error = str(exc)
    ready = (request.match_info['op'] == 'request' and not reconcile
             and not lock_held(run.path('keeper.lock')))
    return web.json_response({'ok': True, 'start_error': start_error, 'native_ready': ready})


async def terminal_close(request):
    c = request.app[APP_KEY]
    key = (request.match_info['fleet'], request.match_info['task'])
    async with c.mutation:
        terminal = c.terminals.get(key)
        if terminal:
            await terminal.close()
            if c.terminals.get(key) is terminal:
                del c.terminals[key]
    return web.json_response({'ok': True})


async def terminal_socket(request):
    c = request.app[APP_KEY]
    if request.headers.get('Origin') != f'http://{request.host}':
        raise web.HTTPForbidden(text='Terminal requires the local app origin')
    key = (request.match_info['fleet'], request.match_info['task'])
    async with c.mutation:
        run = c.run_for(*key)
        spec = json.loads(c.fleet(key[0]).manifest.read_text())
        task = next(t for t in spec['tasks'] if t['id'] == key[1])
        if task.get('delete_requested'):
            raise ValueError('This agent is queued for deletion')
        mode = request.query.get('mode', 'shell')
        if mode not in ('shell', 'codex', 'agent'):
            raise ValueError('Unknown terminal mode')
        if mode != 'shell':
            spec = json.loads(c.fleet(key[0]).manifest.read_text())
            task = next(t for t in spec['tasks'] if t['id'] == key[1])
            if not task.get('terminal_requested'):
                raise ValueError('Request the interactive terminal first')
        elif c.running(c.entry(key[0])):
            raise ValueError('Stop the manager and reconcile active workers before opening a shell')
        terminal = c.terminals.get(key)
        if terminal and terminal.exited:
            await terminal.close()
            terminal = None
        if terminal and (terminal.mode == 'shell') != (mode == 'shell'):
            raise ValueError('Close the other terminal before switching modes')
        if terminal is None:
            terminal = Terminal(run, c.shell, mode, c.home, c.use_tmux or terminal_owner_path(run.dir, run.worktree).exists())
            c.terminals[key] = terminal
        if terminal.ws and not terminal.ws.closed:
            if mode == 'shell' or request.query.get('takeover') != '1':
                raise ValueError('This terminal is attached in another browser tab. Select Reconnect here to move it.')
            previous = terminal.ws
            await previous.send_json({'type': 'detached', 'message': 'Terminal moved to another browser tab. Reconnect here to bring it back.'})
            await previous.close()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=65536)
        await ws.prepare(request)
        terminal.ws = ws
    output = asyncio.create_task(terminal.output(ws))
    try:
        async for msg in ws:
            terminal.last_seen = time.monotonic()
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get('type') == 'input':
                    text = data.get('data', '')
                    if not isinstance(text, str) or len(text) > 16384:
                        raise ValueError('Terminal input is too large')
                    remaining = memoryview(text.encode())
                    while remaining:
                        try:
                            remaining = remaining[os.write(terminal.master, remaining):]
                        except BlockingIOError:
                            await asyncio.sleep(0.01)
                elif data.get('type') == 'resize':
                    terminal.resize(data.get('cols'), data.get('rows'))
    except (ValueError, OSError):
        await ws.close(code=1008)
    finally:
        output.cancel()
        await asyncio.gather(output, return_exceptions=True)
        if terminal.ws is ws:
            terminal.ws = None
    return ws


async def deployment_log(request):
    path=deploy.target_path(ROOT).parent/'jobs'/(request.match_info['job']+'.log')
    if not path.is_file():
        raise web.HTTPNotFound(text='Deployment log not found')
    return web.Response(text=tail(path),content_type='text/plain')


async def deploy_action(request):
    c = request.app[APP_KEY]
    body = await object_body(request)
    source = body.get('source')
    message = body.get('message', '')
    if not isinstance(message, str) or len(message) > 1000:
        raise ValueError('Deployment message must be text of at most 1000 characters')
    if not isinstance(source, str) or not source:
        raise ValueError('Select a worker worktree to merge and redeploy')
    # Only registered agents in this repository are exposed through the browser.
    known = [Path(t['worktree']).resolve() for entry in c.status() for t in entry['tasks']]
    if Path(source).resolve() not in known or deploy.common_dir(source) != deploy.common_dir(ROOT):
        raise ValueError('Choose a worker from the repository serving this workspace')
    async with c.mutation:
        result = await asyncio.to_thread(deploy.submit, source, message)
    return web.json_response(result, status=202)


def create_app(home, use_tmux=False):
    app = web.Application(middlewares=[boundary], client_max_size=64 * 1024 * 1024)
    app[APP_KEY] = Controller(home, use_tmux)
    app.router.add_get('/', index)
    def file_handler(path):
        async def serve(request):
            return web.FileResponse(path)
        return serve
    for name in ('app.js', 'styles.css', 'logo.svg'):
        app.router.add_get('/' + name, file_handler(ASSETS / name))
    dependencies = ASSETS / 'node_modules' / '@xterm'
    for route, path in {'xterm.js': dependencies / 'xterm' / 'lib' / 'xterm.js',
                        'xterm.css': dependencies / 'xterm' / 'css' / 'xterm.css',
                        'fit.js': dependencies / 'addon-fit' / 'lib' / 'addon-fit.js',
                        'web-links.js': dependencies / 'addon-web-links' / 'lib' / 'addon-web-links.js'}.items():
        app.router.add_get('/vendor/' + route, file_handler(path))
    app.router.add_get('/api/state', state)
    app.router.add_post('/api/deploy', deploy_action)
    app.router.add_get('/api/deploy/jobs/{job:[0-9a-f]{16}}/log', deployment_log)
    app.router.add_get('/api/supervisor', supervisor_state)
    app.router.add_get('/api/supervisor/search', supervisor_search)
    app.router.add_get('/api/events', supervisor_events)
    app.router.add_post('/api/supervisor/settings', supervisor_settings)
    app.router.add_post('/api/supervisor/agents', supervisor_agents)
    app.router.add_post('/api/supervisor/agents/{task}/delete', supervisor_agent_delete)
    app.router.add_post('/api/supervisor/agents/{task}/{op:remove|restore}', supervisor_agent_action)
    app.router.add_post('/api/supervisor/{op:resume|stop}', supervisor_action)
    app.router.add_post('/api/supervisor/import', supervisor_import)
    app.router.add_get('/api/goal-template', goal_template)
    app.router.add_get('/api/models', models)
    app.router.add_post('/api/models', models)
    app.router.add_get('/api/projects', projects)
    app.router.add_post('/api/projects', projects)
    app.router.add_post('/api/fleets', create)
    app.router.add_post('/api/import', import_fleet)
    app.router.add_post('/api/fleets/{fleet}/{op:resume|stop}', action)
    app.router.add_post('/api/fleets/{fleet}/tasks/{task}/note', note)
    app.router.add_post('/api/fleets/{fleet}/tasks/{task}/settings', task_settings)
    app.router.add_get('/api/fleets/{fleet}/tasks/{task}/logs', logs)
    app.router.add_get('/api/fleets/{fleet}/tasks/{task}/changes', changes)
    app.router.add_get('/api/fleets/{fleet}/logs', logs)
    app.router.add_get('/api/fleets/{fleet}/tasks/{task}/terminal', terminal_socket)
    app.router.add_post('/api/fleets/{fleet}/tasks/{task}/terminal/close', terminal_close)
    app.router.add_post('/api/fleets/{fleet}/tasks/{task}/native/{op:request|release|session}', native_action)
    app.on_startup.append(app[APP_KEY].recover_native)
    app.on_startup.append(app[APP_KEY].guardian)
    app.on_shutdown.append(app[APP_KEY].shutdown)
    app.on_cleanup.append(app[APP_KEY].cleanup)
    return app


def main():
    parser = argparse.ArgumentParser(description='Local browser interface for agent-loop')
    parser.add_argument('--home', default=str(ROOT / '.agent-loop' / 'web'))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--tmux-terminal', action=argparse.BooleanOptionalAction, default=None,
                        help='Enable or disable persistent agent terminals (enabled when tmux is installed)')
    args = parser.parse_args()
    if not (ASSETS / 'node_modules' / '@xterm' / 'xterm').exists():
        parser.error('Browser assets are missing. Run scripts/setup-supervise-web first.')
    tmux_available = bool(shutil.which('tmux'))
    if args.tmux_terminal and not tmux_available:
        parser.error('The experimental tmux terminal requires tmux on PATH.')
    app = create_app(args.home, use_tmux=tmux_available if args.tmux_terminal is None else args.tmux_terminal)
    deploy.register(ROOT, args.home, args.port)
    print(f'Open http://127.0.0.1:{args.port} — local supervisor', flush=True)
    web.run_app(app, host='127.0.0.1', port=args.port, print=None)


if __name__ == '__main__':
    main()
