"""The gold suite: construction, and grading that actually bites."""

from __future__ import annotations

import shutil
from pathlib import Path

from open_model_coding_agent.gold import GoldSuite
from open_model_coding_agent.tools import SandboxTools

REPO_ROOT = Path(__file__).resolve().parents[1]
_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


def test_suite_loads_five_tasks():
    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    assert len(suite.tasks) == 5
    assert {t.id for t in suite.tasks} == {
        "reserve-overcommit",
        "tax-before-discount",
        "low-stock-report",
        "cancel-leaks-reservations",
        "bulk-tier-boundary",
    }


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
    task = suite.get("reserve-overcommit")
    assert _grade(task, fixed=False, scratch=tmp_path / "a") is False
    assert _grade(task, fixed=True, scratch=tmp_path / "b") is True
