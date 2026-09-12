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
    sub.add_parser("status")
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
    args = parser.parse_args()
    engine = Engine(args.repo)
    try:
        if args.command == "status":
            print(json.dumps(engine.status(), indent=2))
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


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped. Progress is saved; rerun the same plan to resume.")
        raise SystemExit(130)
