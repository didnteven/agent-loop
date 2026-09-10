import argparse
import json
import signal
import time
from pathlib import Path

from .adapters import codex_limits
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
    retry = sub.add_parser("retry", help="Retry a repaired authentication/configuration failure")
    retry.add_argument("run_id")
    retry.add_argument("task_id")
    args = parser.parse_args()
    engine = Engine(args.repo)
    try:
        if args.command == "status":
            print(json.dumps(engine.status(), indent=2))
        elif args.command == "codex-quota":
            print(json.dumps(codex_limits(Path(args.repo)), indent=2))
        elif args.command == "publish":
            from .release import publish
            with engine.lock():
                result = publish(engine, args.run_id, args.github, args.base, args.merge, args.review)
                engine.event(args.run_id, "release", result["state"], result)
        elif args.command == "hold":
            engine.hold(args.provider, args.until, args.reason)
        elif args.command == "retry":
            with engine.lock():
                row = engine.db.execute("SELECT status FROM tasks WHERE run_id=? AND id=?",
                                        (args.run_id, args.task_id)).fetchone()
                if not row or row["status"] == "done":
                    raise ValueError("Task is missing or already completed")
                engine.set_task(args.run_id, args.task_id, status="pending", attempts=0, retry_at=0)
        else:
            plan = json.loads(Path(args.plan).read_text())
            with engine.lock():
                if args.review and not engine.db.execute(
                        "SELECT 1 FROM runs WHERE id=?", (plan.get("id"),)).fetchone():
                    from .review import review_plan
                    verdict = review_plan(plan.get("review_provider", "claude"), plan.get("review_model"),
                                          plan, engine.repo, plan.get("worker_timeout_seconds", 180))
                    if not verdict["approved"]:
                        print(json.dumps({"state": "review_declined", "reasoning": verdict["reasoning"]}))
                        return 2
                while True:
                    state, deadline = engine.tick(plan)
                    if args.once or state == "ready_for_pr":
                        print(json.dumps({"state": state, "retry_at": deadline}))
                        return 2 if state == "blocked" else 0
                    if state == "blocked":
                        # Authentication/configuration failures wait for operator repair,
                        # without burning tokens. Stop and use `retry`, then resume.
                        time.sleep(30)
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
