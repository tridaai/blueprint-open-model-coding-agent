"""Round 11: the review workbench.

The queue is a pure derivation over the stored records — orders a
person must act on, each naming its single next act and its age,
with no queue table and no bulk decision anywhere in the route
table. The packet assembles everything a decision stands on.
Request changes is a third recorded decision: its note is stored
on the order and shown to the next plan draft. Reviewer history
folds the decision records by actor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import open_model_coding_agent.service as service_module
from open_model_coding_agent.api import create_app
from open_model_coding_agent.errors import GateError
from open_model_coding_agent.model_port import OfflineScriptedModel, Step
from open_model_coding_agent.models import WorkOrderIn


# -- helpers -----------------------------------------------------------------


class DiesAfterNCalls(OfflineScriptedModel):
    """The scripted model, until its Nth next_step call — which
    raises the way a dying process does, after real work landed."""

    def __init__(self, scripts=None, *, die_on: int):
        super().__init__(scripts=scripts)
        self._calls = 0
        self._die_on = die_on

    def next_step(self, *, transcript):
        self._calls += 1
        if self._calls >= self._die_on:
            raise RuntimeError("the process died mid-run")
        return super().next_step(transcript=transcript)


@pytest.fixture()
def model_factory(monkeypatch):
    state = {"make": None}

    def install(make):
        state["make"] = make
        monkeypatch.setattr(
            service_module,
            "make_model",
            lambda profile_id, scripts=None, **_kw: state["make"](scripts),
        )

    return install


def _succeeded(service, gold_task_id="reserve-overcommit"):
    order = service.create_gold_work_order(gold_task_id, actor="tester")
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid, actor="tester")
    assert run["status"] == "succeeded"
    return wid, run


def _failed(service, monkeypatch):
    order = service.create_gold_work_order("reserve-overcommit", actor="tester")
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    bad_script = {
        "reserve-overcommit": [
            {
                "path": "workshop/inventory.py",
                "old": "        self._reserved[sku] = self._reserved.get(sku, 0) + qty\n        return True",
                "new": "        self._reserved[sku] = self._reserved.get(sku, 0) + qty\n        return True  # looks fine",
            }
        ]
    }
    monkeypatch.setattr(
        service_module,
        "make_model",
        lambda profile_id, scripts=None, **_kw: OfflineScriptedModel(
            scripts=bad_script
        ),
    )
    run = service.start_run(wid, actor="tester")
    assert run["status"] == "failed"
    return wid, run


# -- the queue -----------------------------------------------------------------


def test_queue_derives_every_waiting_state(service, monkeypatch, model_factory):
    # A plan awaiting its decision.
    plan_order = service.create_gold_work_order("tax-before-discount", actor="t")
    service.draft_plan(plan_order["work_order_id"], actor="t")

    # A verified run awaiting review.
    succeeded_wid, run = _succeeded(service)

    # A failed run, with its failure class.
    failed_wid, _bad_run = _failed(service, monkeypatch)

    # An interrupted run awaiting a recovery decision: the model
    # dies on its third call — after the read and the landed edit.
    order = service.create_gold_work_order("low-stock-report", actor="t")
    interrupted_wid = order["work_order_id"]
    service.draft_plan(interrupted_wid, actor="t")
    service.decide_plan(interrupted_wid, approve=True, actor="Reviewer")
    model_factory(lambda scripts: DiesAfterNCalls(scripts, die_on=3))
    dead = service.start_run(interrupted_wid, actor="t")
    assert dead["status"] == "interrupted"
    model_factory(lambda scripts: OfflineScriptedModel(scripts=scripts))

    # Orders a reviewer does NOT see: a bare draft, and an accepted
    # order (decided — nothing waits).
    service.create_gold_work_order("cancel-leaks-reservations", actor="t")
    accepted_wid, _ = _succeeded(service, "bulk-tier-boundary")
    service.decide_review(accepted_wid, accept=True, actor="Reviewer")

    queue = service.review_queue()
    by_id = {e["work_order_id"]: e for e in queue["entries"]}
    assert queue["count"] == 4
    assert set(by_id) == {
        plan_order["work_order_id"],
        succeeded_wid,
        failed_wid,
        interrupted_wid,
    }

    plan_entry = by_id[plan_order["work_order_id"]]
    assert plan_entry["category"] == "plan_awaiting_approval"
    assert plan_entry["next_act"] == "decide_plan"
    assert plan_entry["plan_version"] == 1

    review_entry = by_id[succeeded_wid]
    assert review_entry["category"] == "awaiting_review"
    assert review_entry["next_act"] == "review"
    assert review_entry["run_id"] == run["run_id"]
    assert review_entry["risk_flags"]["hidden_verdict"] == "pass"
    assert review_entry["risk_flags"]["test_surface_touched"] is False

    failed_entry = by_id[failed_wid]
    assert failed_entry["category"] == "run_failed"
    assert failed_entry["next_act"] == "restart_run"
    assert failed_entry["failure_class"] == "verification_failed"
    assert "grading tests fail" in failed_entry["failure_detail"]

    interrupted_entry = by_id[interrupted_wid]
    assert interrupted_entry["category"] == "run_interrupted"
    assert interrupted_entry["next_act"] == "resume_run"
    assert interrupted_entry["available_acts"] == [
        "resume_run",
        "restart_run",
        "abandon_run",
    ]
    assert interrupted_entry["failure_class"] == "run_interrupted"


def test_queue_age_is_time_in_the_current_state(service):
    order = service.create_gold_work_order("reserve-overcommit", actor="t")
    service.draft_plan(order["work_order_id"], actor="t")
    future = "2999-01-01T00:00:00+00:00"
    entry = service.review_queue(now=future)["entries"][0]
    since = datetime.fromisoformat(entry["since"])
    expected = int(
        (datetime(2999, 1, 1, tzinfo=timezone.utc) - since).total_seconds()
    )
    assert entry["age_seconds"] == expected


def test_no_bulk_decision_route_exists(settings):
    """The route table is the proof: every decision route is a POST
    on ONE work order; the queue and the history are reads only."""
    app = create_app(settings)
    routes = [(r.path, set(r.methods or ())) for r in app.routes]
    for path, _methods in routes:
        assert "bulk" not in path
        assert "batch" not in path
    decision_posts = [
        path
        for path, methods in routes
        if "POST" in methods and ("review" in path or "decide" in path)
    ]
    assert decision_posts  # the per-order decision routes exist
    for path in decision_posts:
        assert "{work_order_id}" in path
    queue_routes = [(p, m) for p, m in routes if p == "/api/review-queue"]
    assert queue_routes and all(m == {"GET"} for _p, m in queue_routes)


# -- the packet ------------------------------------------------------------------


def test_packet_assembles_the_whole_decision(service):
    wid, run = _succeeded(service)
    packet = service.review_packet(wid)
    assert packet["task"]["task_text"]
    assert packet["task"]["file_scope"] == ["workshop/inventory.py"]
    assert packet["plan"]["version"] == run["plan_version"]
    assert packet["plan"]["fingerprint_state"] == "current"
    assert packet["run"]["run_id"] == run["run_id"]
    assert packet["patch"] == run["patch"] and packet["patch"]
    assert [f["file"] for f in packet["patch_shape"]["per_file"]] == [
        "workshop/inventory.py"
    ]
    assert packet["verification"]["passed"] is True
    flags = packet["risk_flags"]
    assert flags["visible_verdict"] == "pass"
    assert flags["hidden_verdict"] == "pass"
    assert flags["hidden_verdict_absent"] is False
    assert flags["test_surface_touched"] is False
    assert flags["unplanned_files"] == []
    assert packet["usage"]  # the run's usage rows ride the packet
    assert packet["cost"]["total"] is not None
    assert [h["run_id"] for h in packet["run_history"]] == [run["run_id"]]
    assert packet["review_decisions"] == []
    assert packet["changes_since_previous"] is None
    assert service.review_packet("wo-missing") is None


def test_packet_names_the_absent_hidden_verdict_for_adhoc(
    service, tmp_path, monkeypatch
):
    target = tmp_path / "target"
    (target / "pkg").mkdir(parents=True)
    (target / "tests").mkdir()
    (target / "pkg" / "__init__.py").write_text("")
    (target / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n"
    )
    (target / "tests" / "test_grading_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    order = service.create_work_order(
        WorkOrderIn(
            title="Make add add",
            task_text="Make add add.",
            file_scope=["pkg/calc.py"],
            target_path=str(target),
            grading_tests=["tests/test_grading_calc.py"],
        ),
        actor="tester",
    )
    wid = order["work_order_id"]

    class QueueModel(OfflineScriptedModel):
        def start_run(self, *, work_order, plan):
            self._queue = [
                Step(
                    "edit_file",
                    {
                        "path": "pkg/calc.py",
                        "old": "return a - b",
                        "new": "return a + b",
                    },
                ),
                Step("finish", {}),
            ]

    monkeypatch.setattr(
        service_module,
        "make_model",
        lambda profile_id, scripts=None, **_kw: QueueModel(scripts),
    )
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid, actor="tester")
    assert run["status"] == "succeeded"
    flags = service.review_packet(wid)["risk_flags"]
    # An ad-hoc order has no hidden set: the flag says absent, and
    # the verdict field says not_applicable — never a silent pass.
    assert flags["hidden_verdict"] == "not_applicable"
    assert flags["hidden_verdict_absent"] is True


# -- request changes -----------------------------------------------------------


def test_request_changes_sends_the_order_back_with_its_note(
    service, monkeypatch
):
    wid, _run = _succeeded(service)
    seen: list[dict] = []
    original = OfflineScriptedModel.draft_plan

    def spy(self, *, work_order, repo_files):
        seen.append(dict(work_order))
        return original(self, work_order=work_order, repo_files=repo_files)

    monkeypatch.setattr(OfflineScriptedModel, "draft_plan", spy)

    final = service.decide_review(
        wid,
        actor="Lead Reviewer",
        decision="request_changes",
        reason="cover the zero-quantity case as well",
    )
    assert final["status"] == "changes_requested"
    assert final["review_note"] == "cover the zero-quantity case as well"

    # The decision is on the audit trail with the note, and a fresh
    # plan version drafts from the new state — with the note in the
    # context the planner is shown.
    events = [a for a in service.audit(wid) if a["event"] == "review_changes_requested"]
    assert events and events[0]["detail"]["note"] == (
        "cover the zero-quantity case as well"
    )
    plan = service.draft_plan(wid, actor="tester")
    assert plan["version"] == 2
    assert seen and seen[-1]["_review_note"] == (
        "cover the zero-quantity case as well"
    )


def test_request_changes_refusals_are_named(service):
    # No plan, no run at all: nothing to send back.
    order = service.create_gold_work_order("reserve-overcommit", actor="t")
    wid = order["work_order_id"]
    with pytest.raises(GateError) as exc:
        service.decide_review(
            wid, actor="R", decision="request_changes", reason="more work"
        )
    assert exc.value.code == "no_succeeded_run_to_return"

    # Approved but not yet run: still nothing to send back.
    service.draft_plan(wid, actor="t")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    with pytest.raises(GateError) as exc:
        service.decide_review(
            wid, actor="R", decision="request_changes", reason="more work"
        )
    assert exc.value.code == "no_succeeded_run_to_return"


def test_request_changes_needs_its_note(service):
    wid, _run = _succeeded(service)
    with pytest.raises(GateError) as exc:
        service.decide_review(wid, actor="R", decision="request_changes")
    assert exc.value.code == "changes_note_required"
    # The order is untouched: still standing at review.
    assert service.get_work_order(wid)["status"] == "succeeded"


def test_request_changes_can_reverse_a_rejection(service):
    wid, _run = _succeeded(service)
    service.decide_review(wid, accept=False, actor="R", reason="not this patch")
    assert service.get_work_order(wid)["status"] == "rejected"
    # A plain second decision on a rejected order still refuses —
    final = service.decide_review(
        wid,
        actor="R",
        decision="request_changes",
        reason="on reflection, send it back instead",
    )
    assert final["status"] == "changes_requested"
    with pytest.raises(GateError) as exc:
        service.decide_review(wid, accept=True, actor="R")
    assert exc.value.code == "no_verified_run_awaiting_review"


def test_rereview_packet_names_what_changed(service):
    wid, first = _succeeded(service)
    service.decide_review(
        wid,
        actor="Lead Reviewer",
        decision="request_changes",
        reason="tighten the guard",
    )
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    second = service.start_run(wid, actor="tester")
    assert second["status"] == "succeeded"

    packet = service.review_packet(wid)
    decisions = packet["review_decisions"]
    assert [d["kind"] for d in decisions] == ["changes_requested"]
    assert decisions[0]["actor"] == "Lead Reviewer"
    assert decisions[0]["run_id"] == first["run_id"]

    changes = packet["changes_since_previous"]
    assert changes is not None
    assert changes["previous_run_id"] == first["run_id"]
    assert changes["plan_version"] == {"before": 1, "now": 2}
    assert changes["patch_files"]["unchanged"] == ["workshop/inventory.py"]
    assert changes["patch_files"]["added"] == []
    assert changes["verdicts"]["visible"] == {"before": "pass", "now": "pass"}
    assert changes["verdicts"]["hidden"] == {"before": "pass", "now": "pass"}
    # The run history carries both attempts, oldest first.
    assert [h["run_id"] for h in packet["run_history"]] == [
        first["run_id"],
        second["run_id"],
    ]


# -- reviewer history ------------------------------------------------------------


def test_reviewer_history_folds_decisions_by_actor(service):
    first_wid, _ = _succeeded(service, "reserve-overcommit")
    service.decide_review(
        first_wid,
        actor="Lead Reviewer",
        decision="request_changes",
        reason="one more case",
    )
    service.draft_plan(first_wid, actor="tester")
    service.decide_plan(first_wid, approve=True, actor="Reviewer")
    service.start_run(first_wid, actor="tester")
    service.decide_review(first_wid, accept=True, actor="Second Reviewer")

    second_wid, _ = _succeeded(service, "tax-before-discount")
    service.decide_review(
        second_wid, accept=False, actor="Lead Reviewer", reason="wrong layer"
    )

    history = service.reviewer_history()
    by_actor = {r["actor"]: r for r in history["reviewers"]}
    lead = by_actor["Lead Reviewer"]
    assert lead["decisions"] == 2
    assert lead["changes_requested"] == 1
    assert lead["rejected"] == 1
    assert lead["accepted"] == 0
    assert lead["work_orders"] == [first_wid, second_wid]
    assert [h["kind"] for h in lead["history"]] == [
        "changes_requested",
        "rejected",
    ]
    # Both of the lead's decisions were timed against their runs'
    # landings, and the median is a real, non-negative number of
    # seconds (records carry second-resolution timestamps).
    assert lead["timed_decisions"] == 2
    assert lead["median_time_to_decision_seconds"] is not None
    assert lead["median_time_to_decision_seconds"] >= 0

    second = by_actor["Second Reviewer"]
    assert second["decisions"] == 1 and second["accepted"] == 1
    assert second["work_orders"] == [first_wid]


# -- the API surface -----------------------------------------------------------


def test_workbench_api(settings, service):
    app = create_app(settings)
    client = TestClient(app)
    svc = app.state.service
    wid, _run = _succeeded(svc)

    queue = client.get("/api/review-queue")
    assert queue.status_code == 200
    entries = queue.json()["entries"]
    assert [e["work_order_id"] for e in entries] == [wid]
    assert entries[0]["next_act"] == "review"

    packet = client.get(f"/api/work-orders/{wid}/review-packet")
    assert packet.status_code == 200
    assert packet.json()["verification"]["passed"] is True
    missing = client.get("/api/work-orders/wo-missing/review-packet")
    assert missing.status_code == 404

    decided = client.post(
        f"/api/work-orders/{wid}/review",
        json={
            "decision": "request_changes",
            "actor": "API Reviewer",
            "reason": "name the boundary in the approach",
        },
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "changes_requested"

    reviewers = client.get("/api/reviewers")
    assert reviewers.status_code == 200
    actors = {r["actor"]: r for r in reviewers.json()["reviewers"]}
    assert actors["API Reviewer"]["changes_requested"] == 1
