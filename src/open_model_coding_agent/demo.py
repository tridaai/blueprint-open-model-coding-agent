"""Demo: one gold task, end to end, offline.

Creates a work order from the gold suite's first task, drafts the
plan, approves it as a demo reviewer, runs the agent in its sandbox,
lets the shell verify the patch on a pristine copy, and accepts the
verified patch — exporting the .patch artifact. Every step is the
real service path; nothing is staged for show.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .config import load_settings
from .service import CodingService


def run_demo(settings=None) -> dict:
    scratch = Path(tempfile.mkdtemp(prefix="coding-agent-demo-"))
    settings = settings or load_settings(
        sqlite_path=str(scratch / "codingagent.db"),
        targets_dir=scratch / "targets",
        runs_dir=scratch / "runs",
        artifacts_dir=scratch / "artifacts",
    )
    service = CodingService(settings)
    task = service.gold.tasks[0]
    order = service.create_gold_work_order(task.id, actor="demo")
    plan = service.draft_plan(order["work_order_id"], actor="demo")
    service.decide_plan(
        order["work_order_id"], approve=True, actor="Demo Reviewer",
        note="demo approval",
    )
    run = service.start_run(order["work_order_id"], actor="demo")
    final = service.decide_review(
        order["work_order_id"], accept=True, actor="Demo Reviewer"
    )
    return {
        "task": task.id,
        "work_order_id": order["work_order_id"],
        "plan_files": plan["files"],
        "run_status": run["status"],
        "changed_files": run["changed_files"],
        "verification": run["verification"]["reason"],
        "final_status": final["status"],
        "artifact": str(settings.artifacts_dir / f"{order['work_order_id']}.patch"),
        "scratch": str(scratch),
    }


def main() -> None:
    result = run_demo()
    print(f"Gold task:        {result['task']}")
    print(f"Work order:       {result['work_order_id']}")
    print(f"Planned files:    {', '.join(result['plan_files'])}")
    print(f"Run:              {result['run_status']} — {result['verification']}")
    print(f"Changed:          {', '.join(result['changed_files'])}")
    print(f"Review:           {result['final_status']}")
    print(f"Patch artifact:   {result['artifact']}")


if __name__ == "__main__":
    main()
