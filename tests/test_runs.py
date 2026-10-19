"""Runs end to end: the scripted repair, verification in code, the
review export — and a wrong repair failing verification."""

from __future__ import annotations

from pathlib import Path

import open_model_coding_agent.service as service_module
from open_model_coding_agent.model_port import OfflineScriptedModel


def test_gold_run_succeeds_and_review_exports_the_patch(service, approved_order):
    wid = approved_order["work_order_id"]
    run = service.start_run(wid)
    assert run["status"] == "succeeded"
    assert run["verification"]["passed"] is True
    assert run["changed_files"] == ["workshop/inventory.py"]
    assert "available" in run["patch"]
    assert service.get_work_order(wid)["status"] == "succeeded"

    final = service.decide_review(wid, accept=True, actor="Reviewer")
    assert final["status"] == "accepted"
    artifact = Path(service.settings.artifacts_dir) / f"{wid}.patch"
    assert artifact.is_file()
    assert artifact.read_text() == run["patch"]

    events = [a["event"] for a in service.audit(wid)]
    for expected in (
        "work_order_created",
        "plan_drafted",
        "plan_approved",
        "run_started",
        "tool_call",
        "verification_completed",
        "run_succeeded",
        "review_accepted",
    ):
        assert expected in events


def test_review_rejection_records_the_reason(service, approved_order):
    wid = approved_order["work_order_id"]
    service.start_run(wid)
    final = service.decide_review(
        wid, accept=False, actor="Reviewer", reason="fix the naming too"
    )
    assert final["status"] == "rejected"
    audit = service.audit(wid)
    rejection = [a for a in audit if a["event"] == "review_rejected"]
    assert rejection and rejection[0]["detail"]["reason"] == "fix the naming too"


def test_a_wrong_repair_fails_verification(service, approved_order, monkeypatch):
    """The agent's own say-so is not the verdict: a plausible edit
    that does not fix the defect lands FAILED, with grading named."""
    wid = approved_order["work_order_id"]
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
    run = service.start_run(wid)
    assert run["status"] == "failed"
    assert "grading tests fail" in run["failure_reason"]
    assert service.get_work_order(wid)["status"] == "failed"


def test_second_gold_task_runs_clean(service):
    order = service.create_gold_work_order("low-stock-report")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    assert run["status"] == "succeeded"
    assert run["changed_files"] == ["workshop/inventory.py"]
