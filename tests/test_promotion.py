"""Round 7: the model promotion gate.

The gold suite is split into a tuning set and a held-out set, each
named by a content hash over its tasks. A profile is benchmarked
through the real service path and the records are stored; a
promotion evaluation runs the held-out set N times and is scored
by its worst run. Promotion is a judged act — it needs a passing,
current-hash evaluation, no regression against the incumbent's own
recorded baseline, and a judge who is not the evaluator — and it
moves the default as a stored record, with rollback as its own
recorded act.

The controls are the point: an unevaluated profile cannot promote,
the evaluator cannot promote their own candidate, a candidate
whose worst run regresses against the baseline cannot promote,
and an evaluation recorded against a set that has since moved is
stale. An equal candidate — here, a copy of the incumbent — CAN
promote: the bar is no regression, not must-beat.

Run budget, stated honestly: the behavioural controls need real
runs (each gold run shells out to pytest for verification, as the
product does), so pytest drives evaluations at times=1 over the
2-task held-out set — the smallest shape that proves the rules.
The full worst-of-3 discipline is exercised in the eval runner's
promotion leg.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import open_model_coding_agent.service as service_module
from open_model_coding_agent.api import create_app
from open_model_coding_agent.benchmarks import (
    BENCHMARK_SETS,
    SET_HELD_OUT,
    split_problems,
    task_set_hash,
)
from open_model_coding_agent.config import load_settings
from open_model_coding_agent.errors import GateError
from open_model_coding_agent.gold import GoldSuite
from open_model_coding_agent.model_port import OfflineScriptedModel
from open_model_coding_agent.service import CodingService

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLD_DIR = REPO_ROOT / "evals" / "gold"


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for name in ("BASETEN_API_KEY", "BASETEN_MODEL", "BASETEN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def write_profiles(folder: Path) -> Path:
    """The repo's profile records, plus two offline-kind records
    the gate's controls need: a byte-identical copy of the
    incumbent (same kind, same behaviour — a different record),
    and a second copy that will serve as the weak candidate."""
    folder.mkdir(parents=True, exist_ok=True)
    for source in sorted((REPO_ROOT / "profiles").glob("*.json")):
        shutil.copy(source, folder / source.name)
    for profile_id, label in (
        ("offline-copy", "Offline scripted model — copy of the incumbent"),
        ("offline-weak", "Offline scripted model — weak candidate"),
    ):
        (folder / f"{profile_id}.json").write_text(
            json.dumps(
                {
                    "profile_id": profile_id,
                    "version": 1,
                    "label": label,
                    "kind": "offline",
                }
            )
        )
    return folder


def promo_settings(tmp_path: Path, **overrides):
    kwargs = dict(
        sqlite_path=str(tmp_path / "codingagent.db"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        profiles_dir=write_profiles(tmp_path / "profiles"),
    )
    kwargs.update(overrides)
    return load_settings(**kwargs)


@pytest.fixture()
def promo(tmp_path):
    return CodingService(promo_settings(tmp_path))


# -- the split and the hash -------------------------------------------------


def test_split_partitions_the_suite():
    suite = GoldSuite(GOLD_DIR)
    assert split_problems(suite) == []
    assert len(BENCHMARK_SETS["tuning"]) == 4
    assert len(BENCHMARK_SETS[SET_HELD_OUT]) == 2
    assigned = [t for ids in BENCHMARK_SETS.values() for t in ids]
    assert sorted(assigned) == sorted(t.id for t in suite.tasks)


def test_task_set_hash_is_stable_and_content_bound(tmp_path):
    suite = GoldSuite(GOLD_DIR)
    held_out = task_set_hash(suite, SET_HELD_OUT)
    tuning = task_set_hash(suite, "tuning")
    assert held_out == task_set_hash(GoldSuite(GOLD_DIR), SET_HELD_OUT)
    assert held_out != tuning

    moved = tmp_path / "gold"
    shutil.copytree(GOLD_DIR, moved)
    overlay = moved / "broken" / "order-coupon" / "workshop" / "pricing.py"
    overlay.write_text(overlay.read_text() + "\n# someone edited the defect\n")
    drifted = GoldSuite(moved)
    assert task_set_hash(drifted, SET_HELD_OUT) != held_out
    assert task_set_hash(drifted, "tuning") == tuning

    grading = moved / "grading" / "bulk-tier-boundary" / "test_grading_pricing.py"
    grading.write_text(grading.read_text() + "\n# a stricter grading test\n")
    assert task_set_hash(GoldSuite(moved), SET_HELD_OUT) != held_out


# -- benchmarks ---------------------------------------------------------------


def test_benchmark_records_the_real_path(promo):
    record = promo.run_benchmark("offline", set_name=SET_HELD_OUT, actor="Tester")
    assert record["successes"] == 2
    assert record["tasks"] == 2
    assert record["violations"] == 0
    assert record["task_set_hash"] == task_set_hash(promo.gold, SET_HELD_OUT)
    for outcome in record["outcomes"]:
        assert outcome["verified_success"] is True
        assert outcome["failure_class"] is None
        assert outcome["run_id"] and outcome["work_order_id"]
    stored = promo.list_benchmarks("offline")
    assert [b["benchmark_id"] for b in stored] == [record["benchmark_id"]]


# -- the gate -------------------------------------------------------------------


def test_promotion_happy_path_self_promotion_and_rollback(promo):
    baseline = promo.evaluate_profile("offline", actor="Evaluator", times=1)
    assert baseline["passed"] is True
    assert baseline["worst_successes"] == baseline["set_size"] == 2

    candidate = promo.evaluate_profile("offline-copy", actor="Evaluator", times=1)
    assert candidate["passed"] is True
    assert candidate["worst_successes"] == 2
    assert len(candidate["benchmark_ids"]) == 1

    # The evaluator cannot promote their own candidate — by
    # normalised name, so case and spacing do not launder it.
    with pytest.raises(GateError) as exc:
        promo.promote(
            "offline-copy", actor="  EVALUATOR ", rationale="my own work"
        )
    assert exc.value.code == "self_promotion"
    assert promo.default_profile_id() == "offline"

    event = promo.promote(
        "offline-copy", actor="Promoter", rationale="equal evidence, new default"
    )
    assert event["from_profile"] == "offline"
    assert event["evaluation_id"] == candidate["evaluation_id"]
    assert promo.default_profile_id() == "offline-copy"

    # The promotion is live in the shell, not just on the record:
    # new work is served and stamped by the promoted profile.
    order = promo.create_gold_work_order("reserve-overcommit", actor="Tester")
    plan = promo.draft_plan(order["work_order_id"], actor="Tester")
    assert plan["model_profile"] == "offline-copy"

    back = promo.rollback(actor="Promoter", rationale="not yet")
    assert back["default_profile_id"] == "offline"
    assert promo.default_profile_id() == "offline"
    state = promo.promotion_state()
    assert [e["kind"] for e in state["events"]] == ["promotion", "rollback"]


def test_promote_without_evidence_and_rollback_without_history(promo):
    with pytest.raises(GateError) as exc:
        promo.promote("offline-copy", actor="Promoter", rationale="trust me")
    assert exc.value.code == "no_passing_evaluation"
    with pytest.raises(GateError) as exc:
        promo.rollback(actor="Promoter", rationale="undo nothing")
    assert exc.value.code == "no_promotion_history"
    assert promo.default_profile_id() == "offline"


def _no_op_scripts(scripts):
    """The weak candidate's scripts: the order-coupon repair with
    its first edit turned into a no-op — the anchor is found, the
    edit lands, the feature does not arrive, and grading fails.
    A plausible-looking run that does not do the work."""
    copied = {
        task_id: [dict(edit) for edit in edits]
        for task_id, edits in (scripts or {}).items()
    }
    edits = copied.get("order-coupon")
    if edits:
        edits[0]["new"] = edits[0]["old"]
    return copied


def test_regression_against_the_baseline_refuses(promo, monkeypatch):
    baseline = promo.evaluate_profile("offline", actor="Evaluator", times=1)
    assert baseline["worst_successes"] == 2

    original = service_module.make_model

    def weak_make_model(profile_id, scripts=None, **kwargs):
        if profile_id == "offline-weak":
            return OfflineScriptedModel(scripts=_no_op_scripts(scripts))
        return original(profile_id, scripts=scripts, **kwargs)

    monkeypatch.setattr(service_module, "make_model", weak_make_model)
    weak = promo.evaluate_profile("offline-weak", actor="Evaluator", times=1)
    # The weak candidate's failure is honest work undone, not a
    # zero-tolerance violation: its evaluation stands, scored 1/2 —
    # and 1/2 against a 2/2 baseline is a regression.
    assert weak["passed"] is True
    assert weak["successes_per_run"] == [1]
    (bench,) = promo.list_benchmarks("offline-weak")
    assert bench["benchmark_id"] == weak["benchmark_ids"][0]
    outcomes = {o["task_id"]: o for o in bench["outcomes"]}
    assert outcomes["order-coupon"]["failure_class"] == "run_failed"
    assert outcomes["bulk-tier-boundary"]["verified_success"] is True

    with pytest.raises(GateError) as exc:
        promo.promote("offline-weak", actor="Promoter", rationale="cheaper")
    assert exc.value.code == "regression"
    assert promo.default_profile_id() == "offline"


def test_evaluation_against_a_moved_set_is_stale(tmp_path):
    settings = promo_settings(tmp_path)
    service_a = CodingService(settings)
    service_a.evaluate_profile("offline-copy", actor="Evaluator", times=1)

    moved = tmp_path / "gold-moved"
    shutil.copytree(GOLD_DIR, moved)
    overlay = moved / "broken" / "bulk-tier-boundary" / "workshop" / "pricing.py"
    overlay.write_text(overlay.read_text() + "\n# the set moved\n")

    service_b = CodingService(
        promo_settings(
            tmp_path,
            sqlite_path=settings.sqlite_path,
            gold_dir=moved,
            targets_dir=tmp_path / "targets-b",
            runs_dir=tmp_path / "runs-b",
            artifacts_dir=tmp_path / "artifacts-b",
        )
    )
    with pytest.raises(GateError) as exc:
        service_b.promote(
            "offline-copy", actor="Promoter", rationale="yesterday's evidence"
        )
    assert exc.value.code == "evaluation_stale"


# -- the API surface ------------------------------------------------------------


def test_promotion_api(tmp_path):
    settings = promo_settings(tmp_path)
    service = CodingService(settings)
    service.evaluate_profile("offline", actor="Evaluator", times=1)
    service.evaluate_profile("offline-copy", actor="Evaluator", times=1)

    client = TestClient(create_app(settings))
    readiness = client.get("/readiness").json()
    assert readiness["model_profile"] == "offline"
    assert readiness["default_model_profile"] == "offline"

    promoted = client.post(
        "/api/promotions/promote",
        json={
            "profile_id": "offline-copy",
            "actor": "Promoter",
            "rationale": "equal evidence, new default",
        },
    )
    assert promoted.status_code == 200
    assert promoted.json()["default_profile_id"] == "offline-copy"

    state = client.get("/api/promotions").json()
    assert state["default_profile_id"] == "offline-copy"
    assert state["configured_profile_id"] == "offline"
    assert len(state["evaluations"]) == 2
    assert len(state["events"]) == 1
    assert state["sets"][SET_HELD_OUT]["tasks"] == list(BENCHMARK_SETS[SET_HELD_OUT])

    benchmarks = client.get("/api/benchmarks").json()
    assert len(benchmarks) == 2
    assert all(b["successes"] == 2 for b in benchmarks)

    rolled = client.post(
        "/api/promotions/rollback",
        json={"actor": "Promoter", "rationale": "hold off"},
    )
    assert rolled.status_code == 200
    assert rolled.json()["default_profile_id"] == "offline"
    assert client.get("/readiness").json()["default_model_profile"] == "offline"
