"""Round 13 — the challenge pass.

A second, independent look at a verified patch, computed from
the run's stored records alone. The pure rules are proved at the
level of :func:`compute_challenges` — each rule fires on a
crafted patch, stays silent on a named/carried one, and the six
gold patches raise nothing at all — and the governance is
proved end to end on a crafted ad-hoc repo: challenges are
raised when a run lands SUCCEEDED, an unanswered must-answer
challenge blocks acceptance by name, the three dispositions
answer it as recorded acts, a newer run supersedes the old
run's challenges without deleting them, and re-running the
pass over the same run stores nothing new.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import open_model_coding_agent.service as service_module
from open_model_coding_agent.api import create_app
from open_model_coding_agent.challenges import compute_challenges
from open_model_coding_agent.errors import GateError
from open_model_coding_agent.gold import GoldSuite
from open_model_coding_agent.model_port import OfflineScriptedModel, Step
from open_model_coding_agent.models import WorkOrderIn
from open_model_coding_agent.patch import build_patch

REPO_ROOT = Path(__file__).resolve().parents[1]

CALC = (
    "def add(a, b):\n"
    "    return a - b\n"
    "\n\n"
    "def helper(amount):\n"
    "    return amount\n"
    "\n\n"
    "def _fee(amount):\n"
    "    return amount\n"
)


# -- the pure rules ------------------------------------------------------


def _plan(intent="Make add add numbers.", approach="Fix add."):
    return {
        "approach": approach,
        "steps": [
            {"intent": intent, "files": ["pkg/calc.py"], "tests": []}
        ],
    }


def test_intent_creep_fires_only_on_unnamed_symbols():
    patch = (
        "--- a/pkg/calc.py\n"
        "+++ b/pkg/calc.py\n"
        "@@ -1,6 +1,6 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
        " \n"
        " \n"
        " def helper(amount):\n"
        "-    return amount\n"
        "+    return abs(amount)\n"
    )
    cands = compute_challenges(
        task_text="Make add add.",
        plan=_plan(),
        patch_text=patch,
        changed_files=["pkg/calc.py"],
        prior_texts={"pkg/calc.py": CALC},
    )
    assert [(c["rule"], c["evidence"]["symbol"]) for c in cands] == [
        ("intent_creep", "helper")
    ]
    assert cands[0]["severity"] == "must_answer"
    assert cands[0]["evidence"]["chain"] == "helper"
    # Naming the symbol in the task text silences the rule: the
    # work was asked for, in the record's own words.
    named = compute_challenges(
        task_text="Make add add, and have helper take the absolute value.",
        plan=_plan(),
        patch_text=patch,
        changed_files=["pkg/calc.py"],
        prior_texts={"pkg/calc.py": CALC},
    )
    assert named == []


def test_intent_creep_private_symbol_is_advisory():
    patch = (
        "--- a/pkg/calc.py\n"
        "+++ b/pkg/calc.py\n"
        "@@ -9,2 +9,2 @@\n"
        " def _fee(amount):\n"
        "-    return amount\n"
        "+    return abs(amount)\n"
    )
    cands = compute_challenges(
        task_text="Make add add.",
        plan=_plan(),
        patch_text=patch,
        changed_files=["pkg/calc.py"],
        prior_texts={"pkg/calc.py": CALC},
    )
    assert [(c["rule"], c["severity"]) for c in cands] == [
        ("intent_creep", "advisory")
    ]


def test_appended_function_is_surface_not_creep():
    patch = (
        "--- a/pkg/calc.py\n"
        "+++ b/pkg/calc.py\n"
        "@@ -5,3 +5,7 @@\n"
        " def helper(amount):\n"
        "     return amount\n"
        "+\n"
        "+\n"
        "+def discount(amount):\n"
        "+    return amount\n"
    )
    cands = compute_challenges(
        task_text="Make add add.",
        plan=_plan(),
        patch_text=patch,
        changed_files=["pkg/calc.py"],
        prior_texts={"pkg/calc.py": CALC},
    )
    assert [(c["rule"], c["evidence"]["symbol"]) for c in cands] == [
        ("unrequested_surface", "discount")
    ]
    # A new private helper is not surface, and a signature change
    # to an existing function is an edit, not an introduction.
    quiet = (
        "--- a/pkg/calc.py\n"
        "+++ b/pkg/calc.py\n"
        "@@ -1,2 +1,6 @@\n"
        "-def add(a, b):\n"
        "+def add(a, b, c=0):\n"
        "     return a - b\n"
        "+\n"
        "+\n"
        "+def _memo(x):\n"
        "+    return x\n"
    )
    assert (
        compute_challenges(
            task_text="Make add add.",
            plan=_plan(),
            patch_text=quiet,
            changed_files=["pkg/calc.py"],
            prior_texts={"pkg/calc.py": CALC},
        )
        == []
    )


def test_single_source_figures_and_their_carriers():
    patch = (
        "--- a/pkg/calc.py\n"
        "+++ b/pkg/calc.py\n"
        "@@ -5,2 +5,2 @@\n"
        " def helper(amount):\n"
        "-    return amount\n"
        "+    return amount * 17 // 100 + 5\n"
    )
    base = dict(
        plan=_plan(),
        patch_text=patch,
        changed_files=["pkg/calc.py"],
        prior_texts={"pkg/calc.py": CALC},
    )
    def figures(cands):
        return {
            c["evidence"]["normalised"]
            for c in cands
            if c["rule"] == "single_source_figures"
        }

    fired = compute_challenges(task_text="Make add add.", **base)
    # All three figures fire — 17, 100, and 5: the exemption is
    # 0 and 1 only, and nothing in the records carries these.
    # (The patch also edits helper, so intent creep fires beside
    # them; this test reads the figures rule's own candidates.)
    assert figures(fired) == {"17", "100", "5"}
    # A figure the task text carries is not single-source.
    carried = compute_challenges(
        task_text="Make add add; the house takes 17 percent.", **base
    )
    assert figures(carried) == {"100", "5"}
    # A figure the file already carried is not single-source —
    # by value, so 100.0 in the prior content carries 100.
    prior = CALC + "\n\nRATE = 100.0\n"
    carried_prior = compute_challenges(
        task_text="Make add add.",
        plan=_plan(),
        patch_text=patch,
        changed_files=["pkg/calc.py"],
        prior_texts={"pkg/calc.py": prior},
    )
    assert figures(carried_prior) == {"17", "5"}
    # 0 and 1 are structural: defaults and indexes, not figures.
    structural = (
        "--- a/pkg/calc.py\n"
        "+++ b/pkg/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return [a, b][0] + [a, b][1] - 1 + 1\n"
    )
    assert (
        compute_challenges(
            task_text="Make add add.",
            plan=_plan(),
            patch_text=structural,
            changed_files=["pkg/calc.py"],
            prior_texts={"pkg/calc.py": CALC},
        )
        == []
    )


def test_gold_patches_raise_no_challenges(tmp_path):
    """The binding proof: the pass over each gold task's own
    verified patch — built from the suite's broken and fixed
    overlays exactly as a run's patch is built — is empty. A
    rule that fires on a gold patch is a wrong rule."""
    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    workshop = REPO_ROOT / "workshop"
    for task in suite.tasks:
        trees = {}
        for kind, overlay in (
            ("prior", task.broken_files()),
            ("fixed", task.fixed_files()),
        ):
            dest = tmp_path / task.id / kind
            dest.mkdir(parents=True)
            shutil.copytree(
                workshop,
                dest / "workshop",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            for rel, text in overlay.items():
                (dest / rel).write_text(text)
            trees[kind] = dest
        patch, changed = build_patch(trees["prior"], trees["fixed"])
        plan = {"approach": task.plan_approach, "steps": task.plan_steps}
        cands = compute_challenges(
            task_text=task.task_text,
            plan=plan,
            patch_text=patch,
            changed_files=changed,
            prior_texts={
                rel: (trees["prior"] / rel).read_text() for rel in changed
            },
        )
        assert cands == [], f"{task.id}: {cands}"


# -- end to end on a crafted repo ------------------------------------------


class _QueueModel(OfflineScriptedModel):
    def __init__(self, steps, scripts=None):
        super().__init__(scripts=scripts)
        self._steps = list(steps)

    def start_run(self, *, work_order, plan):
        self._queue = list(self._steps)


def _craft_repo(base: Path) -> Path:
    target = base / "target"
    (target / "pkg").mkdir(parents=True)
    (target / "tests").mkdir()
    (target / "pkg" / "__init__.py").write_text("")
    (target / "pkg" / "calc.py").write_text(CALC)
    (target / "tests" / "test_grading_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    return target


def _run_crafted(service, monkeypatch, target: Path, steps) -> tuple[str, dict]:
    model = _QueueModel(steps)
    monkeypatch.setattr(
        service_module,
        "make_model",
        lambda profile_id, scripts=None, **_kw: model,
    )
    order = service.create_work_order(
        WorkOrderIn(
            title="Fix add",
            task_text="Make add add.",
            file_scope=["pkg/calc.py"],
            target_path=str(target),
            grading_tests=["tests/test_grading_calc.py"],
        ),
        actor="tester",
    )
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid, actor="tester")
    assert run["status"] == "succeeded"
    return wid, run


_FIX_ADD = Step(
    "edit_file",
    {"path": "pkg/calc.py", "old": "return a - b", "new": "return a + b"},
)
_CREEP_EDIT = Step(
    "edit_file",
    {
        "path": "pkg/calc.py",
        "old": "def helper(amount):\n    return amount",
        "new": "def helper(amount):\n    return abs(amount)",
    },
)
_FIGURES_EDIT = Step(
    "edit_file",
    {
        "path": "pkg/calc.py",
        "old": "def helper(amount):\n    return amount",
        "new": "def helper(amount):\n    return amount * 17 // 100",
    },
)
# A figure inside the named function only: figures fire, creep
# does not — the downgrade test's isolated case. The arithmetic
# still satisfies the grading test (17 - 17 is no change).
_FIGURE_IN_ADD = Step(
    "edit_file",
    {
        "path": "pkg/calc.py",
        "old": "return a - b",
        "new": "return a + b + 17 - 17",
    },
)
_FINISH = Step("finish", {})


def test_challenge_blocks_accept_until_answered(
    service, tmp_path, monkeypatch
):
    target = _craft_repo(tmp_path)
    wid, run = _run_crafted(
        service, monkeypatch, target, [_FIX_ADD, _CREEP_EDIT, _FINISH]
    )
    challenges = service.list_challenges(wid, run_id=run["run_id"])
    assert [c["rule"] for c in challenges] == ["intent_creep"]
    challenge = challenges[0]
    assert challenge["status"] == "open"
    assert challenge["severity"] == "must_answer"
    assert challenge["evidence"]["symbol"] == "helper"

    # The packet and the queue both carry the challenge.
    packet = service.review_packet(wid)
    assert packet["challenges"][0]["challenge_id"] == challenge["challenge_id"]
    assert packet["challenge_summary"]["open_must_answer"] == 1
    entry = next(
        e for e in service.review_queue()["entries"]
        if e["work_order_id"] == wid
    )
    assert entry["unanswered_must_answer"] == 1

    # Acceptance refuses, naming the count.
    with pytest.raises(GateError) as refused:
        service.decide_review(wid, accept=True, actor="Reviewer")
    assert refused.value.code == "unanswered_challenges"
    assert "1 unanswered" in refused.value.message

    # A rebuttal without its cited counter-evidence refuses; one
    # with it answers the challenge, and acceptance lands.
    with pytest.raises(GateError) as no_evidence:
        service.answer_challenge(
            wid, challenge["challenge_id"], kind="rebut",
            actor="Reviewer", text="helper is named in the plan",
        )
    assert no_evidence.value.code == "rebuttal_needs_evidence"
    answered = service.answer_challenge(
        wid, challenge["challenge_id"], kind="rebut",
        actor="Reviewer",
        text="the helper change is the fix's own guard",
        cites="plan step 1",
    )
    assert answered["status"] == "answered"
    assert answered["disposition"]["kind"] == "rebut"
    assert answered["disposition"]["cites"] == "plan step 1"
    final = service.decide_review(wid, accept=True, actor="Reviewer")
    assert final["status"] == "accepted"

    events = [
        a["event"] for a in service.audit(wid)
    ]
    assert "challenge_raised" in events
    assert "challenge_answered" in events


def test_answer_refusals_are_named(service, tmp_path, monkeypatch):
    target = _craft_repo(tmp_path)
    wid, run = _run_crafted(
        service, monkeypatch, target, [_FIX_ADD, _CREEP_EDIT, _FINISH]
    )
    challenge = service.list_challenges(wid)[0]
    cid = challenge["challenge_id"]
    with pytest.raises(GateError) as unknown:
        service.answer_challenge(
            wid, "challenge-nope", kind="rebut", actor="R",
            text="x", cites="y",
        )
    assert unknown.value.code == "unknown_challenge"
    with pytest.raises(GateError) as bad_kind:
        service.answer_challenge(
            wid, cid, kind="shrug", actor="R",
        )
    assert bad_kind.value.code == "unknown_challenge_disposition"
    service.answer_challenge(
        wid, cid, kind="rebut", actor="R", text="stands", cites="task text",
    )
    with pytest.raises(GateError) as twice:
        service.answer_challenge(
            wid, cid, kind="rebut", actor="R", text="again", cites="task text",
        )
    assert twice.value.code == "challenge_already_answered"


def test_downgrade_keeps_the_original_severity(
    service, tmp_path, monkeypatch
):
    target = _craft_repo(tmp_path)
    wid, run = _run_crafted(
        service, monkeypatch, target, [_FIGURE_IN_ADD, _FINISH]
    )
    challenges = service.list_challenges(wid, run_id=run["run_id"])
    assert [c["rule"] for c in challenges] == ["single_source_figures"]
    first = challenges[0]
    assert first["evidence"]["value"] == "17"
    with pytest.raises(GateError) as no_reason:
        service.answer_challenge(
            wid, first["challenge_id"], kind="downgrade", actor="Reviewer",
        )
    assert no_reason.value.code == "downgrade_reason_required"
    answered = service.answer_challenge(
        wid, first["challenge_id"], kind="downgrade", actor="Reviewer",
        reason="the rate is the house standard, named in the ticket thread",
    )
    assert answered["severity"] == "must_answer"  # the rule's, kept
    assert answered["current_severity"] == "advisory"  # the person's, moved
    assert answered["disposition"]["reason"].startswith("the rate is")
    assert service.review_packet(wid)["challenge_summary"][
        "open_must_answer"
    ] == 0
    final = service.decide_review(wid, accept=True, actor="Reviewer")
    assert final["status"] == "accepted"
    # An answered challenge takes no second answer, of any kind.
    with pytest.raises(GateError) as again:
        service.answer_challenge(
            wid, first["challenge_id"], kind="downgrade",
            actor="Reviewer", reason="again",
        )
    assert again.value.code == "challenge_already_answered"


def test_advisory_challenge_never_blocks(service, tmp_path, monkeypatch):
    fee_edit = Step(
        "edit_file",
        {
            "path": "pkg/calc.py",
            "old": "def _fee(amount):\n    return amount",
            "new": "def _fee(amount):\n    return abs(amount)",
        },
    )
    target = _craft_repo(tmp_path)
    wid, run = _run_crafted(
        service, monkeypatch, target, [_FIX_ADD, fee_edit, _FINISH]
    )
    challenges = service.list_challenges(wid, run_id=run["run_id"])
    assert [(c["rule"], c["current_severity"]) for c in challenges] == [
        ("intent_creep", "advisory")
    ]
    entry = next(
        e for e in service.review_queue()["entries"]
        if e["work_order_id"] == wid
    )
    assert entry["unanswered_must_answer"] == 0
    final = service.decide_review(wid, accept=True, actor="Reviewer")
    assert final["status"] == "accepted"
    # Downgrading an advisory challenge refuses by name: only a
    # must-answer challenge can be downgraded.
    advisory = challenges[0]
    with pytest.raises(GateError) as not_downgradable:
        service.answer_challenge(
            wid, advisory["challenge_id"], kind="downgrade",
            actor="Reviewer", reason="n/a",
        )
    assert not_downgradable.value.code == "challenge_not_downgradable"


def test_accept_and_revise_then_supersede(service, tmp_path, monkeypatch):
    # One edit, three challenges: the figures edit strays into
    # helper (intent creep) and introduces two figures no record
    # carries. Answer the creep by accepting and revising; the
    # figures stay open — and unanswerable once superseded.
    target = _craft_repo(tmp_path)
    wid, first = _run_crafted(
        service, monkeypatch, target, [_FIX_ADD, _FIGURES_EDIT, _FINISH],
    )
    challenges = {
        c["rule"]: c for c in service.list_challenges(wid, run_id=first["run_id"])
    }
    assert set(challenges) == {"intent_creep", "single_source_figures"}
    creep = challenges["intent_creep"]

    sent_back = service.answer_challenge(
        wid, creep["challenge_id"], kind="accept_and_revise",
        actor="Reviewer", text="keep the fix inside add",
    )
    assert sent_back["status"] == "answered"
    order = service.get_work_order(wid)
    assert order["status"] == "changes_requested"
    assert creep["challenge_id"] in order["review_note"]
    assert "intent_creep" in order["review_note"]

    # A fresh plan and run: the first run's challenges — answered
    # and open alike — are marked superseded, never deleted, and
    # the open one takes no answer any more.
    open_before = [
        c for c in service.list_challenges(wid, run_id=first["run_id"])
        if c["status"] == "open"
    ]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    clean_model = _QueueModel([_FIX_ADD, _FINISH])
    monkeypatch.setattr(
        service_module,
        "make_model",
        lambda profile_id, scripts=None, **_kw: clean_model,
    )
    second = service.start_run(wid, actor="tester")
    assert second["status"] == "succeeded"
    old = service.list_challenges(wid, run_id=first["run_id"])
    assert old and all(c["status"] == "superseded" for c in old)
    answered_old = next(
        c for c in old if c["challenge_id"] == creep["challenge_id"]
    )
    assert answered_old["disposition"]["kind"] == "accept_and_revise"
    for stale in open_before:
        with pytest.raises(GateError) as superseded:
            service.answer_challenge(
                wid, stale["challenge_id"], kind="rebut",
                actor="Reviewer", text="too late", cites="task text",
            )
        assert superseded.value.code == "challenge_superseded"
    events = [a["event"] for a in service.audit(wid)]
    assert "challenge_superseded" in events
    # The new run is clean, and its packet says so.
    assert service.list_challenges(wid, run_id=second["run_id"]) == []
    final = service.decide_review(wid, accept=True, actor="Reviewer")
    assert final["status"] == "accepted"


def test_pass_is_idempotent_and_needs_a_succeeded_run(
    service, tmp_path, monkeypatch
):
    target = _craft_repo(tmp_path)
    wid, run = _run_crafted(
        service, monkeypatch, target, [_FIX_ADD, _CREEP_EDIT, _FINISH]
    )
    before = service.list_challenges(wid, run_id=run["run_id"])
    again = service.challenge_pass(wid, run_id=run["run_id"])
    assert [c["challenge_id"] for c in again] == [
        c["challenge_id"] for c in before
    ]
    assert len(service.list_challenges(wid)) == len(before)

    fresh = service.create_gold_work_order("reserve-overcommit", actor="t")
    with pytest.raises(GateError) as no_run:
        service.challenge_pass(fresh["work_order_id"])
    assert no_run.value.code == "unknown_run"


def test_gold_run_raises_no_challenges(service, approved_order):
    wid = approved_order["work_order_id"]
    run = service.start_run(wid, actor="tester")
    assert run["status"] == "succeeded"
    assert service.list_challenges(wid, run_id=run["run_id"]) == []
    packet = service.review_packet(wid)
    assert packet["challenge_summary"] == {
        "total": 0,
        "open_must_answer": 0,
        "open_advisory": 0,
        "answered": 0,
        "superseded": 0,
    }
    final = service.decide_review(wid, accept=True, actor="Reviewer")
    assert final["status"] == "accepted"


# -- the API surface ---------------------------------------------------------


def test_challenges_over_the_api(settings, tmp_path, monkeypatch):
    target = _craft_repo(tmp_path)
    app = create_app(settings)
    service = app.state.service
    wid, run = _run_crafted(
        service, monkeypatch, target, [_FIX_ADD, _CREEP_EDIT, _FINISH]
    )
    client = TestClient(app, raise_server_exceptions=False)
    listed = client.get(f"/api/work-orders/{wid}/challenges")
    assert listed.status_code == 200
    body = listed.json()
    assert [c["rule"] for c in body] == ["intent_creep"]
    cid = body[0]["challenge_id"]

    blocked = client.post(
        f"/api/work-orders/{wid}/review",
        json={"accept": True, "actor": "Reviewer"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "unanswered_challenges"

    answered = client.post(
        f"/api/work-orders/{wid}/challenges/{cid}/answer",
        json={
            "kind": "rebut",
            "actor": "Reviewer",
            "text": "the guard is the fix",
            "cites": "plan step 1",
        },
    )
    assert answered.status_code == 200
    assert answered.json()["status"] == "answered"
    accepted = client.post(
        f"/api/work-orders/{wid}/review",
        json={"accept": True, "actor": "Reviewer"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    missing = client.get("/api/work-orders/wo-nope/challenges")
    assert missing.status_code == 404
