"""Round 14 — batch work orders.

A batch is a named submission of work-order specifications,
validated whole before any of it exists, whose items become
ordinary work orders through ordinary intake. The driver
advances items one at a time through the person-free acts and
stops at every gate a person owns; the batch budget folds from
the items' usage records and stops unstarted items at batch
level; cancellation marks only unstarted items. Nothing here
approves anything in bulk — there is no such act to test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from open_model_coding_agent.api import create_app
from open_model_coding_agent.errors import GateError
from open_model_coding_agent.models import (
    BatchIn,
    BatchItemIn,
    PlanStepIn,
    TemplateIn,
)

GUARD_PARAMS = {
    "module": "inventory",
    "behaviour": (
        "a reservation can commit more units than are available, "
        "so available stock goes negative"
    ),
}


def gold(task_id: str) -> BatchItemIn:
    return BatchItemIn(kind="gold", gold_task_id=task_id)


def submit(service, items, *, name="Friday fixes", actor="Submitter", budget=None):
    return service.create_batch(
        BatchIn(name=name, actor=actor, items=items, budget=budget)
    )


# -- creation: provenance through ordinary intake ------------------------------


def test_batch_creation_makes_ordinary_orders_with_provenance(service, tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    report = submit(
        service,
        [
            gold("reserve-overcommit"),
            BatchItemIn(
                kind="template",
                template_name="guard-clause-fix",
                parameters=GUARD_PARAMS,
                gold_task_id="reserve-overcommit",
            ),
            BatchItemIn(
                kind="adhoc",
                title="Make add add",
                task_text="The add function subtracts. Make it add.",
                file_scope=["pkg/calc.py"],
                target_path=str(repo),
            ),
        ],
    )
    batch = report["batch"]
    assert report["status"] == "open"
    assert [item["kind"] for item in batch["items"]] == [
        "gold",
        "template",
        "adhoc",
    ]
    assert all(
        item["item_state"] == "pending" for item in batch["items"]
    )
    orders = [
        service.get_work_order(item["work_order_id"])
        for item in batch["items"]
    ]
    for index, order in enumerate(orders):
        assert order["batch"] == {
            "batch_id": batch["batch_id"],
            "name": "Friday fixes",
            "index": index,
        }
        # Ordinary orders, standing where ordinary intake leaves
        # them: draft, no plan, nothing run, nothing approved.
        assert order["status"] == "draft"
        assert service.store.get_plan(order["work_order_id"]) is None
    assert orders[1]["template"]["name"] == "guard-clause-fix"
    assert orders[2]["template"] is None

    # The creation audit names the act, and the report folds the
    # submission honestly: three items, all pending, no spend.
    events = [a["event"] for a in service.batch_audit(batch["batch_id"])]
    assert events == ["batch_created"]
    assert report["counts"] == {
        "items": 3,
        "by_batch_state": {"pending": 3},
        "by_order_status": {"draft": 3},
    }
    assert report["spend"]["model_calls"] == 0
    assert report["cost"]["successful_tasks"] == 0
    assert report["awaiting_person"] == []


# -- creation: the whole batch refuses on the first invalid item -------------


def _assert_nothing_created(service):
    assert service.list_work_orders() == []
    assert service.store.list_batches() == []


def test_batch_with_an_unknown_gold_task_creates_nothing(service):
    with pytest.raises(GateError) as exc:
        submit(
            service,
            [gold("reserve-overcommit"), gold("no-such-task")],
        )
    assert exc.value.code == "unknown_gold_task"
    assert "item 1" in exc.value.message
    _assert_nothing_created(service)


def test_batch_with_an_unpublished_template_creates_nothing(service):
    service.create_template(
        TemplateIn(
            name="batch-draft-shape",
            description="An unpublished shape, for the batch refusal.",
            task_text="Fix {module}: {behaviour}.",
            parameters=["module", "behaviour"],
            scope_pattern=["workshop/{module}.py"],
            steps=[
                PlanStepIn(
                    intent="Fix it in workshop/{module}.py",
                    files=["workshop/{module}.py"],
                )
            ],
            author="Batch Author",
        )
    )
    with pytest.raises(GateError) as exc:
        submit(
            service,
            [
                BatchItemIn(
                    kind="template",
                    template_name="batch-draft-shape",
                    parameters=GUARD_PARAMS,
                )
            ],
        )
    assert exc.value.code == "template_not_published"
    assert "item 0" in exc.value.message
    _assert_nothing_created(service)


def test_batch_with_a_traversal_parameter_creates_nothing(service):
    with pytest.raises(GateError) as exc:
        submit(
            service,
            [
                BatchItemIn(
                    kind="template",
                    template_name="guard-clause-fix",
                    parameters={
                        **GUARD_PARAMS,
                        "module": "../inventory",
                    },
                )
            ],
        )
    assert exc.value.code == "template_parameter_invalid"
    _assert_nothing_created(service)


def test_batch_shape_and_budget_refusals(service):
    cases = [
        (BatchIn(name="x", actor="a", items=[]), "batch_empty"),
        (
            BatchIn(
                name="x",
                actor="a",
                items=[BatchItemIn(kind="sideways")],
            ),
            "invalid_batch_item",
        ),
        (
            BatchIn(
                name="x",
                actor="a",
                items=[BatchItemIn(kind="adhoc", title="t")],
            ),
            "invalid_batch_item",
        ),
        (
            BatchIn(
                name="x",
                actor="a",
                items=[gold("reserve-overcommit")],
                budget={"max_steps": 3},
            ),
            "invalid_batch_budget",
        ),
        (
            BatchIn(
                name="x",
                actor="a",
                items=[gold("reserve-overcommit")],
                budget={"max_tokens": 0},
            ),
            "invalid_batch_budget",
        ),
        (
            BatchIn(name="  ", actor="a", items=[gold("reserve-overcommit")]),
            "invalid_batch",
        ),
    ]
    for body, code in cases:
        with pytest.raises(GateError) as exc:
            service.create_batch(body)
        assert exc.value.code == code, (code, exc.value.code)
    _assert_nothing_created(service)


# -- the driver: gates stop it, approvals outside it move it -------------------


def _two_gold_batch(service):
    report = submit(
        service, [gold("reserve-overcommit"), gold("low-stock-report")]
    )
    batch = report["batch"]
    wids = [item["work_order_id"] for item in batch["items"]]
    return batch, wids


def test_driver_stops_at_the_plan_gate_and_resumes_after_approval(service):
    batch, wids = _two_gold_batch(service)
    drive = service.drive_batch(batch["batch_id"], actor="Driver")
    assert drive["stopped_reason"] is None
    assert [e["action"] for e in drive["entries"]] == [
        "drafted_plan",
        "drafted_plan",
    ]
    assert all(e["stopped_at"] == "plan_gate" for e in drive["entries"])
    assert all(e["next_act"] == "decide_plan" for e in drive["entries"])
    # The driver drafted and stopped: nothing ran, nothing was
    # approved — approval is a person's act, taken outside the
    # batch, one order at a time.
    for wid in wids:
        assert service.get_work_order(wid)["status"] == "plan_ready"
        assert service.store.list_runs(wid) == []
    stored = service.store.get_batch(batch["batch_id"])
    assert stored["driver"] is None
    assert stored["last_drive"]["actor"] == "Driver"

    # The queue names the batch each waiting entry belongs to.
    queue = service.review_queue()
    assert len(queue["entries"]) == 2
    for entry in queue["entries"]:
        assert entry["batch"]["batch_id"] == batch["batch_id"]
        assert entry["batch"]["name"] == "Friday fixes"

    for wid in wids:
        service.decide_plan(wid, approve=True, actor="Reviewer")
    drive = service.drive_batch(batch["batch_id"], actor="Driver")
    assert [e["action"] for e in drive["entries"]] == ["ran", "ran"]
    assert all(e["next_act"] == "review" for e in drive["entries"])
    for wid in wids:
        assert service.get_work_order(wid)["status"] == "succeeded"
        # The packet a reviewer reads names the batch too.
        packet = service.review_packet(wid)
        assert packet["batch"]["batch_id"] == batch["batch_id"]

    report = service.get_batch(batch["batch_id"])
    assert report["status"] == "open"
    assert report["runs"]["succeeded"] == 2
    assert report["runs"]["failures_by_class"] == {}
    assert report["challenges_outstanding"] == {
        "open_must_answer": 0,
        "open_advisory": 0,
    }
    assert report["cost"]["successful_tasks"] == 2
    # Offline usage is scripted characters: counted, never priced,
    # so the fold's money is an honest zero over the two successes
    # — the suite cost report's own convention.
    assert report["cost"]["priced_cost"] == "0"
    assert Decimal(report["cost"]["cost_per_successful_task"]) == 0
    assert report["cost"]["scripted_calls"] > 0
    assert report["spend"]["model_calls"] > 0
    assert {a["work_order_id"] for a in report["awaiting_person"]} == set(
        wids
    )
    assert all(
        a["category"] == "awaiting_review"
        for a in report["awaiting_person"]
    )


# -- the batch budget: a fold that stops unstarted items -----------------------


def test_batch_budget_marks_unstarted_items_and_refuses_by_name(service):
    report = submit(
        service,
        [
            gold("reserve-overcommit"),
            gold("low-stock-report"),
            gold("tax-before-discount"),
        ],
        budget={"max_model_calls": 1},
    )
    batch = report["batch"]
    wids = [item["work_order_id"] for item in batch["items"]]
    # One drafting call is the whole budget. The first item
    # drafts; the fold then stands at the ceiling, and the pass
    # refuses to start anything further, by name.
    with pytest.raises(GateError) as exc:
        service.drive_batch(batch["batch_id"], actor="Driver")
    assert exc.value.code == "batch_budget_exhausted"

    stored = service.store.get_batch(batch["batch_id"])
    assert stored["budget_stop"]["ceiling"] == "max_model_calls"
    assert stored["budget_stop"]["items"] == [1, 2]
    assert [i["item_state"] for i in stored["items"]] == [
        "pending",
        "budget_stopped",
        "budget_stopped",
    ]
    # The mark is the batch's; the orders keep their true state:
    # drafted for the first, untouched draft for the stopped.
    assert service.get_work_order(wids[0])["status"] == "plan_ready"
    for wid in wids[1:]:
        assert service.get_work_order(wid)["status"] == "draft"
        assert service.store.get_plan(wid) is None
    # The completed pass cleared its driver mark and recorded
    # where it stopped, refusal and all.
    assert stored["driver"] is None
    assert stored["last_drive"]["stopped_reason"] == "batch_budget_exhausted"

    report = service.get_batch(batch["batch_id"])
    assert report["spend"]["model_calls"] == 1
    assert report["spend"]["budget_stop"]["ceiling"] == "max_model_calls"
    states = {i["index"]: i["batch_state"] for i in report["items"]}
    assert states == {0: "started", 1: "budget_stopped", 2: "budget_stopped"}
    events = [a["event"] for a in service.batch_audit(batch["batch_id"])]
    assert "batch_item_budget_stopped" in events
    assert "batch_budget_stopped" in events

    # Driving again starts nothing new: the stopped items are
    # skipped on their marks, and no further refusal fires.
    drive = service.drive_batch(batch["batch_id"], actor="Driver")
    assert [e["action"] for e in drive["entries"]] == [
        "waiting",
        "skipped",
        "skipped",
    ]


# -- the driver mark: one active pass, and releasing a dead one ----------------


def test_a_second_driver_refuses_while_a_pass_is_recorded_active(
    service, monkeypatch
):
    report = submit(service, [gold("reserve-overcommit")])
    batch_id = report["batch"]["batch_id"]

    def die(work_order_id, actor="console"):
        raise RuntimeError("the pass died here")

    monkeypatch.setattr(service, "draft_plan", die)
    with pytest.raises(RuntimeError):
        service.drive_batch(batch_id, actor="Driver")
    stored = service.store.get_batch(batch_id)
    # The dead pass's mark stands — the record does not pretend
    # it finished.
    assert stored["driver"]["actor"] == "Driver"
    with pytest.raises(GateError) as exc:
        service.drive_batch(batch_id, actor="Driver")
    assert exc.value.code == "batch_driver_active"

    # Releasing is a recorded act; without a mark it refuses.
    report = service.release_batch_driver(
        batch_id, actor="Operator", reason="the driving process was killed"
    )
    assert report["batch"]["driver"] is None
    events = [
        (a["event"], a["detail"])
        for a in service.batch_audit(batch_id)
    ]
    released = [d for e, d in events if e == "batch_driver_released"]
    assert released[0]["released_actor"] == "Driver"
    with pytest.raises(GateError) as exc:
        service.release_batch_driver(
            batch_id, actor="Operator", reason="nothing stands"
        )
    assert exc.value.code == "batch_driver_not_active"

    monkeypatch.undo()
    drive = service.drive_batch(batch_id, actor="Driver")
    assert drive["entries"][0]["action"] == "drafted_plan"


# -- cancellation: unstarted items only, orders never deleted ------------------


def test_cancel_marks_only_unstarted_items(service):
    report = submit(
        service,
        [
            gold("reserve-overcommit"),
            gold("low-stock-report"),
            gold("tax-before-discount"),
        ],
    )
    batch_id = report["batch"]["batch_id"]
    wids = [i["work_order_id"] for i in report["batch"]["items"]]

    with pytest.raises(GateError) as exc:
        service.cancel_batch(batch_id, actor="Anyone", reason=" ")
    assert exc.value.code == "batch_cancel_reason_required"

    # Cancel before anything starts: every item is marked, the
    # orders stay readable in their true state, and the batch
    # drives nothing ever again. Any named actor may cancel —
    # cancellation approves nothing.
    report = service.cancel_batch(
        batch_id, actor="Someone Else", reason="the sprint was re-planned"
    )
    assert report["status"] == "cancelled"
    assert [i["batch_state"] for i in report["items"]] == ["cancelled"] * 3
    for wid in wids:
        order = service.get_work_order(wid)
        assert order is not None and order["status"] == "draft"
    with pytest.raises(GateError) as exc:
        service.drive_batch(batch_id, actor="Driver")
    assert exc.value.code == "batch_cancelled"
    with pytest.raises(GateError) as exc:
        service.cancel_batch(batch_id, actor="Anyone", reason="again")
    assert exc.value.code == "batch_already_cancelled"


def test_cancel_leaves_started_work_untouched(service):
    report = submit(
        service, [gold("reserve-overcommit"), gold("low-stock-report")]
    )
    batch_id = report["batch"]["batch_id"]
    wids = [i["work_order_id"] for i in report["batch"]["items"]]
    service.drive_batch(batch_id, actor="Driver")  # both drafted: started
    report = service.cancel_batch(
        batch_id, actor="Submitter", reason="priorities changed"
    )
    assert report["batch"]["status"] == "cancelled"
    # Both items had begun (a plan stands on each), so neither is
    # marked — the batch's cancellation reaches nothing that has
    # started, and the orders stand exactly where their own
    # gates left them.
    assert [i["batch_state"] for i in report["items"]] == [
        "started",
        "started",
    ]
    for wid in wids:
        assert service.get_work_order(wid)["status"] == "plan_ready"


# -- the report: completion is derived, never stored ----------------------------


def test_batch_report_completion_is_derived(service):
    report = submit(service, [gold("reserve-overcommit")])
    batch_id = report["batch"]["batch_id"]
    wid = report["batch"]["items"][0]["work_order_id"]
    service.drive_batch(batch_id, actor="Driver")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    service.drive_batch(batch_id, actor="Driver")
    report = service.get_batch(batch_id)
    assert report["status"] == "open"  # a review is still owed
    service.decide_review(wid, accept=True, actor="Reviewer")
    report = service.get_batch(batch_id)
    assert report["status"] == "complete"
    assert report["awaiting_person"] == []
    assert report["counts"]["by_order_status"] == {"accepted": 1}


# -- the API surface -------------------------------------------------------------


def test_batches_api_round_trip(settings):
    client = TestClient(create_app(settings))
    created = client.post(
        "/api/batches",
        json={
            "name": "API batch",
            "actor": "Submitter",
            "items": [{"kind": "gold", "gold_task_id": "reserve-overcommit"}],
            "budget": {"max_model_calls": 50},
        },
    )
    assert created.status_code == 201
    report = created.json()
    batch_id = report["batch"]["batch_id"]
    assert report["batch"]["budget"]["max_model_calls"] == 50
    assert report["batch"]["budget"]["max_tokens"] is None

    bad = client.post(
        "/api/batches",
        json={
            "name": "bad",
            "actor": "Submitter",
            "items": [{"kind": "gold", "gold_task_id": "nope"}],
        },
    )
    assert bad.status_code == 409
    assert bad.json()["code"] == "unknown_gold_task"

    listing = client.get("/api/batches")
    assert [r["batch"]["batch_id"] for r in listing.json()] == [batch_id]
    drive = client.post(
        f"/api/batches/{batch_id}/drive", json={"actor": "Driver"}
    )
    assert drive.status_code == 200
    assert drive.json()["entries"][0]["action"] == "drafted_plan"
    cancelled = client.post(
        f"/api/batches/{batch_id}/cancel",
        json={"actor": "Submitter", "reason": "done testing"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    missing = client.get("/api/batches/bat-nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "unknown_batch"
