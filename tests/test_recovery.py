"""Round 5: failure recovery.

Checkpoints are recorded as a run works; a run that dies mid-flight
lands INTERRUPTED with the last checkpoint named; resume is decided
in code against the basis the run was interrupted on (plan, target,
workspace — each refusal named); restart discards the workspace and
runs fresh under the same approval; abandon is a recorded act that
names the partial patch, and a partial patch can never be reviewed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import open_model_coding_agent.service as service_module
from open_model_coding_agent.api import create_app
from open_model_coding_agent.errors import GateError
from open_model_coding_agent.model_port import OfflineScriptedModel, Step
from open_model_coding_agent.models import Run
from open_model_coding_agent.plans import file_digests
from open_model_coding_agent.service import CodingService
from open_model_coding_agent.store import utcnow


class DiesAfterNCalls(OfflineScriptedModel):
    """The scripted model, until its Nth next_step call — which
    raises the way a dying process does, after real work has landed.
    The dying call consumes nothing."""

    def __init__(self, scripts=None, *, die_on: int):
        super().__init__(scripts=scripts)
        self._calls = 0
        self._die_on = die_on

    def next_step(self, *, transcript):
        self._calls += 1
        if self._calls >= self._die_on:
            raise RuntimeError("the process died mid-run")
        return super().next_step(transcript=transcript)


class FinishesImmediately(OfflineScriptedModel):
    def start_run(self, *, work_order, plan):
        self._queue = [Step("finish", {})]


@pytest.fixture()
def model_factory(monkeypatch):
    """Swap the model the service builds. The installed callable
    receives the scripts dict, exactly as the real factory's
    product would be handed them."""

    state = {"make": None}

    def install(make):
        state["make"] = make
        monkeypatch.setattr(
            service_module,
            "make_model",
            lambda profile_id, scripts=None, **_kw: state["make"](scripts),
        )

    return install


def _healthy(scripts):
    return OfflineScriptedModel(scripts=scripts)


def _interrupted(service, approved_order, model_factory, *, die_on=3):
    """Run the reserve-overcommit order with a model that dies
    after the read and the landed edit, then restore a healthy one.
    The queue for this task is read -> edit -> finish, so the third
    call is the one that dies."""
    wid = approved_order["work_order_id"]
    model_factory(lambda scripts: DiesAfterNCalls(scripts, die_on=die_on))
    run = service.start_run(wid)
    assert run["status"] == "interrupted"
    model_factory(_healthy)
    return wid, run


# -- checkpoints --------------------------------------------------------


def test_checkpoints_follow_landed_edits_and_completed_steps(service):
    order = service.create_gold_work_order("order-coupon")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    assert run["status"] == "succeeded"

    checkpoints = run["checkpoints"]
    edits = [c for c in checkpoints if c["kind"] == "edit"]
    steps = [c for c in checkpoints if c["kind"] == "step"]
    # Four scripted edits land (two per file); each completes
    # nothing until its file's step is fully covered — the pricing
    # edits complete step 0, the orders edits complete step 1.
    assert len(edits) == 4
    assert [c["step_index"] for c in steps] == [0, 1]
    assert [c["index"] for c in checkpoints] == list(range(len(checkpoints)))

    last = checkpoints[-1]
    assert run["last_checkpoint"] == last["checkpoint_id"]
    assert last["edited_files"] == ["workshop/pricing.py", "workshop/orders.py"]
    assert last["digests"] == file_digests(
        Path(run["workspace"]),
        ["workshop/pricing.py", "workshop/orders.py"],
    )
    assert last["tool_calls_so_far"] == 6  # two reads + four edits
    assert last["usage_totals"]["calls"] >= 4


# -- interruption ---------------------------------------------------------


def test_a_run_that_dies_mid_flight_lands_interrupted(
    service, approved_order, model_factory
):
    wid, run = _interrupted(service, approved_order, model_factory)
    assert "RuntimeError" in run["interruption_reason"]
    assert "died mid-flight" in run["interruption_reason"]
    assert run["finished_at"] is None
    assert run["last_checkpoint"] is not None
    assert run["failure_reason"] is None
    assert service.get_work_order(wid)["status"] == "interrupted"
    events = [a["event"] for a in service.audit(wid)]
    assert "run_interrupted" in events
    assert "run_failed" not in events
    detail = service.work_order_detail(wid)
    assert detail["runs"][0]["status"] == "interrupted"
    assert detail["runs"][0]["checkpoints"]


def test_startup_reconciliation_marks_orphaned_runs_interrupted(
    service, settings, approved_order
):
    wid = approved_order["work_order_id"]
    # A run whose driver died before recording an outcome: the
    # record stands at 'running', exactly as the crash left it.
    orphan = Run(
        run_id="run-orphaned",
        work_order_id=wid,
        plan_version=1,
        model_profile="offline",
        workspace="",
        created_at=utcnow(),
    )
    service.store.put_run(orphan.model_dump())
    order = service.get_work_order(wid)
    order["status"] = "running"
    service.store.put_work_order(order)

    rebooted = CodingService(settings)
    run = rebooted.store.get_run("run-orphaned")
    assert run["status"] == "interrupted"
    assert run["interruption_reason"] == "process_lost"
    assert rebooted.get_work_order(wid)["status"] == "interrupted"

    # Idempotent: a second boot finds nothing standing at 'running'
    # and records nothing new.
    again = CodingService(settings)
    interruptions = [
        a
        for a in again.audit(wid)
        if a["event"] == "run_interrupted"
        and a["detail"].get("run_id") == "run-orphaned"
    ]
    assert len(interruptions) == 1


# -- resume -----------------------------------------------------------------


def test_resume_completes_without_repeating_work(
    service, approved_order, model_factory
):
    wid, run = _interrupted(service, approved_order, model_factory)
    resumed = service.resume_run(wid, run["run_id"], actor="tester")
    assert resumed["run_id"] == run["run_id"]
    assert resumed["status"] == "succeeded"
    assert resumed["verification"]["passed"] is True
    assert service.get_work_order(wid)["status"] == "succeeded"

    calls = [
        a["detail"]
        for a in service.audit(wid)
        if a["event"] == "tool_call" and a["detail"]["run_id"] == run["run_id"]
    ]
    # The read and the edit happened once, in the first attempt.
    # The resume added no tool calls at all before finishing: the
    # landed edit was not re-applied and the completed step was not
    # re-executed.
    assert [c["tool"] for c in calls] == ["read_file", "edit_file"]
    assert any(a["event"] == "run_resumed" for a in service.audit(wid))

    final = service.decide_review(wid, accept=True, actor="Reviewer")
    assert final["status"] == "accepted"


def test_resume_refuses_a_run_that_is_not_interrupted(service, approved_order):
    wid = approved_order["work_order_id"]
    run = service.start_run(wid)
    assert run["status"] == "succeeded"
    with pytest.raises(GateError) as excinfo:
        service.resume_run(wid, run["run_id"], actor="tester")
    assert excinfo.value.code == "run_not_interrupted"


def test_resume_refused_when_the_plan_was_superseded(
    service, approved_order, model_factory
):
    wid, run = _interrupted(service, approved_order, model_factory)
    plan = service.store.get_plan(wid)
    service.store.put_plan({**plan, "version": plan["version"] + 1})
    with pytest.raises(GateError) as excinfo:
        service.resume_run(wid, run["run_id"], actor="tester")
    assert excinfo.value.code == "resume_basis_changed"
    assert "plan" in excinfo.value.message
    refusals = [a for a in service.audit(wid) if a["event"] == "resume_refused"]
    assert refusals and refusals[0]["detail"]["basis"] == "plan_superseded"
    # A refusal changes nothing: the run still stands interrupted.
    assert service.store.get_run(run["run_id"])["status"] == "interrupted"


def test_resume_refused_when_the_target_repo_moved(
    service, approved_order, model_factory
):
    wid, run = _interrupted(service, approved_order, model_factory)
    order = service.get_work_order(wid)
    target = Path(order["target_path"]) / "workshop/inventory.py"
    target.write_text(target.read_text() + "\n# someone else committed\n")
    with pytest.raises(GateError) as excinfo:
        service.resume_run(wid, run["run_id"], actor="tester")
    assert excinfo.value.code == "resume_basis_changed"
    refusals = [a for a in service.audit(wid) if a["event"] == "resume_refused"]
    assert refusals and refusals[0]["detail"]["basis"] == "target_moved"


def test_resume_refused_when_the_workspace_was_touched_then_restart(
    service, approved_order, model_factory
):
    wid, run = _interrupted(service, approved_order, model_factory)
    target = Path(run["workspace"]) / "workshop/inventory.py"
    target.write_text(target.read_text() + "\n# tampered while interrupted\n")
    with pytest.raises(GateError) as excinfo:
        service.resume_run(wid, run["run_id"], actor="tester")
    assert excinfo.value.code == "resume_basis_changed"
    assert "workspace" in excinfo.value.message
    refusals = [a for a in service.audit(wid) if a["event"] == "resume_refused"]
    assert refusals and refusals[0]["detail"]["basis"] == "workspace_changed"
    # The offered path works: restart discards the touched
    # workspace and runs fresh under the same approval.
    fresh = service.restart_run(wid, run["run_id"], actor="tester")
    assert fresh["status"] == "succeeded"


# -- restart ------------------------------------------------------------------


def test_restart_discards_the_workspace_and_runs_fresh(
    service, approved_order, model_factory
):
    wid, run = _interrupted(service, approved_order, model_factory)
    old_dir = Path(run["workspace"]).parent
    assert old_dir.is_dir()
    fresh = service.restart_run(wid, run["run_id"], actor="tester")
    assert fresh["run_id"] != run["run_id"]
    assert fresh["status"] == "succeeded"
    assert not old_dir.exists()
    old = service.store.get_run(run["run_id"])
    assert old["status"] == "interrupted"
    assert old["workspace"] == ""
    restarts = [a for a in service.audit(wid) if a["event"] == "run_restarted"]
    assert restarts
    assert "workshop/inventory.py" in restarts[0]["detail"]["discarded_files"]
    assert service.get_work_order(wid)["status"] == "succeeded"


def test_restarting_a_succeeded_run_refuses(service, approved_order):
    wid = approved_order["work_order_id"]
    run = service.start_run(wid)
    with pytest.raises(GateError) as excinfo:
        service.restart_run(wid, run["run_id"], actor="tester")
    assert excinfo.value.code == "run_not_restartable"


# -- abandon ------------------------------------------------------------------


def test_abandon_names_the_partial_patch_and_review_refuses_it(
    service, approved_order, model_factory
):
    wid, run = _interrupted(service, approved_order, model_factory)
    abandoned = service.abandon_run(
        wid,
        run["run_id"],
        actor="Ops Lead",
        reason="the supplier data changed under us",
    )
    assert abandoned["status"] == "abandoned"
    assert abandoned["partial_files"] == ["workshop/inventory.py"]
    assert abandoned["partial_checkpoint"] == run["last_checkpoint"]
    assert "available" in abandoned["partial_patch"]
    assert abandoned["abandoned_by"] == "Ops Lead"
    assert abandoned["abandon_reason"] == "the supplier data changed under us"
    assert service.get_work_order(wid)["status"] == "abandoned"

    with pytest.raises(GateError) as excinfo:
        service.decide_review(wid, accept=True, actor="Reviewer")
    assert excinfo.value.code == "partial_patch_not_reviewable"

    with pytest.raises(GateError) as excinfo:
        service.abandon_run(
            wid, run["run_id"], actor="Ops Lead", reason="again"
        )
    assert excinfo.value.code == "run_not_abandonable"


def test_abandoning_a_succeeded_run_refuses(service, approved_order):
    wid = approved_order["work_order_id"]
    run = service.start_run(wid)
    assert run["status"] == "succeeded"
    with pytest.raises(GateError) as excinfo:
        service.abandon_run(wid, run["run_id"], actor="Ops Lead", reason="no")
    assert excinfo.value.code == "run_not_abandonable"


def test_abandoning_a_run_with_no_edits_names_no_partial(
    service, approved_order, model_factory
):
    wid = approved_order["work_order_id"]
    model_factory(lambda scripts: FinishesImmediately(scripts))
    run = service.start_run(wid)
    assert run["status"] == "failed"  # no patch: verification fails it
    abandoned = service.abandon_run(
        wid, run["run_id"], actor="Ops Lead", reason="nothing to salvage"
    )
    assert abandoned["status"] == "abandoned"
    assert abandoned["partial_patch"] == ""
    assert abandoned["partial_files"] == []
    assert abandoned["partial_checkpoint"] is None


# -- the API surface -----------------------------------------------------------


def test_recovery_acts_over_the_api(settings, model_factory):
    model_factory(lambda scripts: DiesAfterNCalls(scripts, die_on=3))
    client = TestClient(create_app(settings))
    created = client.post("/api/work-orders/from-gold/reserve-overcommit")
    wid = created.json()["work_order_id"]
    client.post(f"/api/work-orders/{wid}/plan")
    client.post(
        f"/api/work-orders/{wid}/plan/decide",
        json={"approve": True, "actor": "Reviewer"},
    )
    run = client.post(f"/api/work-orders/{wid}/runs", json={}).json()
    assert run["status"] == "interrupted"

    model_factory(_healthy)
    resumed = client.post(
        f"/api/work-orders/{wid}/runs/{run['run_id']}/resume",
        json={"actor": "console"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "succeeded"

    # A second order, interrupted and abandoned over the API: the
    # refusal of its partial patch at review is a 409 with the code.
    model_factory(lambda scripts: DiesAfterNCalls(scripts, die_on=3))
    created = client.post("/api/work-orders/from-gold/reserve-overcommit")
    wid2 = created.json()["work_order_id"]
    client.post(f"/api/work-orders/{wid2}/plan")
    client.post(
        f"/api/work-orders/{wid2}/plan/decide",
        json={"approve": True, "actor": "Reviewer"},
    )
    run2 = client.post(f"/api/work-orders/{wid2}/runs", json={}).json()
    abandoned = client.post(
        f"/api/work-orders/{wid2}/runs/{run2['run_id']}/abandon",
        json={"actor": "Ops Lead", "reason": "not worth salvaging"},
    )
    assert abandoned.status_code == 200
    assert abandoned.json()["partial_files"] == ["workshop/inventory.py"]
    review = client.post(
        f"/api/work-orders/{wid2}/review",
        json={"accept": True, "actor": "Reviewer"},
    )
    assert review.status_code == 409
    assert review.json()["code"] == "partial_patch_not_reviewable"
