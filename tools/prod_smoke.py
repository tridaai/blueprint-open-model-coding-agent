#!/usr/bin/env python3
"""Prod smoke: one gold task, end to end, against a configured
deployment.

This is the post-deploy check for blueprint #14 (round 20). Where
``tools/live_smoke.py`` proves the live adapter against Baseten's
endpoint, this tool proves *a deployment*: it builds the service
from the environment exactly as the app does (``load_settings`` —
``DATABASE_URL``, ``DEPLOYMENT_PROFILE``, ``MODEL_PROFILE``, the
provider key), prints the deployment's readiness as the shell
itself reports it, then runs ONE gold task (default
``reserve-overcommit``) through every gate the deployment runs:
work order, plan draft, plan approval, sandboxed run,
verification in code, and a review acceptance — and prints what
the shell recorded.

The bar is the deployment serving, not the paperwork completing:

- Readiness problems, or a named refusal anywhere on the run
  path, print by name and exit 2 — the deployment did not serve.
- A run that lands but does not verify exits 1.
- A verified run exits 0. The review outcome is reported as it
  stands: accepted (the patch exported), parked awaiting a
  second decision (a dual-control deployment — the second
  decision is a person's, and the smoke does not forge it), or
  blocked by unanswered must-answer challenges (the challenge
  pass doing its job). All three are governance working.

The smoke leaves its work order on the deployment's store, under
the actor name ``prod-smoke``: a deployment check that leaves no
auditable trace proves nothing about the store it ran against.

Run it from the repo root, against the deployment's environment:

    uv run python tools/prod_smoke.py
    uv run python tools/prod_smoke.py --task low-stock-report

Against the local default (no environment) it runs the offline
profile on SQLite — the same check, at developer scale.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from open_model_coding_agent.config import load_settings
    from open_model_coding_agent.errors import ConfigError, GateError
    from open_model_coding_agent.service import CodingService
except ImportError:  # running from a bare checkout, package not installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from open_model_coding_agent.config import load_settings
    from open_model_coding_agent.errors import ConfigError, GateError
    from open_model_coding_agent.service import CodingService

DEFAULT_TASK = "reserve-overcommit"
ACTOR = "prod-smoke"


def run_prod_smoke(
    task_id: str = DEFAULT_TASK,
    *,
    settings=None,
    service=None,
    actor: str = ACTOR,
) -> dict:
    """Run one gold task end to end on the configured deployment
    and return the recorded evidence as a dict. Refusals on the
    run path propagate as the shell's own named errors (the CLI
    wrapper prints them, exit 2); the review outcome never
    raises — it is reported, as it stands, in the report."""
    if service is None:
        settings = settings or load_settings()
        service = CodingService(settings)
    readiness = service.readiness()
    profile = service.serving_profile()

    report: dict = {
        "deployment_profile": readiness["deployment_profile"],
        "deployment_profile_version": readiness["deployment_profile_version"],
        "store": readiness["store"],
        "migrations_current": bool(
            readiness["migrations"] and readiness["migrations"]["current"]
        ),
        "readiness_status": readiness["status"],
        "readiness_problems": readiness["problems"],
        "price_table_version": readiness["price_table_version"],
        "profile_id": profile.profile_id,
        "model": profile.model,
        "task_id": task_id,
    }
    if readiness["problems"]:
        report["stage"] = "readiness"
        return report

    order = service.create_gold_work_order(task_id, actor=actor)
    work_order_id = order["work_order_id"]
    plan = service.draft_plan(work_order_id, actor=actor)
    service.decide_plan(work_order_id, approve=True, actor=actor)
    run = service.start_run(work_order_id, actor=actor)

    detail = service.work_order_detail(work_order_id)
    run_read = next(
        (r for r in detail["runs"] if r["run_id"] == run["run_id"]), run
    )
    report.update(
        {
            "stage": "run",
            "work_order_id": work_order_id,
            "plan": {
                "version": plan.get("version"),
                "model_profile": plan.get("model_profile"),
                "files": plan.get("files"),
            },
            "run_status": run_read["status"],
            "deployment_stamp": run_read.get("deployment_profile"),
            "verification": run_read.get("verification") or {},
            "changed_files": run_read.get("changed_files") or [],
            "usage": detail["usage"],
            "cost": run_read.get("cost") or {},
        }
    )
    if run_read["status"] != "succeeded":
        report["review"] = "not reached — the run did not succeed"
        report["final_status"] = detail["work_order"]["status"]
        return report

    # The review gate, as the deployment's own rules run it. The
    # outcome is reported, never forced: acceptance may complete,
    # park for a second decision under dual control, or refuse
    # on unanswered must-answer challenges — each is the shell
    # governing, and the smoke's bar (the deployment served and
    # verified) is already met.
    try:
        decided = service.decide_review(
            work_order_id, decision="accept", actor=actor
        )
        report["review"] = (
            "accepted"
            if decided["status"] == "accepted"
            else f"parked: {decided['status']}"
        )
    except GateError as exc:
        report["review"] = f"blocked: {exc.code}: {exc.message}"
    report["final_status"] = service.get_work_order(work_order_id)["status"]
    return report


def _print_report(report: dict) -> None:
    print(
        f"Deployment:     {report['deployment_profile']} "
        f"v{report['deployment_profile_version']} · store "
        f"{report['store']} · migrations "
        f"{'current' if report['migrations_current'] else 'BEHIND'} · "
        f"price table v{report['price_table_version']}"
    )
    print(f"Readiness:      {report['readiness_status']}")
    for problem in report["readiness_problems"]:
        print(f"  problem: {problem}")
    print(f"Profile:        {report['profile_id']} ({report['model']})")
    print(f"Gold task:      {report['task_id']}")
    if report["stage"] == "readiness":
        print("Smoke stopped at readiness: the deployment did not serve.")
        return
    plan = report["plan"]
    verification = report["verification"]
    cost = report["cost"]
    print(f"Work order:     {report['work_order_id']}")
    print(
        f"Plan:           v{plan['version']} drafted by "
        f"{plan['model_profile']}; approved"
    )
    print(f"Planned files:  {', '.join(plan['files'] or [])}")
    print(
        f"Run:            {report['run_status']} "
        f"(deployment stamp: {report['deployment_stamp']})"
    )
    print(
        f"Verification:   passed={verification.get('passed')} — "
        f"{verification.get('reason')}"
    )
    print(f"Changed:        {', '.join(report['changed_files']) or '(nothing)'}")
    if cost:
        print(
            f"Priced cost:    ${cost['total']} total "
            f"({cost['priced_tokens']} priced tokens under price "
            f"table version(s) {cost['price_table_versions']})"
        )
    print(f"Review:         {report.get('review')}")
    print(f"Work order:     {report['final_status']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one gold task end to end against the "
        "configured deployment and print the recorded evidence."
    )
    parser.add_argument("--task", default=DEFAULT_TASK)
    args = parser.parse_args(argv)
    try:
        report = run_prod_smoke(task_id=args.task)
    except ConfigError as exc:
        # A deployment that refuses its own configuration is a
        # named refusal, not a crash.
        print(f"prod smoke refused: {exc}")
        return 2
    except GateError as exc:
        print(f"prod smoke refused: {exc.code}: {exc.message}")
        return 2
    _print_report(report)
    if report["stage"] == "readiness":
        return 2
    return 0 if report["run_status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
