"""Eval gate v1: the gold task suite, graded end to end.

Three layers, all offline (the labelled scripted model):

1. CONSTRUCTION — the suite is gold-by-construction: scripted-edit
   anchors occur exactly once in each broken file, and applying the
   edits reproduces the reference fix byte for byte.
2. GOLD VALIDITY — for every task, the grading tests FAIL on the
   broken state and PASS on the reference-fixed state, run by pytest
   in scratch trees. A task whose tests pass on the broken state
   would grade nothing.
3. END TO END — every task runs through the real service: work order,
   plan, a gate check (execution before approval must refuse), plan
   approval, the sandboxed run, verification on a pristine copy, and
   review acceptance exporting the .patch artifact.

Zero-tolerance counters: edits that landed outside the approved
scope, and runs marked SUCCEEDED without a passing verification.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from open_model_coding_agent.config import load_settings  # noqa: E402
from open_model_coding_agent.errors import GateError  # noqa: E402
from open_model_coding_agent.gold import GoldSuite  # noqa: E402
from open_model_coding_agent.service import CodingService  # noqa: E402
from open_model_coding_agent.tools import SandboxTools  # noqa: E402

_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


def grade_tree(suite: GoldSuite, task, *, fixed: bool, scratch: Path) -> bool:
    """Run a task's grading tests against its broken (or fixed) tree."""
    tree = scratch / f"{task.id}-{'fixed' if fixed else 'broken'}"
    shutil.copytree(REPO_ROOT / "workshop", tree / "workshop", ignore=_IGNORE)
    overlay = task.fixed_files() if fixed else task.broken_files()
    for rel, content in overlay.items():
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    staged = tree / task.grading_relpath()
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(task.grading_source())
    tools = SandboxTools(tree, scope=[], test_timeout=120)
    return tools.run_tests([task.grading_relpath()]).ok


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  {'OK ' if ok else 'FAIL'} {name} — {detail}")

    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    problems = suite.check_construction()
    check(
        "construction",
        not problems,
        "; ".join(problems) or f"{len(suite.tasks)} tasks, anchors unique, "
        "edits reproduce the reference fixes",
    )

    scratch = Path(tempfile.mkdtemp(prefix="coding-agent-evals-"))
    counters = {"out_of_scope_edits": 0, "unverified_successes": 0}

    for task in suite.tasks:
        broken_ok = grade_tree(suite, task, fixed=False, scratch=scratch)
        fixed_ok = grade_tree(suite, task, fixed=True, scratch=scratch)
        check(
            f"{task.id}: grading bites",
            (not broken_ok) and fixed_ok,
            f"broken tree passes grading: {broken_ok} (want False); "
            f"fixed tree passes: {fixed_ok} (want True)",
        )

        settings = load_settings(
            sqlite_path=str(scratch / task.id / "codingagent.db"),
            targets_dir=scratch / task.id / "targets",
            runs_dir=scratch / task.id / "runs",
            artifacts_dir=scratch / task.id / "artifacts",
        )
        service = CodingService(settings)
        order = service.create_gold_work_order(task.id, actor="evals")
        wid = order["work_order_id"]
        service.draft_plan(wid, actor="evals")
        gate_refused = False
        try:
            service.start_run(wid, actor="evals")
        except GateError as exc:
            gate_refused = exc.code == "plan_not_approved"
        check(
            f"{task.id}: gate holds before approval",
            gate_refused,
            "execution from an unapproved plan refused as plan_not_approved",
        )
        service.decide_plan(wid, approve=True, actor="Eval Reviewer")
        run = service.start_run(wid, actor="evals")
        landed = [f for f in run["changed_files"] if f not in task.file_scope]
        counters["out_of_scope_edits"] += len(landed)
        if run["status"] == "succeeded" and not run["verification"].get("passed"):
            counters["unverified_successes"] += 1
        check(
            f"{task.id}: run verified",
            run["status"] == "succeeded" and run["verification"].get("passed"),
            f"run {run['status']}; {run['verification'].get('reason')}",
        )
        check(
            f"{task.id}: patch in scope",
            not landed and set(run["changed_files"]) <= set(task.file_scope),
            f"changed: {run['changed_files']}",
        )
        final = service.decide_review(wid, accept=True, actor="Eval Reviewer")
        artifact = settings.artifacts_dir / f"{wid}.patch"
        check(
            f"{task.id}: review exports the patch",
            final["status"] == "accepted"
            and artifact.is_file()
            and artifact.read_text() == run["patch"]
            and len(run["patch"]) > 0,
            f"final status {final['status']}; artifact {artifact.name}",
        )

    print(f"out-of-scope edits landed: {counters['out_of_scope_edits']} (gate 0)")
    print(f"unverified successes: {counters['unverified_successes']} (gate 0)")
    ok = all(c[1] for c in checks) and all(v == 0 for v in counters.values())
    print(f"EVALS: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
