#!/usr/bin/env python3
"""Live smoke: one gold task, end to end, against the real endpoint.

This is the deployment live check for blueprint #14. It runs ONE
gold task (default ``reserve-overcommit``) through the real service
with a chosen live profile (default ``baseten-deepseek``) — work
order, plan draft, plan approval, sandboxed run, verification in
code — and prints what the shell recorded: the plan decision, the
run's status and verification verdict, every usage row, and the
priced cost from the governed cost fold.

It constructs the service exactly the way the app does: settings
from the environment (``load_settings``), the transport from the
settings. A live profile without its key in the environment is
refused there, by name — this tool prints that refusal and exits
non-zero; it never prints a traceback for a refusal the shell
already names, and it never falls back to the offline model.

Run it from the repo root, with the key in the environment:

    BASETEN_API_KEY=... uv run python tools/live_smoke.py
    BASETEN_API_KEY=... uv run python tools/live_smoke.py \
        --profile baseten-glm --task low-stock-report

Programmatic callers (tests, a deployment harness) may pass their
own settings and inject a transport:

    from tools.live_smoke import run_smoke
    report = run_smoke(settings=my_settings, transport=fake)

The injected transport is the only seam: everything else — the
gates, the sandbox, the verification, the cost fold — is the
production path, unchanged.
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

DEFAULT_PROFILE = "baseten-deepseek"
DEFAULT_TASK = "reserve-overcommit"
ACTOR = "live-smoke"


def run_smoke(
    profile_id: str = DEFAULT_PROFILE,
    task_id: str = DEFAULT_TASK,
    *,
    settings=None,
    transport=None,
    actor: str = ACTOR,
) -> dict:
    """Run one gold task end to end on a live profile and return
    the recorded evidence as a dict. Refusals propagate as the
    shell's own named errors (ConfigError for a profile that cannot
    serve in this environment, GateError for a gate refusal); the
    CLI wrapper below turns them into a printed refusal."""
    if settings is None:
        settings = load_settings(model_profile=profile_id)
    if transport is not None:
        # The settings' transport seam — the same one the service
        # reads when it builds a model. Injection changes the wire,
        # never the governance.
        settings.model_transport = transport
    service = CodingService(settings)
    profile = service.serving_profile()

    order = service.create_gold_work_order(task_id, actor=actor)
    work_order_id = order["work_order_id"]
    plan = service.draft_plan(work_order_id, actor=actor)
    service.decide_plan(work_order_id, approve=True, actor=actor)
    run = service.start_run(work_order_id, actor=actor)

    detail = service.work_order_detail(work_order_id)
    run_read = next(
        (r for r in detail["runs"] if r["run_id"] == run["run_id"]), run
    )
    return {
        "profile_id": profile.profile_id,
        "model": profile.model,
        "task_id": task_id,
        "work_order_id": work_order_id,
        "plan": {
            "version": plan.get("version"),
            "model_profile": plan.get("model_profile"),
            "files": plan.get("files"),
            "decision": "approved",
            "decided_by": actor,
        },
        "run_status": run_read["status"],
        "verification": run_read.get("verification") or {},
        "changed_files": run_read.get("changed_files") or [],
        "usage": detail["usage"],
        "cost": run_read.get("cost") or {},
        "final_status": detail["work_order"]["status"],
    }


def _print_report(report: dict) -> None:
    plan = report["plan"]
    verification = report["verification"]
    cost = report["cost"]
    print(f"Profile:        {report['profile_id']} ({report['model']})")
    print(f"Gold task:      {report['task_id']}")
    print(f"Work order:     {report['work_order_id']}")
    print(
        f"Plan:           v{plan['version']} drafted by "
        f"{plan['model_profile']}; {plan['decision']} by {plan['decided_by']}"
    )
    print(f"Planned files:  {', '.join(plan['files'] or [])}")
    print(f"Run:            {report['run_status']}")
    print(
        f"Verification:   passed={verification.get('passed')} — "
        f"{verification.get('reason')}"
    )
    print(f"Changed:        {', '.join(report['changed_files']) or '(nothing)'}")
    print("Usage (recorded per call):")
    for row in report["usage"]:
        phase = "run" if row.get("run_id") else "plan"
        print(
            f"  [{phase}] {row['model']} — {row['prompt_tokens']} in / "
            f"{row['completion_tokens']} out ({row['source']}/{row['unit']}), "
            f"{row['latency_ms']} ms"
        )
    if cost:
        print(
            f"Priced cost:    ${cost['total']} total "
            f"(plan ${cost['plan']} + execution ${cost['execution']}), "
            f"{cost['priced_tokens']} priced tokens under price table "
            f"version(s) {cost['price_table_versions']}"
        )
        if cost["unpriced_tokens"]:
            print(
                f"Unpriced:       {cost['unpriced_tokens']} tokens "
                f"({', '.join(cost['unpriced_reasons'])}) — counted, "
                f"never zeroed"
            )
    print(f"Work order:     {report['final_status']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one gold task end to end on a live Baseten "
        "profile and print the recorded evidence."
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--task", default=DEFAULT_TASK)
    args = parser.parse_args(argv)
    try:
        report = run_smoke(profile_id=args.profile, task_id=args.task)
    except ConfigError as exc:
        # A profile that cannot serve here (no key, not in the
        # provider's catalog) is a named refusal, not a crash.
        print(f"live smoke refused: {exc}")
        return 2
    except GateError as exc:
        print(f"live smoke refused: {exc.code}: {exc.message}")
        return 2
    _print_report(report)
    return 0 if report["run_status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
