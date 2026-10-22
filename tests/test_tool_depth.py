"""Tool depth (round 4): named edit refusals with recovery, bounded
reads, honest search, and affected-test selection computed in code."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

import open_model_coding_agent.service as service_module
from open_model_coding_agent.gold import GoldSuite
from open_model_coding_agent.model_port import OfflineScriptedModel, Step
from open_model_coding_agent.tools import SandboxTools, dispatch

WORKSHOP = Path(__file__).resolve().parents[1] / "workshop"
GOLD_DIR = Path(__file__).resolve().parents[1] / "evals" / "gold"


@pytest.fixture()
def repo_tools(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(WORKSHOP, root / "workshop")
    return SandboxTools(
        root,
        scope=["workshop/inventory.py", "workshop/pricing.py", "workshop/orders.py"],
        test_timeout=60,
    )


# -- anchored edits: codes, digests, recovery ---------------------------


def test_edit_success_records_anchor_and_digests(repo_tools):
    old = "def skus(self)"
    result = repo_tools.edit_file(
        "workshop/inventory.py", old, "def skus(self)  # all"
    )
    assert result.ok
    detail = result.detail
    assert detail["anchor"] == old
    assert detail["anchor_sha256"] == hashlib.sha256(old.encode()).hexdigest()
    assert detail["before_sha256"] != detail["after_sha256"]
    assert repo_tools.edited_files == ["workshop/inventory.py"]


def test_anchor_refusals_carry_stable_codes(repo_tools):
    missing = repo_tools.edit_file("workshop/inventory.py", "no such text", "y")
    assert not missing.ok and missing.refused
    assert missing.code == "anchor_not_found"

    ambiguous = repo_tools.edit_file("workshop/inventory.py", "return", "return")
    assert not ambiguous.ok and ambiguous.refused
    assert ambiguous.code == "anchor_ambiguous"
    assert ambiguous.detail["occurrences"] > 1


def test_an_empty_anchor_is_named_not_counted(repo_tools):
    # A live llama3.1 run sent `old` empty four times and was told the
    # anchor "occurs 3126 times" — the refusal now says what is wrong.
    empty = repo_tools.edit_file("workshop/inventory.py", "", "x = 1")
    assert empty.refused and empty.code == "anchor_not_found"
    assert "anchor `old` is empty" in empty.output
    assert "occurs" not in empty.output


def test_dispatch_names_the_arguments_a_call_left_out(repo_tools):
    edit = dispatch(
        repo_tools,
        Step("edit_file", {"path": "workshop/inventory.py",
                           "old_str": "def skus(self)", "new_str": "x"}),
    )
    assert edit.refused
    assert "missing argument(s) ['old', 'new']" in edit.output
    assert "received ['new_str', 'old_str', 'path']" in edit.output

    tests = dispatch(
        repo_tools,
        Step("run_tests", {"test_files": "['workshop/inventory.py']"}),
    )
    assert tests.refused and tests.code == "no_test_paths"
    assert "this tool takes ['paths']" in tests.output


def test_a_bare_string_path_is_one_path_not_its_characters(repo_tools):
    result = dispatch(
        repo_tools, Step("run_tests", {"paths": "workshop/inventory.py"})
    )
    # Refused as a non-test file by its whole name — never as "w".
    assert result.refused
    assert "workshop/inventory.py is not a test file" in result.output


def test_ambiguous_anchor_recovers_by_narrowing(repo_tools):
    refused = repo_tools.edit_file("workshop/pricing.py", "return", "return")
    assert refused.code == "anchor_ambiguous"
    landed = repo_tools.edit_file(
        "workshop/pricing.py",
        "    if qty >= 10:\n        return 5\n    return 0",
        "    if qty >= 10:\n        return 5\n    return 0  # no discount",
    )
    assert landed.ok
    assert "no discount" in (repo_tools.root / "workshop/pricing.py").read_text()


# -- read discipline ------------------------------------------------------


def test_read_line_range_returns_exactly_those_lines(repo_tools):
    text = (WORKSHOP / "pricing.py").read_text()
    lines = text.splitlines()
    result = repo_tools.read_file("workshop/pricing.py", start_line=2, end_line=4)
    assert result.ok
    assert result.output == "\n".join(lines[1:4]) + "\n"
    assert result.detail["start_line"] == 2
    assert result.detail["end_line"] == 4
    assert result.detail["truncated"] is False


def test_read_range_refusals_are_named(repo_tools):
    invalid = repo_tools.read_file("workshop/pricing.py", start_line=0)
    assert not invalid.ok and invalid.code == "invalid_line_range"
    backwards = repo_tools.read_file(
        "workshop/pricing.py", start_line=9, end_line=3
    )
    assert not backwards.ok and backwards.code == "invalid_line_range"
    beyond = repo_tools.read_file("workshop/pricing.py", start_line=10000)
    assert not beyond.ok and beyond.code == "line_range_out_of_bounds"


def test_read_oversized_file_truncates_with_the_true_size(repo_tools):
    big = repo_tools.root / "workshop" / "big.py"
    big.write_text("".join(f"value_{i} = {i}\n" for i in range(4000)))
    total = big.stat().st_size
    assert total > 32768
    result = repo_tools.read_file("workshop/big.py")
    assert result.ok
    assert result.detail["truncated"] is True
    assert result.detail["total_bytes"] == total
    assert "[read truncated:" in result.output
    assert f"{total} bytes" in result.output
    assert "value_0 = 0" in result.output  # the head, not the tail
    assert "value_3999" not in result.output


def test_binary_files_refuse_by_name(repo_tools, tmp_path):
    blob = repo_tools.root / "workshop" / "blob.bin"
    blob.write_bytes(b"\x00\x01\x02\xff\xfe binary")
    read = repo_tools.read_file("workshop/blob.bin")
    assert not read.ok and read.refused and read.code == "binary_file"
    scoped = SandboxTools(
        repo_tools.root, scope=["workshop/blob.bin"], test_timeout=60
    )
    edit = scoped.edit_file("workshop/blob.bin", "x", "y")
    assert not edit.ok and edit.refused and edit.code == "binary_file"


# -- search precision -------------------------------------------------------


@pytest.fixture()
def search_tools(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "many.py").write_text(
        "".join(f'entry_{i} = "needle"\n' for i in range(60))
    )
    (root / "pkg" / "small.py").write_text(
        'alpha = 1\nbeta = "Needle"\ngamma = 3\n'
    )
    (root / "notes.txt").write_text("a needle in a text file\n")
    return SandboxTools(root, scope=[], test_timeout=60)


def test_search_truncation_is_labelled_with_the_total(search_tools):
    result = search_tools.search("needle", limit=10)
    assert result.ok
    assert result.detail["total_matches"] == 60
    assert result.detail["shown_matches"] == 10
    assert result.detail["truncated"] is True
    assert "[search truncated: showing 10 of 60 matches" in result.output


def test_search_context_and_case(search_tools):
    sensitive = search_tools.search("Needle", context=1)
    assert sensitive.detail["total_matches"] == 1
    assert 'pkg/small.py:2: beta = "Needle"' in sensitive.output
    assert "pkg/small.py:1- alpha = 1" in sensitive.output
    assert "pkg/small.py:3- gamma = 3" in sensitive.output
    strict = search_tools.search("needle", path="pkg/small.py")
    assert strict.detail["total_matches"] == 0
    relaxed = search_tools.search(
        "needle", path="pkg/small.py", case_sensitive=False
    )
    assert relaxed.detail["total_matches"] == 1


def test_search_glob_scopes_the_file_kinds(search_tools):
    by_default = search_tools.search("needle")
    assert by_default.detail["total_matches"] == 60  # .py only
    text = search_tools.search("needle", glob="*.txt")
    assert text.detail["total_matches"] == 1
    assert "notes.txt:1:" in text.output


def test_search_outside_the_workspace_refuses_by_name(search_tools):
    by_path = search_tools.search("needle", path="../outside")
    assert not by_path.ok and by_path.refused
    assert by_path.code == "search_outside_workspace"
    by_glob = search_tools.search("needle", glob="../*.py")
    assert not by_glob.ok and by_glob.refused
    assert by_glob.code == "search_glob_invalid"


# -- affected-test selection -------------------------------------------------


def test_affected_tests_on_the_two_file_gold_task(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(WORKSHOP, root / "workshop")
    task = GoldSuite(GOLD_DIR).get("order-coupon")
    tools = SandboxTools(
        root,
        scope=list(task.file_scope),
        test_timeout=60,
        plan_steps=[dict(step) for step in task.plan_steps],
    )
    assert tools.affected_tests() == []
    assert tools.edit_file(
        "workshop/pricing.py",
        "def line_total(unit_cents: int, qty: int) -> int:",
        "def line_total(unit_cents: int, qty: int) -> int:  # touched",
    ).ok
    # Plan rule: the pricing step names the grading tests; import
    # rule: the workshop's pricing tests import workshop.pricing.
    assert tools.affected_tests() == [
        "grading/test_grading_coupon.py",
        "workshop/tests/test_pricing.py",
    ]
    assert tools.edit_file(
        "workshop/orders.py", "class Order:", "class Order:  # touched"
    ).ok
    assert tools.affected_tests() == [
        "grading/test_grading_coupon.py",
        "workshop/tests/test_orders.py",
        "workshop/tests/test_pricing.py",
    ]


def test_run_tests_only_affected_whole_set_subset_and_miss(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(WORKSHOP, root / "workshop")
    steps = [
        {
            "intent": "touch pricing",
            "files": ["workshop/pricing.py"],
            "tests": ["workshop/tests/test_pricing.py"],
        }
    ]
    tools = SandboxTools(
        root, scope=["workshop/pricing.py"], test_timeout=60, plan_steps=steps
    )
    nothing = tools.run_tests([], affected_only=True)
    assert not nothing.ok and nothing.code == "no_affected_tests"

    assert tools.edit_file(
        "workshop/pricing.py",
        "def line_total(unit_cents: int, qty: int) -> int:",
        "def line_total(unit_cents: int, qty: int) -> int:  # touched",
    ).ok
    whole = tools.run_tests([], affected_only=True)
    assert whole.ok, whole.output
    assert whole.detail["selected"] == ["workshop/tests/test_pricing.py"]
    assert whole.detail["not_in_workspace"] == []

    subset = tools.run_tests(
        ["workshop/tests/test_pricing.py"], affected_only=True
    )
    assert subset.ok, subset.output

    miss = tools.run_tests(
        ["workshop/tests/test_inventory.py"], affected_only=True
    )
    assert not miss.ok and miss.refused and miss.code == "no_affected_tests"


# -- run level: refusals recorded verbatim, recovery end to end --------------


class AmbiguousFirstModel(OfflineScriptedModel):
    """A model that first reaches for too broad an anchor (and a too
    broad search), gets both answers as data, and then does the
    repair with the narrowed anchor — the recovery round 4 exists
    to make possible."""

    def start_run(self, *, work_order, plan):
        super().start_run(work_order=work_order, plan=plan)
        reads = [s for s in self._queue if s.tool == "read_file"]
        rest = [s for s in self._queue if s.tool != "read_file"]
        target = plan["files"][0]
        self._queue = (
            reads
            + [
                Step("search", {"pattern": "def ", "limit": 2}),
                Step(
                    "edit_file",
                    {"path": target, "old": "return", "new": "return"},
                ),
            ]
            + rest
        )


def test_run_recovers_from_an_ambiguous_anchor(service, gold_order, monkeypatch):
    monkeypatch.setattr(
        service_module,
        "make_model",
        lambda profile_id, scripts=None, **_kw: AmbiguousFirstModel(
            scripts=scripts
        ),
    )
    wid = gold_order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    assert run["status"] == "succeeded"
    assert run["verification"]["passed"] is True

    calls = [
        a["detail"]
        for a in service.audit(wid)
        if a["event"] == "tool_call"
    ]
    search_call = next(c for c in calls if c["tool"] == "search")
    assert search_call["result"]["detail"]["truncated"] is True
    assert "search truncated" in search_call["result"]["output"]

    edits = [c for c in calls if c["tool"] == "edit_file"]
    assert edits[0]["refused"] is True
    assert edits[0]["result"]["code"] == "anchor_ambiguous"
    assert edits[1]["ok"] is True
    detail = edits[1]["result"]["detail"]
    assert detail["anchor_sha256"]
    assert detail["before_sha256"] != detail["after_sha256"]
