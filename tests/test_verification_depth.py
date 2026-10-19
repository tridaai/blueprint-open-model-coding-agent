"""Round 10 — verification depth: test-gaming defences.

Three defences beyond "the grading tests pass", each proved where
it bites:

- **Test-editing defence.** A patch touching the test/grading
  surface is flagged ``test_surface_touched`` with the files named.
  On a gold task it fails verification by name; on an ad-hoc order
  it is recorded and the run may still succeed — the review gate
  surfaces the flag in red. The gold cases are driven straight at
  ``_verify`` with crafted plans and patches, because on any plan
  the validator accepted the tool port already refuses such edits
  (the plan's scope is the code files): this is defence in depth,
  proved at the layer that owns the verdict.
- **Hidden-test re-grade.** A gold run that passes the visible set
  is re-graded against the task's hidden set, staged only then.
  The bite case is an *overfit* fix: it special-cases the visible
  tests' SKU, passes every visible test, and fails the hidden set
  — the verdict flips to FAILED as ``hidden_grading_failed`` and
  the record keeps the two verdicts distinct.
- **Scope-creep detection.** The landed patch is compared against
  the approved plan's *steps*: a changed file no step named is an
  unplanned file, failing a gold verification by name.

And patch hygiene: the shape facts land on the record, and a
whitespace-only patch fails as ``no_substantive_change`` — a patch
exists, but no behaviour in it changed. Hidden-set construction
proofs (fail on broken, pass on fixed, for all six tasks) live in
the eval runner, where the subprocess grading already happens,
rather than multiplying full grading runs here.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import open_model_coding_agent.service as service_module
from open_model_coding_agent.gold import GoldSuite
from open_model_coding_agent.model_port import OfflineScriptedModel, Step
from open_model_coding_agent.models import WorkOrderIn
from open_model_coding_agent.patch import build_patch, patch_shape
from open_model_coding_agent import verification as verification_module
from open_model_coding_agent.verification import (
    is_test_surface_path,
    planned_files,
    unplanned_files,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CALC_BROKEN = "def add(a, b):\n    return a - b\n"


# -- the pure checks ----------------------------------------------------


def test_test_surface_rules_by_repo_layout():
    surface = [
        "tests/test_calc.py",
        "pkg/tests/helper.py",
        "grading/test_grading_calc.py",
        "hidden_grading/test_hidden_calc.py",
        "workshop/tests/test_inventory.py",
        "pkg/calc_test.py",
        "conftest.py",
        "pkg/conftest.py",
        "sitecustomize.py",
        "usercustomize.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
    ]
    for rel in surface:
        assert is_test_surface_path(rel), rel
    not_surface = [
        "workshop/pricing.py",
        "workshop/inventory.py",
        "pkg/calc.py",
        "workshop/__init__.py",
        "README.md",
    ]
    for rel in not_surface:
        assert not is_test_surface_path(rel), rel
    assert verification_module.test_surface_files(
        ["pkg/calc.py", "tests/test_calc.py"]
    ) == ["tests/test_calc.py"]


def test_unplanned_files_compare_against_plan_steps_not_scope():
    plan = {
        "files": ["pkg/calc.py", "pkg/other.py"],
        "steps": [
            {"intent": "fix calc", "files": ["pkg/calc.py"], "tests": []},
            {"intent": "fix calc more", "files": ["pkg/calc.py"], "tests": []},
        ],
    }
    assert planned_files(plan) == ["pkg/calc.py"]
    # pkg/other.py is inside the plan's stored file list — the work
    # order's wider scope — but no step named it: unplanned.
    assert unplanned_files(plan, ["pkg/calc.py", "pkg/other.py"]) == [
        "pkg/other.py"
    ]
    assert unplanned_files(plan, ["pkg/calc.py"]) == []
    # A plan record with no steps key at all states its plan as its
    # file list; a present-but-empty step list names nothing.
    legacy = {"files": ["pkg/calc.py"]}
    assert planned_files(legacy) == ["pkg/calc.py"]
    assert unplanned_files(legacy, ["pkg/calc.py"]) == []
    assert planned_files({"files": ["pkg/calc.py"], "steps": []}) == []


def test_patch_shape_counts_lines_and_flags_binary(tmp_path):
    pristine = tmp_path / "pristine"
    workspace = tmp_path / "workspace"
    (pristine / "pkg").mkdir(parents=True)
    (workspace / "pkg").mkdir(parents=True)
    (pristine / "pkg" / "m.py").write_text("one\ntwo\nthree\n")
    (workspace / "pkg" / "m.py").write_text("one\nTWO\nthree\nfour\n")
    patch, changed = build_patch(pristine, workspace)
    shape = patch_shape(pristine, patch, changed)
    assert shape["files_changed"] == 1
    assert shape["per_file"] == [
        {"file": "pkg/m.py", "lines_added": 2, "lines_removed": 1, "binary": False}
    ]
    assert shape["lines_added"] == 2 and shape["lines_removed"] == 1
    assert shape["empty"] is False and shape["whitespace_only"] is False
    assert shape["any_binary"] is False

    (pristine / "bin.dat").write_bytes(b"\x00\x01\x02")
    binary_shape = patch_shape(pristine, "", ["bin.dat"])
    assert binary_shape["any_binary"] is True
    assert binary_shape["binary_files"] == ["bin.dat"]


def test_patch_shape_names_an_empty_patch(tmp_path):
    (tmp_path / "m.py").write_text("x\n")
    shape = patch_shape(tmp_path, "", [])
    assert shape["empty"] is True
    assert shape["whitespace_only"] is False
    assert shape["files_changed"] == 0


def test_every_gold_task_carries_a_hidden_set():
    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    for task in suite.tasks:
        assert task.hidden_grading_file.startswith("test_hidden_")
        assert task.hidden_grading_relpath().startswith("hidden_grading/")
        assert task.hidden_grading_source() != task.grading_source()
    assert suite.check_construction() == []


# -- helpers for the _verify-level cases ---------------------------------


def _patched_copy(pristine: Path, dest: Path, edits: dict[str, tuple[str, str]]):
    """Copy ``pristine`` to ``dest``, apply (old, new) text edits,
    and return the (patch, changed) build_patch sees."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(pristine, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for rel, (old, new) in edits.items():
        target = dest / rel
        text = target.read_text()
        assert text.count(old) == 1, rel
        target.write_text(text.replace(old, new))
    return build_patch(pristine, dest)


def _gold_pristine(service, order) -> Path:
    return Path(order["target_path"])


# -- test-editing defence, gold (defence in depth at _verify) ------------


def test_gold_patch_touching_the_test_surface_fails_by_name(service, gold_order):
    pristine = _gold_pristine(service, gold_order)
    task = service.gold.get("reserve-overcommit")
    fix = task.scripted_edits[0]
    patch, changed = _patched_copy(
        pristine,
        pristine.parent / "crafted-surface",
        {
            "workshop/inventory.py": (fix["old"], fix["new"]),
            "workshop/tests/test_inventory.py": (
                "def test_add_and_read_back():",
                "def test_add_and_read_back():  # weakened elsewhere",
            ),
        },
    )
    # A plan whose steps name BOTH files: the scenario verification
    # defends against is a record where the surface was somehow in
    # scope — the verdict must still refuse, by name.
    plan = {
        "files": ["workshop/inventory.py", "workshop/tests/test_inventory.py"],
        "steps": [
            {"intent": "fix", "files": ["workshop/inventory.py"], "tests": []},
            {
                "intent": "touch tests",
                "files": ["workshop/tests/test_inventory.py"],
                "tests": [],
            },
        ],
    }
    result = service._verify(gold_order, plan, pristine, patch, changed)
    assert result["passed"] is False
    assert "test_surface_touched" in result["reason"]
    assert result["test_surface_touched"] is True
    assert result["test_surface_files"] == ["workshop/tests/test_inventory.py"]
    assert result["visible_verdict"] == "not_run"


def test_gold_patch_touching_a_grading_path_fails_by_name(service, gold_order):
    pristine = _gold_pristine(service, gold_order)
    task = service.gold.get("reserve-overcommit")
    fix = task.scripted_edits[0]
    # Build the grading-path patch by hand: the pristine target has
    # no grading/ tree (it is staged only into verify copies), so a
    # patch creating a file there is a creation diff.
    workspace = pristine.parent / "crafted-grading"
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(pristine, workspace, ignore=shutil.ignore_patterns("__pycache__"))
    inv = workspace / "workshop" / "inventory.py"
    inv.write_text(inv.read_text().replace(fix["old"], fix["new"]))
    staged = workspace / "grading" / "test_grading_inventory.py"
    staged.parent.mkdir(parents=True)
    staged.write_text("def test_fake():\n    assert True\n")
    patch, changed = build_patch(pristine, workspace)
    plan = {
        "files": ["workshop/inventory.py", "grading/test_grading_inventory.py"],
        "steps": [
            {"intent": "fix", "files": ["workshop/inventory.py"], "tests": []},
            {
                "intent": "stage a mimic",
                "files": ["grading/test_grading_inventory.py"],
                "tests": [],
            },
        ],
    }
    result = service._verify(gold_order, plan, pristine, patch, changed)
    assert result["passed"] is False
    assert "test_surface_touched" in result["reason"]
    assert result["test_surface_files"] == ["grading/test_grading_inventory.py"]


# -- scope-creep detection, gold ------------------------------------------


def test_gold_unplanned_file_fails_by_name(service, gold_order):
    pristine = _gold_pristine(service, gold_order)
    task = service.gold.get("reserve-overcommit")
    fix = task.scripted_edits[0]
    patch, changed = _patched_copy(
        pristine,
        pristine.parent / "crafted-creep",
        {
            "workshop/inventory.py": (fix["old"], fix["new"]),
            "workshop/pricing.py": (
                "All money is integer cents.",
                "All money is integer cents. (touched by nobody's plan)",
            ),
        },
    )
    # pricing.py sits in the plan's stored file list, but the only
    # step names inventory.py: the landed pricing edit is unplanned.
    plan = {
        "files": ["workshop/inventory.py", "workshop/pricing.py"],
        "steps": [
            {"intent": "fix", "files": ["workshop/inventory.py"], "tests": []}
        ],
    }
    result = service._verify(gold_order, plan, pristine, patch, changed)
    assert result["passed"] is False
    assert "unplanned_files" in result["reason"]
    assert result["unplanned_files"] == ["workshop/pricing.py"]
    assert result["planned_files"] == ["workshop/inventory.py"]


# -- patch hygiene at _verify ----------------------------------------------


def _craft_repo(base: Path) -> Path:
    target = base / "target"
    (target / "pkg").mkdir(parents=True)
    (target / "tests").mkdir()
    (target / "pkg" / "__init__.py").write_text("")
    (target / "pkg" / "calc.py").write_text(CALC_BROKEN)
    (target / "tests" / "test_grading_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    return target


def _adhoc_order(service, target: Path) -> dict:
    return service.create_work_order(
        WorkOrderIn(
            title="Fix add",
            task_text="Make add add.",
            file_scope=["pkg/calc.py"],
            target_path=str(target),
            grading_tests=["tests/test_grading_calc.py"],
        ),
        actor="tester",
    )


def test_whitespace_only_patch_fails_as_no_substantive_change(service, tmp_path):
    target = _craft_repo(tmp_path)
    order = _adhoc_order(service, target)
    patch, changed = _patched_copy(
        target,
        tmp_path / "ws",
        {"pkg/calc.py": ("return a - b\n", "return a - b   \n")},
    )
    plan = {
        "files": ["pkg/calc.py"],
        "steps": [{"intent": "fix", "files": ["pkg/calc.py"], "tests": []}],
    }
    result = service._verify(order, plan, target, patch, changed)
    assert result["passed"] is False
    assert "no_substantive_change" in result["reason"]
    assert result["patch_shape"]["whitespace_only"] is True
    assert result["patch_shape"]["empty"] is False
    assert result["visible_verdict"] == "not_run"


def test_empty_patch_keeps_its_honest_name(service, tmp_path):
    target = _craft_repo(tmp_path)
    order = _adhoc_order(service, target)
    plan = {
        "files": ["pkg/calc.py"],
        "steps": [{"intent": "fix", "files": ["pkg/calc.py"], "tests": []}],
    }
    result = service._verify(order, plan, target, "", [])
    assert result["passed"] is False
    assert result["reason"] == "the run produced no patch"
    assert result["patch_shape"]["empty"] is True


# -- hidden re-grade, end to end --------------------------------------------


def test_gold_happy_path_passes_visible_and_hidden(service, gold_order):
    wid = gold_order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid, actor="tester")
    assert run["status"] == "succeeded"
    v = run["verification"]
    assert v["passed"] is True
    assert v["visible_verdict"] == "pass"
    assert v["hidden_verdict"] == "pass"
    assert v["hidden_grading"]["passed"] is True
    assert v["hidden_grading"]["grading_tests"] == [
        "hidden_grading/test_hidden_inventory.py"
    ]
    assert v["hidden_grading"]["grading_evidence"]["tests"] == 3
    assert v["hidden_grading"]["grading_evidence"]["failures"] == 0
    assert v["test_surface_touched"] is False
    assert v["unplanned_files"] == []
    shape = v["patch_shape"]
    assert shape["files_changed"] == 1
    assert shape["per_file"] == [
        {
            "file": "workshop/inventory.py",
            "lines_added": 2,
            "lines_removed": 0,
            "binary": False,
        }
    ]
    assert shape["whitespace_only"] is False and shape["any_binary"] is False
    # The hidden set was never in the run's workspace.
    workspace = Path(run["workspace"])
    assert not (workspace / "hidden_grading").exists()
    assert not (workspace / "grading").exists()


def test_overfit_fix_passes_visible_and_fails_hidden(
    service, gold_order, monkeypatch
):
    """The overfit fix special-cases the one SKU every visible test
    uses (WDG-1). The visible set passes in full; the hidden set —
    other SKUs entirely — fails, and the verdict flips."""
    wid = gold_order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    task = service.gold.get("reserve-overcommit")
    edit = task.scripted_edits[0]
    overfit = {
        "reserve-overcommit": [
            {
                "path": edit["path"],
                "old": edit["old"],
                "new": edit["old"].replace(
                    "        self._reserved[sku]",
                    '        if sku == "WDG-1" and qty > self.available(sku):\n'
                    "            return False\n"
                    "        self._reserved[sku]",
                ),
            }
        ]
    }
    monkeypatch.setattr(
        service_module,
        "make_model",
        lambda profile_id, scripts=None, **_kw: OfflineScriptedModel(
            scripts=overfit
        ),
    )
    run = service.start_run(wid, actor="tester")
    assert run["status"] == "failed"
    v = run["verification"]
    assert v["passed"] is False
    assert v["visible_verdict"] == "pass"
    assert v["grading_evidence"] == {
        "tests": 3,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert v["hidden_verdict"] == "fail"
    assert v["hidden_grading"]["passed"] is False
    assert v["hidden_grading"]["grading_evidence"]["failures"] > 0
    assert v["reason"].startswith("hidden_grading_failed")
    assert "hidden_grading/test_hidden_inventory.py" in v["reason"]


# -- test-surface flag on an ad-hoc order, end to end -----------------------


class _QueueModel(OfflineScriptedModel):
    def __init__(self, steps, scripts=None):
        super().__init__(scripts=scripts)
        self._steps = list(steps)

    def start_run(self, *, work_order, plan):
        self._queue = list(self._steps)


def test_adhoc_test_surface_touch_is_flagged_not_failed(
    service, tmp_path, monkeypatch
):
    target = _craft_repo(tmp_path)
    notes = target / "tests" / "test_notes.py"
    notes.write_text("def test_notes():\n    assert True\n")
    order = service.create_work_order(
        WorkOrderIn(
            title="Fix add, note the tests",
            task_text="Make add add.",
            file_scope=["pkg/calc.py", "tests/test_notes.py"],
            target_path=str(target),
            grading_tests=["tests/test_grading_calc.py"],
        ),
        actor="tester",
    )
    wid = order["work_order_id"]
    steps = [
        Step(
            "edit_file",
            {"path": "pkg/calc.py", "old": "return a - b", "new": "return a + b"},
        ),
        Step(
            "edit_file",
            {
                "path": "tests/test_notes.py",
                "old": "assert True",
                "new": "assert True  # reviewed while fixing add",
            },
        ),
        Step("finish", {}),
    ]
    model = _QueueModel(steps)
    monkeypatch.setattr(
        service_module,
        "make_model",
        lambda profile_id, scripts=None, **_kw: model,
    )
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid, actor="tester")
    # The operator scoped the test file in, so the flag is recorded
    # and named — and the honest grading still decides the run.
    assert run["status"] == "succeeded"
    v = run["verification"]
    assert v["passed"] is True
    assert v["test_surface_touched"] is True
    assert v["test_surface_files"] == ["tests/test_notes.py"]
    assert v["hidden_verdict"] == "not_applicable"
    # The review gate still works; the flag rides the audit record.
    audit = [
        a for a in service.audit(wid) if a["event"] == "verification_completed"
    ]
    assert audit[0]["detail"]["test_surface_files"] == ["tests/test_notes.py"]
    final = service.decide_review(wid, accept=True, actor="Reviewer")
    assert final["status"] == "accepted"
