"""The plan gate and the review gate: nothing runs unapproved, and
only a verified run can be accepted."""

from __future__ import annotations

import pytest

from open_model_coding_agent.errors import GateError


def test_execution_refused_before_plan(service, gold_order):
    wid = gold_order["work_order_id"]
    with pytest.raises(GateError) as exc:
        service.start_run(wid)
    assert exc.value.code == "plan_not_approved"


def test_execution_refused_with_undecided_plan(service, gold_order):
    wid = gold_order["work_order_id"]
    service.draft_plan(wid)
    assert service.get_work_order(wid)["status"] == "plan_ready"
    with pytest.raises(GateError) as exc:
        service.start_run(wid)
    assert exc.value.code == "plan_not_approved"


def test_rejected_plan_blocks_execution_and_can_be_redrafted(service, gold_order):
    wid = gold_order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=False, actor="Reviewer", note="too broad")
    assert service.get_work_order(wid)["status"] == "plan_rejected"
    with pytest.raises(GateError):
        service.start_run(wid)
    plan = service.draft_plan(wid)
    assert plan["status"] == "draft"
    assert service.get_work_order(wid)["status"] == "plan_ready"


def test_review_refused_before_a_verified_run(service, approved_order):
    with pytest.raises(GateError) as exc:
        service.decide_review(approved_order["work_order_id"], accept=True, actor="R")
    assert exc.value.code == "no_verified_run_awaiting_review"


def test_unknown_work_order_is_a_named_refusal(service):
    with pytest.raises(GateError) as exc:
        service.draft_plan("wo-nope")
    assert exc.value.code == "unknown_work_order"
