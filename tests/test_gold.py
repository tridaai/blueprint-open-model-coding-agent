"""The gold suite: construction, and grading that actually bites."""

from __future__ import annotations

import shutil
from pathlib import Path

from open_model_coding_agent.gold import GoldSuite
from open_model_coding_agent.tools import SandboxTools

REPO_ROOT = Path(__file__).resolve().parents[1]
_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


def test_suite_loads_six_tasks():
    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    assert len(suite.tasks) == 6
    assert {t.id for t in suite.tasks} == {
        "reserve-overcommit",
        "tax-before-discount",
        "low-stock-report",
        "cancel-leaks-reservations",
        "bulk-tier-boundary",
        "order-coupon",
    }


def test_the_coupon_task_spans_two_files():
    """Round 3's multi-file task: its scope, its plan steps, and its
    scripted edits all cover pricing and orders — neither file's fix
    alone can pass the grading tests."""
    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    task = suite.get("order-coupon")
    assert task.file_scope == ["workshop/pricing.py", "workshop/orders.py"]
    assert {s["files"][0] for s in task.plan_steps} == set(task.file_scope)
    assert {e["path"] for e in task.scripted_edits} == set(task.file_scope)


def test_construction_is_sound():
    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    assert suite.check_construction() == []


def _grade(task, *, fixed: bool, scratch: Path) -> bool:
    tree = scratch / ("fixed" if fixed else "broken")
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


def test_grading_fails_on_broken_and_passes_on_fixed(tmp_path):
    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    for index, task_id in enumerate(("reserve-overcommit", "order-coupon")):
        task = suite.get(task_id)
        assert _grade(task, fixed=False, scratch=tmp_path / f"a{index}") is False
        assert _grade(task, fixed=True, scratch=tmp_path / f"b{index}") is True


def test_coupon_grading_bites_on_either_half_fix(tmp_path):
    """Fixing only pricing, or only orders, still fails the coupon
    grading — the task is not done until both files are fixed."""
    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    task = suite.get("order-coupon")
    broken, fixed = task.broken_files(), task.fixed_files()
    for only in ("workshop/pricing.py", "workshop/orders.py"):
        tree = tmp_path / only.replace("/", "-")
        shutil.copytree(REPO_ROOT / "workshop", tree / "workshop", ignore=_IGNORE)
        for rel, content in broken.items():
            (tree / rel).write_text(content)
        (tree / only).write_text(fixed[only])
        staged = tree / task.grading_relpath()
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(task.grading_source())
        tools = SandboxTools(tree, scope=[], test_timeout=120)
        assert tools.run_tests([task.grading_relpath()]).ok is False
