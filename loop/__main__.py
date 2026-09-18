import argparse
import json
import signal
import time
from pathlib import Path

from .adapters import provider_limits
from .engine import Engine


def main():
    def stop(_signal, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description="A model-free keep-alive for coding CLIs")
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("plan")
    run.add_argument("--once", action="store_true", help="One scheduler tick; no waiting")
    run.add_argument("--review", action="store_true",
                      help="Self-review the plan before starting; abort if declined")
    start = run.add_mutually_exclusive_group()
    start.add_argument("--base", help="Start a new run from this git ref instead of HEAD")
    start.add_argument("--from-run", help="Start a new run from another run's branch")
    status = sub.add_parser("status")
    status.add_argument("--run", help="Compact table for one run instead of the full JSON state")
    logs = sub.add_parser("logs", help="Print (or follow) the latest attempt log")
    logs.add_argument("run_id")
    logs.add_argument("task_id", nargs="?")
    logs.add_argument("--follow", "-f", action="store_true")
    sub.add_parser("supervisors", help="List runs whose supervisor is currently running")
    stop_parser = sub.add_parser("stop", help="Stop a run's supervisor; progress is kept")
    stop_parser.add_argument("run_id")
    amend = sub.add_parser("amend", help="Apply an edited plan to an existing run")
    amend.add_argument("plan")
    amend.add_argument("--budget", action="append", default=[], metavar="PROVIDER=TOKENS",
                       help="Set provider_token_budgets entries (written back to the plan file)")
    amend.add_argument("--set", action="append", default=[], metavar="KEY=JSON",
                       help="Set a top-level plan setting, e.g. worker_idle_timeout_seconds=600")
    clean = sub.add_parser("clean", help="Remove worktrees of superseded/merged runs (dry run by default)")
    clean.add_argument("--yes", action="store_true", help="Actually remove them")
    clean.add_argument("--include-unpublished", action="store_true",
                       help="Also clean finished runs that were never merged")
    sub.add_parser("codex-quota")
    usage = sub.add_parser("usage", help="Read usage/quota telemetry from every provider")
    usage.add_argument("--timeout", type=int, default=30)
    planner = sub.add_parser("plan", help="Scout context with a cheap model, then author a plan")
    planner.add_argument("request")
    planner.add_argument("--output", required=True)
    planner.add_argument("--context-provider", choices=["codex", "claude", "antigravity"], default="antigravity")
    planner.add_argument("--context-model", default="gemini-3.8-flash-low")
    planner.add_argument("--planner-provider", choices=["codex", "claude", "antigravity"], default="antigravity")
    planner.add_argument("--planner-model", default="gemini-3.1-pro-high")
    planner.add_argument("--usage-timeout", type=int, default=30)
    planner.add_argument("--timeout", type=int, default=180)
    release = sub.add_parser("publish", help="Explicitly push a milestone and create/reuse its PR")
    release.add_argument("run_id")
    release.add_argument("--github", required=True, help="Exact OWNER/REPOSITORY matching origin")
    release.add_argument("--base", default="main")
    release.add_argument("--merge", action="store_true", help="Merge only with protected base and passing required checks")
    release.add_argument("--review", action="store_true",
                          help="Self-review the diff against task prompts before publishing; abort if declined")
    hold = sub.add_parser("hold", help="Record an observed provider reset time")
    hold.add_argument("provider", choices=["codex", "claude", "antigravity"])
    hold.add_argument("--until", type=float, required=True, help="Unix timestamp; 0 clears the hold")
    hold.add_argument("--reason", default="Manually observed quota window")
    queue = sub.add_parser("queue", help="Manage the durable objective queue")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_add = queue_sub.add_parser("add")
    queue_add.add_argument("plan")
    queue_add.add_argument("--priority", type=int, default=100)
    queue_add.add_argument("--github", help="OWNER/REPOSITORY release destination")
    queue_add.add_argument("--base", default="main")
    queue_add.add_argument("--merge", action="store_true")
    queue_sub.add_parser("list")
    service = sub.add_parser("service", help="Run the deterministic objective service")
    service.add_argument("--once", action="store_true", help="One service tick; no waiting")
    service.add_argument("--launchd", metavar="LABEL", nargs="?", const="com.agent-loop.service",
                         help="Print a launchd definition instead of running")
    sub.add_parser("learn", help="Update lessons from new events; no model calls")
    lessons = sub.add_parser("lessons", help="Show learned lessons and their status")
    lessons.add_argument("--status", default=None)
    improve_parser = sub.add_parser("improve", help="Manage supervisor self-improvement")
    improve_sub = improve_parser.add_subparsers(dest="improve_command", required=True)
    improve_sub.add_parser("propose")
    evaluate_parser = improve_sub.add_parser("evaluate")
    evaluate_parser.add_argument("candidate")
    evaluate_parser.add_argument("--baseline", default=None)
    promote_parser = improve_sub.add_parser("promote")
    promote_parser.add_argument("version")
    rollback_parser = improve_sub.add_parser("rollback")
    rollback_parser.add_argument("version")
    rollback_parser.add_argument("--reason", required=True)
    retry = sub.add_parser("retry", help="Retry a repaired authentication/configuration failure")
    retry.add_argument("run_id")
    retry.add_argument("task_id")
    lite = sub.add_parser("lite", help="Keeper loop: local rules + read-only supervisor + workers")
    lite_sub = lite.add_subparsers(dest="lite_command", required=True)
    lite_start = lite_sub.add_parser("start", help="Start a new lite run for a goal")
    lite_start.add_argument("goal")
    lite_start.add_argument("--id", help="Run id (default: slug of the goal)")
    lite_start.add_argument("--base", default="HEAD", help="Git ref to branch from")
    for target in (lite_start, lite_sub.add_parser("resume", help="Resume a lite run")):
        if target is not lite_start:
            target.add_argument("run_id")
        target.add_argument("--supervisors", help="Comma-separated preference, e.g. claude,codex")
        target.add_argument("--workers", help="Comma-separated worker providers")
        target.add_argument("--supervisor-model", action="append", default=[],
                            metavar="PROVIDER=MODEL")
        target.add_argument("--provider-order", choices=["expiring", "least_used"],
                            help="Pick providers whose long quota window resets soonest "
                                 "(default) or the least used")
        target.add_argument("--catalog", metavar="FILE",
                            help="JSON list of worker models: {provider, tier, model, effort?}")
        target.add_argument("--setup", action="append", default=[], metavar="COMMAND",
                            help="Shell command the keeper runs once in a new worktree")
        target.add_argument("--keeper-model", help="Local Ollama model for result summaries")
        target.add_argument("--reset-at-tokens", type=int,
                            help="Start a fresh supervisor session above this context size")
        target.add_argument("--max-turns", type=int, help="Stop after this many supervisor turns")
        target.add_argument("--gate", action="append", default=[], metavar="COMMAND",
                            help="Keeper-owned acceptance command required before every commit and done")
        target.add_argument("--no-audit", action="store_true",
                            help="Skip the independent audit of passing changes")
        target.add_argument("--detach", action="store_true",
                            help="Run the keeper in its own session, logging to the run directory")
    lite_note = lite_sub.add_parser("note", help="Send a note to a running (or paused) run's supervisor")
    lite_note.add_argument("run_id")
    lite_note.add_argument("text")
    lite_gate = lite_sub.add_parser("gate", help="Add a keeper-owned acceptance gate to a run")
    lite_gate.add_argument("run_id")
    lite_gate.add_argument("command")
    lite_status = lite_sub.add_parser("status")
    lite_status.add_argument("run_id")
    lite_tail = lite_sub.add_parser("tail", help="Follow the run journal")
    lite_tail.add_argument("run_id")
    args = parser.parse_args()
    if args.command == "lite":
        return lite_main(args)
    engine = Engine(args.repo)
    try:
        if args.command == "status":
            if args.run:
                print(engine.run_summary(args.run))
            else:
                print(json.dumps(engine.status(), indent=2))
        elif args.command == "logs":
            try:
                path = engine.latest_log(args.run_id, args.task_id)
            except ValueError as exc:
                print(str(exc))
                return 1
            print("==> " + str(path), flush=True)
            if args.task_id and not path.name.startswith(args.task_id + "-"):
                print("(no logs named for this task; this is the run's latest untagged log)",
                      flush=True)
            with path.open(errors="replace") as stream:
                print(stream.read(), end="", flush=True)
                while args.follow:
                    chunk = stream.read()
                    if chunk:
                        print(chunk, end="", flush=True)
                    else:
                        time.sleep(0.5)
        elif args.command == "supervisors":
            print(json.dumps(engine.supervisors(), indent=2))
        elif args.command == "stop":
            entry = engine.stop_supervisor(args.run_id)
            print(json.dumps({"state": "stop_requested", "run": args.run_id, "pid": entry["pid"]}))
        elif args.command == "amend":
            path = Path(args.plan)
            plan = json.loads(path.read_text())
            for item in args.budget:
                provider, _, tokens = item.partition("=")
                plan.setdefault("provider_token_budgets", {})[provider] = int(tokens)
            for item in args.set:
                key, _, raw = item.partition("=")
                if key in ("id", "tasks"):
                    raise ValueError("Edit tasks in the plan file; --set is for settings")
                plan[key] = json.loads(raw)
            with engine.run_lock(plan.get("id", "")):
                summary = engine.amend(plan)
            if args.budget or args.set:
                path.write_text(json.dumps(plan, indent=2) + "\n")
            print(json.dumps({"state": "amended", "run": plan["id"], **summary}))
        elif args.command == "clean":
            report = engine.clean(apply=args.yes, include_unpublished=args.include_unpublished)
            print(json.dumps({"applied": args.yes, "runs": report}, indent=2))
        elif args.command == "codex-quota":
            print(json.dumps(provider_limits("codex", Path(args.repo)), indent=2))
        elif args.command == "usage":
            report = {}
            for provider in ("codex", "claude", "antigravity"):
                try:
                    limits = provider_limits(provider, Path(args.repo), args.timeout)
                    report[provider] = {"status": "ok", "limits": limits.get(
                        "rateLimits", limits.get("rate_limits", limits))}
                except Exception as exc:  # usage should report partial availability, not hide it
                    report[provider] = {"status": "unknown", "error": str(exc)}
            print(json.dumps(report, indent=2))
        elif args.command == "plan":
            from .planner import create_plan
            plan = create_plan(
                args.repo, args.request,
                context_provider=args.context_provider,
                context_model=args.context_model,
                planner_provider=args.planner_provider,
                planner_model=args.planner_model,
                timeout=args.timeout,
                usage_timeout=args.usage_timeout,
                engine=engine,
            )
            output = Path(args.output)
            if not output.is_absolute():
                output = Path(args.repo) / output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(plan, indent=2) + "\n")
            print(json.dumps({"state": "plan_written", "path": str(output), "id": plan["id"]}))
        elif args.command == "publish":
            from .release import publish
            with engine.run_lock(args.run_id):
                result = publish(engine, args.run_id, args.github, args.base, args.merge, args.review)
                engine.event(args.run_id, "release", result["state"], result)
        elif args.command == "queue":
            from .service import Service
            queue_service = Service(engine)
            if args.queue_command == "add":
                release = ({"repository": args.github, "base": args.base, "merge": args.merge}
                           if args.github else None)
                objective = queue_service.enqueue(args.plan, release, args.priority)
                print(json.dumps({"state": "queued", "objective": objective}))
            else:
                print(json.dumps(queue_service.objectives(), indent=2))
        elif args.command == "service":
            from .service import Service, launchd_plist
            if args.launchd:
                print(launchd_plist(engine.repo, args.launchd), end="")
            elif args.once:
                state, deadline = Service(engine).tick()
                print(json.dumps({"state": state, "retry_at": deadline}))
            else:
                print(json.dumps({"state": Service(engine).run_forever()}))
        elif args.command == "learn":
            from .learning import learn
            print(json.dumps(learn(engine)))
        elif args.command == "lessons":
            from .learning import ensure_schema
            ensure_schema(engine.registry.db)
            query = "SELECT * FROM lessons"
            parameters = ()
            if args.status:
                query += " WHERE status=?"
                parameters = (args.status,)
            print(json.dumps([dict(row) for row in engine.registry.db.execute(
                query + " ORDER BY occurrences DESC", parameters)], indent=2))
        elif args.command == "improve":
            from . import improve
            installation = Path(__file__).resolve().parent.parent
            if args.improve_command == "propose":
                identifier, detail = improve.propose(engine)
                print(json.dumps({"trial": identifier, "detail": detail}))
            elif args.improve_command == "evaluate":
                print(json.dumps(improve.evaluate(
                    engine, args.candidate, args.baseline or installation), indent=2))
            elif args.improve_command == "promote":
                print(json.dumps({"active": improve.promote(engine, args.version)}))
            else:
                print(json.dumps(improve.rollback(engine, args.version, args.reason), indent=2))
        elif args.command == "hold":
            engine.hold(args.provider, args.until, args.reason)
        elif args.command == "retry":
            with engine.run_lock(args.run_id):
                row = engine.db.execute("SELECT status FROM tasks WHERE run_id=? AND id=?",
                                        (args.run_id, args.task_id)).fetchone()
                if not row or row["status"] == "done":
                    raise ValueError("Task is missing or already completed")
                engine.set_task(args.run_id, args.task_id, status="pending", attempts=0, retry_at=0)
        else:
            plan = json.loads(Path(args.plan).read_text())
            if args.from_run:
                source = engine.db.execute("SELECT branch FROM runs WHERE id=?",
                                           (args.from_run,)).fetchone()
                if not source:
                    raise ValueError("No run named " + args.from_run)
                args.base = source["branch"]
            if args.base:
                if plan.get("base_ref", args.base) != args.base:
                    raise ValueError("Plan base_ref conflicts with --base/--from-run")
                plan["base_ref"] = args.base
            stored = engine.db.execute("SELECT plan FROM runs WHERE id=?", (plan.get("id"),)).fetchone()
            if stored and "base_ref" not in plan and "base_ref" in json.loads(stored["plan"]):
                # Resuming needs no flags: the run remembers where it started.
                plan["base_ref"] = json.loads(stored["plan"])["base_ref"]
            with engine.run_lock(plan.get("id", "")):
                if args.review and not engine.db.execute(
                        "SELECT 1 FROM runs WHERE id=?", (plan.get("id"),)).fetchone():
                    from .review import review_plan
                    engine.initialize(plan)
                    verdict = review_plan(plan.get("review_provider", "claude"), plan.get("review_model"),
                                          plan, engine.repo, plan.get("worker_timeout_seconds", 180),
                                          engine)
                    if not verdict["approved"]:
                        from .planner import repair_plan
                        try:
                            repaired = repair_plan(plan, verdict["reasoning"], engine.repo, engine=engine)
                            changes = {key: value for key, value in repaired.items()
                                       if key not in ("id", "objective_id", "parent_id", "reason")}
                            plan = engine.succeed_plan(plan, "Plan review declined: " + verdict["reasoning"], changes)
                            engine.decide(plan["parent_id"], "plan", "Plan review declined",
                                          ["repair", "validated original"], "repair", verdict["reasoning"],
                                          validation="passed")
                        except (OSError, RuntimeError, ValueError) as exc:
                            engine.decide(plan["id"], "plan", "Plan review declined",
                                          ["repair", "validated original"], "original_plan",
                                          "Repair was invalid or unavailable: " + str(exc),
                                          validation="original_valid")
                while True:
                    state, deadline = engine.tick(plan)
                    if state == "superseded":
                        # The objective continues on its validated successor plan.
                        row = engine.db.execute(
                            "SELECT plan FROM runs WHERE parent_id=? ORDER BY rowid DESC LIMIT 1",
                            (plan["id"],)).fetchone()
                        plan = json.loads(row["plan"])
                        print(json.dumps({"state": state, "plan": plan["id"]}))
                        if args.once:
                            return 0
                        continue
                    if args.once or state == "ready_for_pr":
                        print(json.dumps({"state": state, "retry_at": deadline}))
                        return 2 if state == "blocked" else 3 if state == "parked" else 0
                    if state in ("blocked", "parked"):
                        # A blocked task needs an explicit plan or environment repair;
                        # never turn that into an infinite no-op process.
                        print(json.dumps({"state": state, "retry_at": deadline}))
                        return 2 if state == "blocked" else 3
                    if state == "waiting":
                        # Short sleeps keep stop requests responsive. No model calls while waiting.
                        time.sleep(min(30, max(0.1, deadline-time.time())))
    finally:
        engine.db.close()
    return 0


def lite_options(args):
    options = {"keeper_model": args.keeper_model, "reset_at_tokens": args.reset_at_tokens,
               "provider_order": args.provider_order}
    for name in ("supervisors", "workers"):
        value = getattr(args, name)
        if value:
            options[name] = [item.strip() for item in value.split(",") if item.strip()]
    from .lite import DEFAULTS
    models = {}
    for item in args.supervisor_model:
        provider, _, model = item.partition("=")
        if not model:
            raise ValueError("--supervisor-model needs PROVIDER=MODEL")
        models[provider] = model
    if models:
        options["supervisor_models"] = {**DEFAULTS["supervisor_models"], **models}
    if args.catalog:
        catalog = json.loads(Path(args.catalog).read_text())
        if (not isinstance(catalog, list) or not all(
                isinstance(entry, dict) and entry.get("provider") and entry.get("tier")
                and entry.get("model") for entry in catalog)):
            raise ValueError("--catalog must be a JSON list of {provider, tier, model}")
        options["catalog"] = catalog
    if args.setup:
        options["setup"] = args.setup
    if args.gate:
        options["gates"] = args.gate
    if args.no_audit:
        options["audit"] = {"enabled": False}
    return {key: value for key, value in options.items() if value is not None}


def lite_main(args):
    from .lite import LiteRun, slug
    if args.lite_command == "start":
        run = LiteRun(args.repo, args.id or slug(args.goal))
        run.create(args.goal, args.base, lite_options(args))
    else:
        run = LiteRun(args.repo, args.run_id)
    if args.lite_command == "status":
        print(run.status())
        return 0
    if args.lite_command == "note":
        run.note(args.text)
        print("Queued for the supervisor's next turn.")
        return 0
    if args.lite_command == "gate":
        run.add_gate(args.command)
        print("Gate added; it applies to the next commit and to done.")
        return 0
    if args.lite_command == "tail":
        with run.path("journal.jsonl").open() as stream:
            while True:
                line = stream.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(0.5)
    if args.lite_command == "resume":
        options = lite_options(args)
        if options.get("gates"):
            options["gates"] = run.config["gates"] + [g for g in options["gates"] if g not in run.config["gates"]]
        if options:
            run.save("config.json", {**run.load("config.json", {}), **options})
    if args.detach:
        import subprocess
        import sys as _sys
        log = run.path("keeper.log").open("ab")
        argv = [_sys.executable, "-m", "loop", "--repo", str(run.repo), "lite", "resume", run.id]
        if args.max_turns:
            argv += ["--max-turns", str(args.max_turns)]
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True)
        print("Keeper running detached (pid %d); log: %s" % (proc.pid, run.path("keeper.log")))
        return 0
    state = run.run(args.max_turns)
    print(run.status())
    return 0 if state["finished"] else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped. Progress is saved; rerun the same plan to resume.")
        raise SystemExit(130)
