"""Round 18: dual control on the two consequential acceptance
acts.

Under a deployment profile that requires it (hosted and
hosted-self as shipped; a test record here, so the behaviour is
exercised without the hosted profile's other rules demanding a
live model and a Postgres store):

- review acceptance takes two recorded decisions by different
  actors, neither of them the order's requester or the plan's
  approver; the first parks the order at
  ``awaiting_second_acceptance`` with the patch unexported, the
  second completes it; a reject at the parked state closes the
  act with both decisions on record; the first decider — and
  only they — may withdraw, returning the order to undecided;
- promotion and rollback park and complete the same way at the
  promotion gate, the default unmoved until the second decision;
- every self-dealing shape refuses by its own name, and the
  names stand in the round 15 taxonomy;
- under the developer default (rule off), none of this exists:
  the rest of the suite is the proof, and one test here pins the
  reads that say so.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from open_model_coding_agent.api import create_app
from open_model_coding_agent.config import load_settings
from open_model_coding_agent.deployments import list_deployment_profiles
from open_model_coding_agent.errors import ConfigError, GateError
from open_model_coding_agent.service import CodingService

REPO_ROOT = Path(__file__).resolve().parents[1]

DUAL_RECORD = {
    "name": "dual-required",
    "label": "Dual control required, everything else permissive",
    "version": 1,
    "description": (
        "A test profile: every rule permissive except dual "
        "control, so the gates' behaviour is exercised without "
        "the hosted profile's live-model and Postgres demands."
    ),
    "rules": {
        "model": "any",
        "store": "any",
        "api_key": "profile",
        "endpoint": "any",
        "dual_control": "required",
    },
}


def _write_record(directory: Path, record: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{record['name']}.json").write_text(
        json.dumps(record)
    )
    return directory


def _dual_settings(tmp_path: Path, **extra):
    dep_dir = _write_record(tmp_path / "deployments", DUAL_RECORD)
    kwargs = dict(
        sqlite_path=tmp_path / "dual.db",
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        deployment_profile="dual-required",
        deployment_profiles_dir=dep_dir,
    )
    kwargs.update(extra)
    return load_settings(**kwargs)


@pytest.fixture
def dual_service(tmp_path) -> CodingService:
    return CodingService(_dual_settings(tmp_path))


def _refusal(fn, *args, **kwargs) -> str:
    with pytest.raises(GateError) as caught:
        fn(*args, **kwargs)
    return caught.value.code


def _drive_to_succeeded(service, *, requester="Requester",
                        approver="Approver"):
    order = service.create_gold_work_order(
        "reserve-overcommit", actor=requester
    )
    wid = order["work_order_id"]
    service.draft_plan(wid, actor=requester)
    service.decide_plan(wid, approve=True, actor=approver)
    run = service.start_run(wid, actor=requester)
    assert run["status"] == "succeeded"
    return wid, run


# -- the rule as governed data ------------------------------------------


def test_the_shipped_records_bind_dual_control():
    profiles = {
        p.name: p
        for p in list_deployment_profiles(
            REPO_ROOT / "profiles" / "deployment"
        )
    }
    assert profiles["local"].rules["dual_control"] == "off"
    assert profiles["hosted"].rules["dual_control"] == "required"
    assert profiles["hosted-self"].rules["dual_control"] == "required"
    # Round 18 took the records to v2: the vocabulary grew, and a
    # rule change is a new version, never a quiet edit.
    assert {p.version for p in profiles.values()} == {2}


def test_the_loader_refuses_a_record_that_does_not_declare_the_rule(
    tmp_path,
):
    record = dict(DUAL_RECORD)
    record["rules"] = {
        k: v for k, v in DUAL_RECORD["rules"].items()
        if k != "dual_control"
    }
    directory = _write_record(tmp_path / "deps", record)
    with pytest.raises(ConfigError, match="dual_control"):
        list_deployment_profiles(directory)


def test_the_loader_refuses_an_unknown_dual_control_value(tmp_path):
    record = dict(DUAL_RECORD)
    record["rules"] = {**DUAL_RECORD["rules"], "dual_control": "sometimes"}
    directory = _write_record(tmp_path / "deps", record)
    with pytest.raises(ConfigError, match="dual_control"):
        list_deployment_profiles(directory)


def test_readiness_states_the_dual_control_rule(dual_service):
    rules = {
        r["rule"]: r
        for r in dual_service.readiness()["deployment_rules"]
    }
    assert rules["dual_control"]["requires"] == "required"
    assert rules["dual_control"]["state"] == "satisfied"
    assert "two recorded decisions" in rules["dual_control"]["detail"]


def test_dual_control_is_off_under_the_developer_default(service):
    assert service.promotion_state()["dual_control"] == {
        "required": False,
        "parked_act": None,
    }
    assert service.operations_digest()["dual_control"]["required"] is False


# -- review acceptance ----------------------------------------------------


def test_acceptance_parks_then_a_second_actor_completes(dual_service):
    wid, run = _drive_to_succeeded(dual_service)
    artifact = dual_service.settings.artifacts_dir / f"{wid}.patch"
    # The creators are excluded from deciding, by name.
    assert _refusal(
        dual_service.decide_review, wid, accept=True, actor="Requester"
    ) == "dual_control_excluded_actor"
    assert _refusal(
        dual_service.decide_review, wid, accept=True, actor=" approver "
    ) == "dual_control_excluded_actor"
    # The first decision parks: named state, patch unexported.
    parked = dual_service.decide_review(
        wid, accept=True, actor="Reviewer One"
    )
    assert parked["status"] == "awaiting_second_acceptance"
    assert not artifact.exists()
    queue = dual_service.review_queue()
    entry = next(
        e for e in queue["entries"] if e["work_order_id"] == wid
    )
    assert entry["category"] == "awaiting_second_acceptance"
    assert entry["next_act"] == "review"
    assert entry["first_actor"] == "Reviewer One"
    packet = dual_service.review_packet(wid)
    assert packet["dual_control"]["required"] is True
    assert packet["dual_control"]["acceptance"]["first_actor"] == (
        "Reviewer One"
    )
    # The same actor again is not a second decision.
    assert _refusal(
        dual_service.decide_review, wid, accept=True,
        actor=" REVIEWER ONE ",
    ) == "dual_control_same_actor"
    # Nor is an excluded actor, however distinct.
    assert _refusal(
        dual_service.decide_review, wid, accept=True, actor="Requester"
    ) == "dual_control_excluded_actor"
    # The second, distinct decision completes the act.
    done = dual_service.decide_review(
        wid, accept=True, actor="Reviewer Two"
    )
    assert done["status"] == "accepted"
    assert artifact.read_text() == run["patch"]
    accepted = [
        row for row in dual_service.store.list_audit("work_order", wid)
        if row["event"] == "review_accepted"
    ]
    assert accepted[-1]["detail"]["first_actor"] == "Reviewer One"
    assert accepted[-1]["detail"]["second_actor"] == "Reviewer Two"
    digest = dual_service.operations_digest()
    assert digest["dual_control"]["acceptances_parked"] == 1
    # A completed act takes no further decision.
    assert _refusal(
        dual_service.decide_review, wid, accept=True,
        actor="Reviewer Three",
    ) == "dual_control_already_decided"
    assert _refusal(
        dual_service.decide_review, wid, accept=False,
        actor="Reviewer Three", reason="too late",
    ) == "dual_control_already_decided"


def test_a_reject_at_the_parked_state_closes_with_both_records(
    dual_service,
):
    wid, run = _drive_to_succeeded(dual_service)
    dual_service.decide_review(wid, accept=True, actor="Reviewer One")
    closed = dual_service.decide_review(
        wid, accept=False, actor="Reviewer Two",
        reason="the guard is broader than the task asked for",
    )
    assert closed["status"] == "rejected"
    events = [
        (row["event"], row["actor"])
        for row in dual_service.store.list_audit("work_order", wid)
    ]
    assert ("review_acceptance_parked", "Reviewer One") in events
    rejected = [
        row for row in dual_service.store.list_audit("work_order", wid)
        if row["event"] == "review_rejected"
    ]
    assert rejected[-1]["actor"] == "Reviewer Two"
    assert rejected[-1]["detail"]["parked_first_actor"] == "Reviewer One"
    assert dual_service.review_packet(wid)["dual_control"][
        "acceptance"
    ] is None


def test_withdraw_returns_the_act_to_undecided(dual_service):
    wid, run = _drive_to_succeeded(dual_service)
    dual_service.decide_review(wid, accept=True, actor="Reviewer One")
    # Only the decider may withdraw their decision.
    assert _refusal(
        dual_service.withdraw_review_acceptance, wid,
        actor="Reviewer Two",
    ) == "dual_control_not_the_decider"
    restored = dual_service.withdraw_review_acceptance(
        wid, actor=" reviewer one "
    )
    assert restored["status"] == "succeeded"
    assert dual_service.review_packet(wid)["dual_control"][
        "acceptance"
    ] is None
    # Undecided again: a fresh pair of decisions completes it,
    # in the other order.
    dual_service.decide_review(wid, accept=True, actor="Reviewer Two")
    done = dual_service.decide_review(
        wid, accept=True, actor="Reviewer One"
    )
    assert done["status"] == "accepted"
    digest = dual_service.operations_digest()
    assert digest["dual_control"]["acceptances_withdrawn"] == 1
    assert digest["dual_control"]["acceptances_parked"] == 2


def test_withdraw_without_a_park_refuses_by_name(dual_service):
    wid, run = _drive_to_succeeded(dual_service)
    assert _refusal(
        dual_service.withdraw_review_acceptance, wid,
        actor="Reviewer One",
    ) == "dual_control_no_parked_act"


def test_the_withdraw_route_is_on_the_api(dual_service):
    client = TestClient(create_app(dual_service.settings))
    wid, run = _drive_to_succeeded(dual_service)
    parked = client.post(
        f"/api/work-orders/{wid}/review",
        json={"accept": True, "actor": "Reviewer One"},
    )
    assert parked.status_code == 200
    assert parked.json()["status"] == "awaiting_second_acceptance"
    refused = client.post(
        f"/api/work-orders/{wid}/review/withdraw-acceptance",
        json={"actor": "Reviewer Two"},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "dual_control_not_the_decider"
    withdrawn = client.post(
        f"/api/work-orders/{wid}/review/withdraw-acceptance",
        json={"actor": "Reviewer One"},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "succeeded"


# -- promotion and rollback -----------------------------------------------


def _write_profiles(tmp_path: Path) -> Path:
    """The repo's model profile records, plus a byte-identical
    behavioural copy of the incumbent — the promotion gate's
    positive control, as in the promotion tests."""
    target = tmp_path / "model-profiles"
    target.mkdir(parents=True)
    for path in sorted((REPO_ROOT / "profiles").glob("*.json")):
        shutil.copy(path, target / path.name)
    (target / "offline-copy.json").write_text(json.dumps({
        "profile_id": "offline-copy",
        "version": 1,
        "label": "Offline scripted model — copy of the incumbent",
        "kind": "offline",
    }))
    return target


@pytest.fixture
def dual_promo(tmp_path) -> CodingService:
    """A dual-control service at the promotion gate: the
    incumbent baselined on the held-out set, the candidate
    evaluated against it — the gate ready for a decision."""
    settings = _dual_settings(
        tmp_path, profiles_dir=_write_profiles(tmp_path)
    )
    service = CodingService(settings)
    service.evaluate_profile("offline", actor="Evaluator", times=1)
    service.evaluate_profile("offline-copy", actor="Evaluator", times=1)
    return service


def test_promotion_parks_then_a_second_actor_completes(dual_promo):
    # The evaluator is excluded from judging their own evidence,
    # under dual control as before it.
    assert _refusal(
        dual_promo.promote, "offline-copy", actor="Evaluator",
        rationale="my own numbers",
    ) == "self_promotion"
    parked = dual_promo.promote(
        "offline-copy", actor="Promoter One", rationale="equal, newer"
    )
    assert parked["status"] == "awaiting_second_decision"
    assert parked["first_actor"] == "Promoter One"
    # Parked is not promoted: the default has not moved and no
    # event stands in its history.
    assert dual_promo.default_profile_id() == "offline"
    assert dual_promo.promotion_state()["events"] == []
    state = dual_promo.promotion_state()["dual_control"]
    assert state["required"] is True
    assert state["parked_act"]["to_profile"] == "offline-copy"
    assert _refusal(
        dual_promo.promote, "offline-copy", actor=" promoter one ",
        rationale="again",
    ) == "dual_control_same_actor"
    done = dual_promo.promote(
        "offline-copy", actor="Promoter Two", rationale="second pair of eyes"
    )
    assert done["kind"] == "promotion"
    assert done["default_profile_id"] == "offline-copy"
    assert done["dual_control"] == {
        "first_actor": "Promoter One",
        "second_actor": "Promoter Two",
    }
    assert dual_promo.default_profile_id() == "offline-copy"
    # Promoting the standing default is a completed act.
    assert _refusal(
        dual_promo.promote, "offline-copy", actor="Promoter Three",
        rationale="once more",
    ) == "dual_control_already_decided"


def test_a_parked_act_blocks_other_acts_and_closes_by_rejection(
    dual_promo,
):
    dual_promo.promote(
        "offline-copy", actor="Promoter One", rationale="first"
    )
    dual_promo.promote(
        "offline-copy", actor="Promoter Two", rationale="second"
    )
    # The promotion's own completing actor may not decide its
    # rollback — the creator exclusion, in the other direction.
    assert _refusal(
        dual_promo.rollback, actor="Promoter Two",
        rationale="undo my own act",
    ) == "dual_control_excluded_actor"
    parked = dual_promo.rollback(
        actor="Operator One", rationale="the copy misbehaves in staging"
    )
    assert parked["status"] == "awaiting_second_decision"
    assert parked["kind"] == "rollback"
    assert dual_promo.default_profile_id() == "offline-copy"
    # A different act while one stands parked refuses by name.
    assert _refusal(
        dual_promo.promote, "offline", actor="Operator Two",
        rationale="promote the original back instead",
    ) == "dual_control_pending_act"
    # The adverse close: both decisions on record, default unmoved.
    rejected = dual_promo.reject_promotion_decision(
        actor="Operator Two", reason="the staging fault was ours"
    )
    assert rejected["status"] == "rejected"
    assert rejected["first_actor"] == "Operator One"
    assert dual_promo.promotion_state()["dual_control"][
        "parked_act"
    ] is None
    assert dual_promo.default_profile_id() == "offline-copy"
    digest = dual_promo.operations_digest()
    assert digest["dual_control"]["promotion_decisions_parked"] == 2
    assert digest["dual_control"]["promotion_decisions_rejected"] == 1


def test_rollback_parks_withdraws_and_completes(dual_promo):
    dual_promo.promote(
        "offline-copy", actor="Promoter One", rationale="first"
    )
    dual_promo.promote(
        "offline-copy", actor="Promoter Two", rationale="second"
    )
    dual_promo.rollback(actor="Operator One", rationale="roll it back")
    assert _refusal(
        dual_promo.withdraw_promotion_decision, actor="Operator Two"
    ) == "dual_control_not_the_decider"
    withdrawn = dual_promo.withdraw_promotion_decision(
        actor="Operator One"
    )
    assert withdrawn["status"] == "withdrawn"
    assert dual_promo.default_profile_id() == "offline-copy"
    dual_promo.rollback(actor="Operator One", rationale="once more")
    done = dual_promo.rollback(
        actor="Operator Two", rationale="confirmed, roll back"
    )
    assert done["kind"] == "rollback"
    assert done["default_profile_id"] == "offline"
    assert done["dual_control"]["first_actor"] == "Operator One"
    assert dual_promo.default_profile_id() == "offline"
    # Nothing parked: the close-out acts refuse by name.
    assert _refusal(
        dual_promo.withdraw_promotion_decision, actor="Operator One"
    ) == "dual_control_no_parked_act"
    assert _refusal(
        dual_promo.reject_promotion_decision, actor="Operator One"
    ) == "dual_control_no_parked_act"


def test_the_promotion_decision_routes_are_on_the_api(dual_promo):
    client = TestClient(create_app(dual_promo.settings))
    empty = client.post(
        "/api/promotions/reject-decision",
        json={"actor": "Operator", "reason": "nothing parked"},
    )
    assert empty.status_code == 409
    assert empty.json()["code"] == "dual_control_no_parked_act"
    parked = client.post(
        "/api/promotions/promote",
        json={
            "profile_id": "offline-copy",
            "actor": "Promoter One",
            "rationale": "first decision",
        },
    )
    assert parked.status_code == 200
    assert parked.json()["status"] == "awaiting_second_decision"
    state = client.get("/api/promotions").json()
    assert state["dual_control"]["parked_act"]["first_actor"] == (
        "Promoter One"
    )
    withdrawn = client.post(
        "/api/promotions/withdraw-decision",
        json={"actor": "Promoter One"},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"
    assert client.get("/api/promotions").json()["dual_control"][
        "parked_act"
    ] is None
