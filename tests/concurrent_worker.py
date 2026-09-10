"""Drive one milestone to completion in its own process, with a stubbed provider.

Used by the concurrency test: separate processes are what exercise the git-level
races that two Engine objects inside one interpreter would not.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loop.adapters import Result
from loop.engine import Engine


def main():
    repo, plan = sys.argv[1], json.loads(sys.argv[2])
    # Wait for the caller's start signal so both processes contend for real.
    start = Path(repo) / ".agent-loop" / "go"
    for _ in range(600):
        if start.exists():
            break
        time.sleep(0.01)
    path = plan["tasks"][0]["files"][0]
    engine = Engine(repo)

    def worker(*_):
        return Result("ok", json.dumps({"files": [{"path": path, "content": "def add(a,b): return a+b\n"}]}),
                      {"input_tokens": 50, "output_tokens": 10})

    try:
        with engine.run_lock(plan["id"]):
            for _ in range(10):
                state, _deadline = engine.tick(plan, worker)
                print(state, flush=True)
                if state in ("ready_for_pr", "blocked"):
                    return 0 if state == "ready_for_pr" else 1
        return 1
    finally:
        engine.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
