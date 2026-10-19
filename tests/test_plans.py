"""Round 3: plan depth — steps as the plan's structure, scope
fingerprints and staleness, and amendments as new versions.

Everything runs on the offline model against gold work orders; the
target repo a gold order materialises is a real directory on disk,
so drift is a real edit to a real file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from open_model_coding_agent.api import create_app
from open_model_coding_agent.errors import GateError
from open_model_coding_agent.model_port import (
    PlanDraft,
    PlanStepDraft,
    validate_plan_draft,
)


def _draft(*steps, approach="Do the work in steps."):
    return PlanDraft(approach=approach, steps=list(steps))


def _step(intent, files, tests=()):
    return PlanStepDraft(intent=intent, files=list(files), tests=list(tests))


# -- plan structure ------------------------------------------------------


def test_a_gold_plan_is_a_step_list(service, gold_order):
    wid = gold_order["work_order_id"]
    plan = service.draft_plan(wid)
    assert plan["version"] == 1
    assert len(plan["steps"]) == 1
    step = plan["steps"][0]
    assert step["intent"]
    assert step["files"] == ["workshop/inventory.py"]
    assert step["tests"] == ["grading/test_grading_inventory.py"]
    # files/tests on the plan are the derived unions over the steps.
    assert plan["files"] == ["workshop/inventory.py"]
    assert plan["tests"] == ["grading/test_grading_inventory.py"]


def test_validator_refuses_steps_without_intent_or_files():
    scope = ["workshop/inventory.py"]
    with pytest.raises(GateError) as exc:
        validate_plan_draft(_draft(_step("  ", scope)), file_scope=scope)
    assert exc.value.code == "malformed_plan"
    with pytest.raises(GateError) as exc:
        validate_plan_draft(_draft(_step("Work.", [])), file_scope=scope)
    assert exc.value.code == "malformed_plan"
    with pytest.raises(GateError) as exc:
        validate_plan_draft(_draft(), file_scope=scope)
    assert exc.value.code == "malformed_plan"


def test_validator_refuses_a_step_file_outside_the_scope():
    with pytest.raises(GateError) as exc:
        validate_plan_draft(
            _draft(_step("Rewrite orders.", ["workshop/orders.py"])),
            file_scope=["workshop/inventory.py"],
        )
    assert exc.value.code == "malformed_plan"
    assert "outside the approved file scope" in exc.value.message


def test_validator_requires_step_tests_to_resolve():
    draft = _draft(
        _step("Fix it.", ["workshop/inventory.py"], ["tests/test_missing.py"])
    )
    with pytest.raises(GateError) as exc:
        validate_plan_draft(
            draft,
            file_scope=["workshop/inventory.py"],
            repo_files=["workshop/inventory.py", "workshop/tests/test_inventory.py"],
            grading_tests=["grading/test_grading_inventory.py"],
        )
    assert exc.value.code == "malformed_plan"
    assert "resolve to nothing" in exc.value.message
    # The same draft passes when its tests resolve — by repo path,
    # by grading path, and by bare file name.
    for named in (
        "workshop/tests/test_inventory.py",
        "grading/test_grading_inventory.py",
        "test_inventory.py",
    ):
        ok = _draft(_step("Fix it.", ["workshop/inventory.py"], [named]))
        validate_plan_draft(
            ok,
            file_scope=["workshop/inventory.py"],
            repo_files=["workshop/inventory.py", "workshop/tests/test_inventory.py"],
            grading_tests=["grading/test_grading_inventory.py"],
        )


def test_a_plan_can_be_redrafted_after_rejection_as_a_new_version(
    service, gold_order
):
    wid = gold_order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=False, actor="Reviewer")
    plan = service.draft_plan(wid)
    assert plan["version"] == 2
    versions = [p["version"] for p in service.store.list_plans(wid)]
    assert versions == [1, 2]


# -- the multi-file gold task ----------------------------------------------


def test_the_coupon_task_runs_end_to_end_across_two_files(service):
    order = service.create_gold_work_order("order-coupon")
    wid = order["work_order_id"]
    plan = service.draft_plan(wid)
    assert [s["files"] for s in plan["steps"]] == [
        ["workshop/pricing.py"],
        ["workshop/orders.py"],
    ]
    assert plan["files"] == ["workshop/pricing.py", "workshop/orders.py"]
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    assert run["status"] == "succeeded"
    assert run["changed_files"] == ["workshop/orders.py", "workshop/pricing.py"]
    final = service.decide_review(wid, accept=True, actor="Reviewer")
    assert final["status"] == "accepted"


# -- fingerprints and staleness ----------------------------------------------


def test_approval_records_the_scope_fingerprint(service, approved_order):
    wid = approved_order["work_order_id"]
    plan = service.store.get_plan(wid)
    assert plan["scope_fingerprint"] and len(plan["scope_fingerprint"]) == 64
    assert plan["scope_digests"]["workshop/inventory.py"]
    detail = service.work_order_detail(wid)
    assert detail["plan"]["fingerprint_state"] == "current"


def _drift(order, rel, text="\n# touched by someone else\n", *, delete=False):
    path = Path(order["target_path"]) / rel
    if delete:
        path.unlink()
    else:
        path.write_text(path.read_text() + text)


def test_an_in_scope_edit_stales_the_plan(service, approved_order):
    wid = approved_order["work_order_id"]
    _drift(approved_order, "workshop/inventory.py")
    detail = service.work_order_detail(wid)
    assert detail["plan"]["fingerprint_state"] == "stale"
    with pytest.raises(GateError) as exc:
        service.start_run(wid)
    assert exc.value.code == "stale_plan"
    # The refusal is recorded, names the drifted file, and the order
    # is back where a fresh plan can be drafted.
    refusal = [a for a in service.audit(wid) if a["event"] == "stale_plan_refused"]
    assert refusal and refusal[0]["detail"]["drifted_files"] == [
        "workshop/inventory.py"
    ]
    assert service.get_work_order(wid)["status"] == "draft"
    # A fresh plan against the moved repo approves and runs.
    plan = service.draft_plan(wid)
    assert plan["version"] == 2
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    assert run["status"] == "succeeded"


def test_a_deleted_in_scope_file_stales_the_plan(service, approved_order):
    wid = approved_order["work_order_id"]
    _drift(approved_order, "workshop/inventory.py", delete=True)
    with pytest.raises(GateError) as exc:
        service.start_run(wid)
    assert exc.value.code == "stale_plan"


def test_an_out_of_scope_edit_does_not_stale_the_plan(service, approved_order):
    wid = approved_order["work_order_id"]
    _drift(approved_order, "workshop/orders.py")  # not in this order's scope
    run = service.start_run(wid)
    assert run["status"] == "succeeded"


# -- amendments ----------------------------------------------------------------


def _amendment_for(wid_task_steps):
    return PlanDraft(
        approach="The same repair, planned in two steps.",
        steps=wid_task_steps,
    )


def test_amendment_binds_its_own_approval(service):
    order = service.create_gold_work_order("cancel-leaks-reservations")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")

    grading = "grading/test_grading_orders.py"
    amended = service.amend_plan(
        wid,
        draft=_amendment_for(
            [
                _step(
                    "Read Order.cancel and the ledger's release path first.",
                    ["workshop/orders.py"],
                    [grading],
                ),
                _step(
                    "Release each reserved line through the ledger when a "
                    "placed order is cancelled.",
                    ["workshop/orders.py"],
                    [grading],
                ),
            ]
        ),
        actor="Lead Reviewer",
    )
    assert amended["version"] == 2
    assert amended["amended_by"] == "Lead Reviewer"
    diff = amended["diff_from_previous"]
    assert diff["from_version"] == 1 and diff["to_version"] == 2
    assert len(diff["steps_added"]) == 1 and not diff["files_added"]
    assert amended["scope_fingerprint"] is None
    assert service.get_work_order(wid)["status"] == "plan_ready"
    events = [a["event"] for a in service.audit(wid)]
    assert "plan_amended" in events

    # The v1 approval does not carry: the latest version is undecided,
    # and naming the superseded version is its own refusal.
    with pytest.raises(GateError) as exc:
        service.start_run(wid)
    assert exc.value.code == "plan_not_approved"
    with pytest.raises(GateError) as exc:
        service.start_run(wid, plan_version=1)
    assert exc.value.code == "plan_superseded"
    with pytest.raises(GateError) as exc:
        service.start_run(wid, plan_version=99)
    assert exc.value.code == "unknown_plan_version"

    # The amendment's own approval binds v2, fingerprint and all.
    decided = service.decide_plan(wid, approve=True, actor="Reviewer")
    assert decided["version"] == 2 and decided["scope_fingerprint"]
    run = service.start_run(wid)
    assert run["status"] == "succeeded"
    assert run["plan_version"] == 2


def test_amendment_cannot_widen_past_the_work_orders_scope(
    service, approved_order
):
    wid = approved_order["work_order_id"]  # scope: workshop/inventory.py
    with pytest.raises(GateError) as exc:
        service.amend_plan(
            wid,
            draft=_amendment_for(
                [
                    _step(
                        "Also rewrite the orders module while we are here.",
                        ["workshop/orders.py"],
                    )
                ]
            ),
            actor="Lead Reviewer",
        )
    assert exc.value.code == "scope_widening_refused"
    # Nothing was written: v1 is still the latest plan, still approved.
    plan = service.store.get_plan(wid)
    assert plan["version"] == 1 and plan["status"] == "approved"
    assert service.get_work_order(wid)["status"] == "approved"


def test_amendment_may_narrow_the_scope(service):
    order = service.create_gold_work_order("order-coupon")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    amended = service.amend_plan(
        wid,
        draft=_amendment_for(
            [
                _step(
                    "Price the coupon in pricing only, for now.",
                    ["workshop/pricing.py"],
                    ["grading/test_grading_coupon.py"],
                )
            ]
        ),
        actor="Lead Reviewer",
    )
    assert amended["version"] == 2
    assert amended["files"] == ["workshop/pricing.py"]
    assert amended["diff_from_previous"]["files_removed"] == ["workshop/orders.py"]


def test_only_an_approved_plan_can_be_amended(service, gold_order):
    wid = gold_order["work_order_id"]
    service.draft_plan(wid)  # plan_ready, not approved
    with pytest.raises(GateError) as exc:
        service.amend_plan(
            wid,
            draft=_amendment_for(
                [_step("Change of plan.", ["workshop/inventory.py"])]
            ),
            actor="Lead Reviewer",
        )
    assert exc.value.code == "plan_not_amendable"


def test_naming_the_latest_version_runs_it(service, approved_order):
    run = service.start_run(approved_order["work_order_id"], plan_version=1)
    assert run["status"] == "succeeded"
    assert run["plan_version"] == 1


# -- the API surface -----------------------------------------------------------


def test_api_amend_and_pinned_run(settings):
    client = TestClient(create_app(settings))
    created = client.post("/api/work-orders/from-gold/reserve-overcommit")
    wid = created.json()["work_order_id"]
    client.post(f"/api/work-orders/{wid}/plan")
    client.post(
        f"/api/work-orders/{wid}/plan/decide",
        json={"approve": True, "actor": "Reviewer"},
    )
    amended = client.post(
        f"/api/work-orders/{wid}/plan/amend",
        json={
            "approach": "The same repair, in two steps.",
            "steps": [
                {
                    "intent": "Read StockLedger.reserve first.",
                    "files": ["workshop/inventory.py"],
                    "tests": ["grading/test_grading_inventory.py"],
                },
                {
                    "intent": "Add the availability check to reserve.",
                    "files": ["workshop/inventory.py"],
                    "tests": ["grading/test_grading_inventory.py"],
                },
            ],
            "actor": "Lead Reviewer",
        },
    )
    assert amended.status_code == 200
    assert amended.json()["version"] == 2
    assert amended.json()["diff_from_previous"]["to_version"] == 2

    detail = client.get(f"/api/work-orders/{wid}").json()
    assert [p["version"] for p in detail["plans"]] == [1, 2]
    assert detail["plans"][0]["fingerprint_state"] == "current"

    pinned = client.post(
        f"/api/work-orders/{wid}/runs", json={"plan_version": 1}
    )
    assert pinned.status_code == 409
    assert pinned.json()["code"] == "plan_superseded"
