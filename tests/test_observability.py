"""Round 15 — observability: the governed failure taxonomy, the
operator digest folded on read, readiness depth, and the log
contract.

The taxonomy conformance test derives every failure name from the
code itself — GateError raise sites, the tool port's refusal
codes, and the failure classes the records carry — and holds the
shipped table to covering all of them. A name added to the code
without a taxonomy entry fails this test, which is the point.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import open_model_coding_agent.service as service_module
from open_model_coding_agent.api import create_app
from open_model_coding_agent.config import REPO_ROOT, Settings
from open_model_coding_agent.errors import ConfigError, GateError
from open_model_coding_agent.model_port import OfflineScriptedModel
from open_model_coding_agent.service import CodingService
from open_model_coding_agent.taxonomy import load_taxonomy

PACKAGE = Path(service_module.__file__).parent


# -- the taxonomy ---------------------------------------------------------


def test_shipped_taxonomy_loads_and_classifies(settings):
    taxonomy = load_taxonomy(settings.taxonomy_dir)
    assert taxonomy.version == 1
    assert taxonomy.classify("stale_plan")["class"] == "plan"
    assert taxonomy.classify("model_output_truncated")["class"] == (
        "model_serving"
    )
    assert taxonomy.classify("unanswered_challenges")["class"] == (
        "gate_refusal"
    )
    entry = taxonomy.classify("verification_failed")
    assert entry["cause"] and entry["action"]


def test_unknown_name_classifies_unclassified_never_dropped(settings):
    taxonomy = load_taxonomy(settings.taxonomy_dir)
    entry = taxonomy.classify("a_name_from_a_newer_build")
    assert entry["class"] == "unclassified"
    assert entry["name"] == "a_name_from_a_newer_build"
    assert taxonomy.classify(None)["class"] == "unclassified"


def _write_taxonomy(tmp_path: Path, mutate) -> Path:
    raw = json.loads(
        (REPO_ROOT / "taxonomy" / "taxonomy-v1.json").read_text()
    )
    mutate(raw)
    folder = tmp_path / "taxonomy"
    folder.mkdir()
    (folder / "taxonomy-v1.json").write_text(json.dumps(raw))
    return folder


def test_taxonomy_loader_refuses_a_version_that_disagrees(tmp_path):
    folder = _write_taxonomy(tmp_path, lambda raw: raw.update(version=9))
    with pytest.raises(ConfigError, match="disagrees"):
        load_taxonomy(folder)


def test_taxonomy_loader_refuses_an_undeclared_class(tmp_path):
    def mutate(raw):
        raw["entries"][0]["class"] = "no_such_class"

    folder = _write_taxonomy(tmp_path, mutate)
    with pytest.raises(ConfigError, match="undeclared class"):
        load_taxonomy(folder)


def test_taxonomy_loader_refuses_a_duplicate_name(tmp_path):
    def mutate(raw):
        raw["entries"].append(dict(raw["entries"][0]))

    folder = _write_taxonomy(tmp_path, mutate)
    with pytest.raises(ConfigError, match="classified twice"):
        load_taxonomy(folder)


def test_taxonomy_loader_refuses_an_entry_without_an_action(tmp_path):
    def mutate(raw):
        raw["entries"][0]["action"] = ""

    folder = _write_taxonomy(tmp_path, mutate)
    with pytest.raises(ConfigError, match="next action"):
        load_taxonomy(folder)


def test_taxonomy_loader_refuses_an_empty_directory(tmp_path):
    with pytest.raises(ConfigError, match="no taxonomy"):
        load_taxonomy(tmp_path)


def _derived_failure_names() -> set[str]:
    """Every failure name the code can produce, derived from the
    source — never hand-listed here:

    - the literal first argument of every ``GateError(...)`` raise;
    - the literal ``code=`` of every tool result built in tools.py;
    - the string constants the service and comparisons record as a
      run's or a benchmark's ``failure_class`` (assignments, dict
      values, list members), the constants ``_run_failure_class``
      returns, and the ``process_lost`` interruption reason.
    ``config_error`` is the one deliberate exception — a
    ConfigError carries no code — and the test names it explicitly
    rather than deriving it.
    """
    names: set[str] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "GateError"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
            if path.name == "tools.py" and isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if (
                        keyword.arg == "code"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        names.add(keyword.value.value)
            if isinstance(node, ast.Assign) and isinstance(
                node.value, ast.Constant
            ):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value
                        in ("failure_class", "interruption_reason")
                        and isinstance(node.value.value, str)
                    ):
                        names.add(node.value.value)
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if not (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                    ):
                        continue
                    if key.value == "failure_class" and isinstance(
                        value, ast.Constant
                    ) and isinstance(value.value, str):
                        names.add(value.value)
                    if key.value == "failure_classes" and isinstance(
                        value, (ast.List, ast.Tuple)
                    ):
                        for element in value.elts:
                            if isinstance(
                                element, ast.Constant
                            ) and isinstance(element.value, str):
                                names.add(element.value)
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_run_failure_class"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Return)
                        and isinstance(sub.value, ast.Tuple)
                        and sub.value.elts
                        and isinstance(sub.value.elts[0], ast.Constant)
                        and isinstance(sub.value.elts[0].value, str)
                    ):
                        names.add(sub.value.elts[0].value)
    return names


def test_every_raisable_failure_name_is_classified(settings):
    taxonomy = load_taxonomy(settings.taxonomy_dir)
    derived = _derived_failure_names()
    assert len(derived) > 100  # the derivation itself is not empty
    missing = sorted(
        name
        for name in derived
        if taxonomy.classify(name)["class"] == "unclassified"
    )
    assert missing == [], (
        f"failure names the code can raise with no taxonomy entry: "
        f"{missing} — publish a new taxonomy version classifying them"
    )
    # The one deliberate non-derived entry: ConfigError has no code.
    assert taxonomy.classify("config_error")["class"] == "store_infra"


# -- the digest -----------------------------------------------------------


def _seed_success_and_failure(service, approved_order, monkeypatch):
    wid = approved_order["work_order_id"]
    run = service.start_run(wid)
    assert run["status"] == "succeeded"
    service.decide_review(wid, accept=True, actor="Reviewer")

    order2 = service.create_gold_work_order("reserve-overcommit")
    wid2 = order2["work_order_id"]
    service.draft_plan(wid2, actor="Tester")
    service.decide_plan(wid2, approve=True, actor="Reviewer")
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
    failed = service.start_run(wid2)
    assert failed["status"] == "failed"
    return wid, wid2


def test_digest_counts_a_seeded_mixed_history(
    service, approved_order, monkeypatch
):
    _seed_success_and_failure(service, approved_order, monkeypatch)
    digest = service.operations_digest()
    assert digest["runs"]["total"] == 2
    assert digest["runs"]["by_status"] == {"succeeded": 1, "failed": 1}
    assert digest["failures"]["total"] == 1
    assert digest["failures"]["by_name"] == {"verification_failed": 1}
    assert digest["failures"]["by_class"] == {"verification": 1}
    assert digest["failures"]["unclassified"] == {}
    top = digest["failures"]["top"][0]
    assert top["name"] == "verification_failed"
    assert top["class_label"] == "Verification"
    assert top["action"]  # the taxonomy's operator text rides along
    assert digest["plan_decisions"]["approved"] == 2
    assert digest["plan_decisions"]["drafted"] == 2
    assert digest["review_decisions"]["accepted"] == 1
    assert digest["taxonomy_version"] == 1
    assert digest["cost"]["cost_per_successful_task"] is not None


def test_digest_window_filters_by_created_at(service, approved_order):
    service.start_run(approved_order["work_order_id"])
    empty = service.operations_digest(since="2999-01-01T00:00:00+00:00")
    assert empty["runs"]["total"] == 0
    assert empty["failures"]["total"] == 0
    assert empty["plan_decisions"]["drafted"] == 0
    everything = service.operations_digest(until="2999-01-01T00:00:00+00:00")
    assert everything["runs"]["total"] == 1


def test_digest_refuses_a_nonsense_window(service):
    with pytest.raises(GateError) as exc:
        service.operations_digest(since="not-a-timestamp")
    assert exc.value.code == "invalid_digest_window"
    with pytest.raises(GateError) as exc:
        service.operations_digest(
            since="2026-10-20T00:00:00+00:00",
            until="2026-10-19T00:00:00+00:00",
        )
    assert exc.value.code == "invalid_digest_window"


def test_digest_counts_an_unclassified_name(
    settings, approved_order, tmp_path
):
    """A service whose taxonomy predates a name counts that name as
    unclassified — visible in the digest, never dropped."""
    wid = approved_order["work_order_id"]
    service = CodingService(settings)
    run = service.start_run(wid)
    assert run["status"] == "succeeded"

    folder = _write_taxonomy(
        tmp_path,
        lambda raw: raw["entries"].__setitem__(
            slice(None),
            [
                entry
                for entry in raw["entries"]
                if entry["name"] != "verification_failed"
            ],
        ),
    )
    older = CodingService(
        Settings(
            **{
                **settings.__dict__,
                "taxonomy_dir": folder,
                "sqlite_path": settings.sqlite_path,
            }
        )
    )
    # Seed a failed run record directly — the fold reads records.
    failed = dict(run)
    failed["run_id"] = "run-from-a-newer-build"
    failed["status"] = "failed"
    failed["failure_class"] = "verification_failed"
    older.store.put_run(failed)
    digest = older.operations_digest()
    assert digest["failures"]["unclassified"] == {"verification_failed": 1}
    assert digest["failures"]["by_class"] == {"unclassified": 1}


def test_digest_is_served_by_the_api(settings):
    client = TestClient(create_app(settings))
    response = client.get("/api/operations-digest")
    assert response.status_code == 200
    body = response.json()
    assert body["taxonomy_version"] == 1
    assert "cost_per_successful_task" in body["cost"]
    refused = client.get(
        "/api/operations-digest", params={"since": "junk"}
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "invalid_digest_window"


# -- readiness ------------------------------------------------------------


def test_readiness_offline_is_ready_with_depth(settings):
    service = CodingService(settings)
    readiness = service.readiness()
    assert readiness["status"] == "ready"
    assert readiness["store_reachable"] is True
    assert readiness["migrations"]["current"] is True
    assert readiness["migrations"]["behind"] == []
    assert set(readiness["migrations"]["applied"]) == set(
        readiness["migrations"]["on_disk"]
    )
    assert readiness["model_state"] == {"state": "ready", "reason": None}
    assert readiness["price_table_version"] == 2
    assert readiness["taxonomy_version"] == 1
    # The fields the API has always carried are still there.
    for key in ("store", "model_profile", "model_label",
                "default_model_profile", "baseten_base_url",
                "baseten_model"):
        assert key in readiness


def test_readiness_names_a_not_servable_profile(tmp_path):
    settings = Settings(
        sqlite_path=tmp_path / "db.sqlite",
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-qwen",
    )
    readiness = CodingService(settings).readiness()
    assert readiness["status"] == "not_ready"
    assert readiness["model_state"]["state"] == "not_ready"
    assert "lists no Qwen model" in readiness["model_state"]["reason"]


def test_readiness_names_a_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    settings = Settings(
        sqlite_path=tmp_path / "db.sqlite",
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-deepseek",
    )
    readiness = CodingService(settings).readiness()
    assert readiness["status"] == "not_ready"
    assert "BASETEN_API_KEY" in readiness["model_state"]["reason"]


def test_readiness_reports_migrations_behind(settings, tmp_path):
    service = CodingService(settings)
    migrations_dir = tmp_path / "migrations"
    sqlite_dir = migrations_dir / "sqlite"
    sqlite_dir.mkdir(parents=True)
    for source in (REPO_ROOT / "migrations" / "sqlite").glob("*.sql"):
        (sqlite_dir / source.name).write_text(source.read_text())
    (sqlite_dir / "9999_from_a_newer_build.sql").write_text(
        "CREATE TABLE IF NOT EXISTS from_a_newer_build (id TEXT);"
    )
    service.store.migrations_dir = migrations_dir
    readiness = service.readiness()
    assert readiness["status"] == "not_ready"
    assert readiness["migrations"]["behind"] == ["9999_from_a_newer_build"]
    assert readiness["migrations"]["current"] is False


# -- the log contract ------------------------------------------------------

_ENTITY_KEYS = (
    "work_order_id",
    "run_id",
    "batch_id",
    "comparison_id",
    "challenge_id",
    "template",
    "profile_id",
)


def test_log_contract_over_a_full_work_order_journey(settings, capfd):
    client = TestClient(create_app(settings))
    headers = {"x-request-id": "req-r15-journey"}
    gold = client.get("/api/gold-tasks").json()
    task = gold[0]
    created = client.post(
        "/api/work-orders",
        json={
            "title": task["title"],
            "task_text": task["task_text"],
            "file_scope": task["file_scope"],
            "gold_task_id": task["id"],
        },
        headers=headers,
    )
    assert created.status_code == 201
    wid = created.json()["work_order_id"]
    assert client.post(
        f"/api/work-orders/{wid}/plan", headers=headers
    ).status_code == 200
    assert client.post(
        f"/api/work-orders/{wid}/plan/decide",
        json={"approve": True, "actor": "Reviewer"},
        headers=headers,
    ).status_code == 200
    run = client.post(f"/api/work-orders/{wid}/runs", headers=headers)
    assert run.status_code == 201
    assert run.json()["status"] == "succeeded"
    assert client.post(
        f"/api/work-orders/{wid}/review",
        json={"decision": "accept", "actor": "Reviewer"},
        headers=headers,
    ).status_code == 200

    out, _err = capfd.readouterr()
    lines = [
        json.loads(line)
        for line in out.splitlines()
        if line.strip().startswith("{")
    ]
    assert lines, "the journey produced no structured log lines"
    for line in lines:
        for field in ("ts", "level", "event", "request_id"):
            assert field in line, f"log line misses {field}: {line}"
        assert any(key in line for key in _ENTITY_KEYS), (
            f"log line names no entity it touches: {line}"
        )
    journey = [line for line in lines if line["request_id"] == "req-r15-journey"]
    assert journey, "no line carries the request's id"
    events = {line["event"] for line in lines}
    # The plan lifecycle logs now, like every other governed act.
    assert "plan_drafted" in events
    assert "plan_decided" in events
    drafted = next(line for line in lines if line["event"] == "plan_drafted")
    assert drafted["work_order_id"] == wid
    # The console renders the digest section.
    console = client.get("/").text
    assert 'id="digest"' in console
    assert "Operations digest" in console
