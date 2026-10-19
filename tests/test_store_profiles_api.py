"""Store round-trips, the fail-closed profile loader, and the API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from open_model_coding_agent.api import create_app
from open_model_coding_agent.config import load_profile, load_settings
from open_model_coding_agent.errors import ConfigError, GateError
from open_model_coding_agent.service import CodingService
from open_model_coding_agent.store import SqlStore


def test_store_round_trip(settings):
    store = SqlStore(settings)
    order = {
        "work_order_id": "wo-1",
        "status": "draft",
        "gold_task_id": None,
        "created_at": "2026-10-19T00:00:00+00:00",
        "updated_at": "2026-10-19T00:00:00+00:00",
        "title": "t",
    }
    store.put_work_order(order)
    assert store.get_work_order("wo-1")["title"] == "t"
    order["status"] = "approved"
    store.put_work_order(order)
    assert store.get_work_order("wo-1")["status"] == "approved"
    store.append_audit("work_order", "wo-1", "created", "tester", {"n": 1})
    store.append_audit("work_order", "wo-1", "planned", "tester", {"n": 2})
    events = [a["event"] for a in store.list_audit("work_order", "wo-1")]
    assert events == ["created", "planned"]


def test_unknown_profile_refused_loudly():
    with pytest.raises(ConfigError) as exc:
        load_profile("nope")
    assert "valid profiles" in str(exc.value)


def test_baseten_profile_shape():
    profile = load_profile("baseten")
    assert profile.base_url == "https://inference.baseten.co/v1"
    assert profile.api_key_env == "BASETEN_API_KEY"
    assert profile.model == "zai-org/GLM-5"


def test_baseten_run_refuses_until_round_2(tmp_path):
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten",
    )
    service = CodingService(settings)
    order = service.create_gold_work_order("reserve-overcommit")
    with pytest.raises(GateError) as exc:
        service.draft_plan(order["work_order_id"])
    assert exc.value.code == "live_model_not_available"


def test_api_flow(settings):
    client = TestClient(create_app(settings))
    assert client.get("/health").json() == {"status": "ok"}
    readiness = client.get("/readiness").json()
    assert readiness["store"] == "sqlite"
    assert readiness["model_profile"] == "offline"

    created = client.post("/api/work-orders/from-gold/bulk-tier-boundary")
    assert created.status_code == 201
    wid = created.json()["work_order_id"]

    refused = client.post(f"/api/work-orders/{wid}/runs")
    assert refused.status_code == 409
    assert refused.json()["code"] == "plan_not_approved"

    client.post(f"/api/work-orders/{wid}/plan")
    client.post(
        f"/api/work-orders/{wid}/plan/decide",
        json={"approve": True, "actor": "Reviewer"},
    )
    run = client.post(f"/api/work-orders/{wid}/runs")
    assert run.status_code == 201
    assert run.json()["status"] == "succeeded"

    detail = client.get(f"/api/work-orders/{wid}").json()
    assert detail["work_order"]["status"] == "succeeded"
    assert detail["plan"]["status"] == "approved"

    reviewed = client.post(
        f"/api/work-orders/{wid}/review",
        json={"accept": True, "actor": "Reviewer"},
    )
    assert reviewed.json()["status"] == "accepted"
