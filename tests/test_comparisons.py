"""Round 8: the shortlist comparison.

A comparison is a stored record produced by running EVERY
shortlist profile over the SAME benchmark set through the round-7
benchmark path — one benchmark per profile, all under one
comparison actor. A profile that cannot serve in this environment
is recorded ``not_servable`` with the named reason, never skipped
and never faked. The report is a pure fold over the stored
records: the per-task matrix, worst/mean successes, total usage,
priced cost and cost per successful task where the price table
prices the slug (unpriced folds labelled, never zero), failure
classes, and each profile's promotion standing read from the
gate's records. The report ranks nothing, and producing a
comparison never changes the default.

Run budget, stated honestly: the main drive runs the tuning set
(4 tasks) under three servable profiles — offline, a copy of it,
and a fake-transport live profile — 12 real gold runs. The fold's
arithmetic (worst/mean over multiple benchmarks, cost per
success, standing states, staleness marking) is proven at unit
level on synthetic records with no runs at all, the staleness
service test uses a shortlist of one keyless profile (zero
benchmarks, zero runs), and the API test drives the held-out set
(2 tasks) under one servable profile.
"""

from __future__ import annotations

import copy
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from open_model_coding_agent.api import create_app
from open_model_coding_agent.baseten import HttpResponse
from open_model_coding_agent.benchmarks import BENCHMARK_SETS, task_set_hash
from open_model_coding_agent.comparisons import build_report
from open_model_coding_agent.config import load_settings
from open_model_coding_agent.errors import GateError
from open_model_coding_agent.gold import GoldSuite
from open_model_coding_agent.prices import PriceTable
from open_model_coding_agent.service import CodingService

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLD_DIR = REPO_ROOT / "evals" / "gold"
FAKE_SLUG = "acme/TestCoder-1"
# Per fake-transport call: 120 prompt + 40 completion tokens at
# $0.30/$0.90 per million = $0.000072 exactly (the R6 shape).
CALL_COST = Decimal("0.000072")


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for name in (
        "BASETEN_API_KEY",
        "BASETEN_MODEL",
        "BASETEN_BASE_URL",
        "KEYLESS_LIVE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


# -- temp shortlists ---------------------------------------------------------


def _record(folder: Path, record: dict) -> None:
    (folder / f"{record['profile_id']}.json").write_text(json.dumps(record))


def write_profiles(folder: Path, *, fake: bool = True, copy_: bool = True) -> Path:
    """A temp shortlist: the offline profile, optionally a copy of
    it, optionally a live profile served by a fake transport, and
    always a live profile whose key env is never set — the
    not_servable control. The keyless profile names its OWN key
    env, so setting the fake profile's key cannot accidentally
    make it servable."""
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "profiles" / "offline.json", folder / "offline.json")
    if copy_:
        _record(
            folder,
            {
                "profile_id": "offline-copy",
                "version": 1,
                "label": "Offline scripted model — copy",
                "kind": "offline",
            },
        )
    if fake:
        _record(
            folder,
            {
                "profile_id": "fake-live",
                "version": 1,
                "label": "Fake-transport live profile",
                "kind": "baseten",
                "base_url": "https://inference.example.test/v1",
                "api_key_env": "BASETEN_API_KEY",
                "model": FAKE_SLUG,
                "allowed_models": [FAKE_SLUG],
                "allowed_model_prefixes": ["acme/"],
            },
        )
    _record(
        folder,
        {
            "profile_id": "keyless-live",
            "version": 1,
            "label": "Live profile with no key in this environment",
            "kind": "baseten",
            "base_url": "https://inference.example.test/v1",
            "api_key_env": "KEYLESS_LIVE_KEY",
            "model": "other-org/Keyless-1",
            "allowed_models": ["other-org/Keyless-1"],
            "allowed_model_prefixes": ["other-org/"],
        },
    )
    return folder


def write_prices(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "table-v1.json").write_text(
        json.dumps(
            {
                "version": 1,
                "currency": "USD",
                "as_of": "2026-10-19",
                "source": "test-recorded prices",
                "prices": {
                    FAKE_SLUG: {
                        "input_per_mtok": "0.30",
                        "output_per_mtok": "0.90",
                    }
                },
            }
        )
    )
    return folder


class FakeTransport:
    def __init__(self, script):
        self.script = list(script)

    def post(self, url, *, headers, json_body, timeout):
        return self.script.pop(0)


def _chat(content=None, tool_calls=None):
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {
        "choices": [{"message": message}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "total_tokens": 160,
        },
    }
    return HttpResponse(200, json.dumps(body))


def _tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def fake_script(set_name: str):
    """The fake live profile's wire script, in benchmark order:
    per task a plan response, one batched tool-call response
    carrying the task's repair, and a closing message — the
    recorded shape the R2/R6 legs replay."""
    suite = GoldSuite(GOLD_DIR)
    script = []
    for task_id in BENCHMARK_SETS[set_name]:
        task = suite.get(task_id)
        assert task is not None
        script.append(
            _chat(
                content=json.dumps(
                    {"approach": task.plan_approach, "steps": task.plan_steps}
                )
            )
        )
        calls = [_tool_call("c-read", "read_file", {"path": task.file_scope[0]})]
        for index, edit in enumerate(task.scripted_edits):
            calls.append(
                _tool_call(
                    f"c-edit-{index}",
                    "edit_file",
                    {
                        "path": edit["path"],
                        "old": edit["old"],
                        "new": edit["new"],
                    },
                )
            )
        script.append(_chat(tool_calls=calls))
        script.append(_chat(content="the repair is complete"))
    return script


def comparison_settings(tmp_path: Path, set_name: str = "tuning", **overrides):
    kwargs = dict(
        sqlite_path=str(tmp_path / "codingagent.db"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        profiles_dir=write_profiles(tmp_path / "profiles"),
        prices_dir=write_prices(tmp_path / "prices"),
        model_transport=FakeTransport(fake_script(set_name)),
    )
    kwargs.update(overrides)
    return load_settings(**kwargs)


def _by_profile(report: dict) -> dict:
    return {p["profile_id"]: p for p in report["profiles"]}


# -- the comparison drive ------------------------------------------------------


def test_comparison_runs_the_whole_shortlist_over_one_set(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    service = CodingService(comparison_settings(tmp_path))
    assert service.default_profile_id() == "offline"

    report = service.run_comparison(set_name="tuning", actor="Comparer")
    rows = _by_profile(report)

    # Every recorded profile is on the comparison, over the same
    # set, named with the set's content hash.
    assert set(rows) == {"offline", "offline-copy", "fake-live", "keyless-live"}
    assert report["set_name"] == "tuning"
    assert report["tasks"] == list(BENCHMARK_SETS["tuning"])
    assert report["task_set_hash"] == task_set_hash(service.gold, "tuning")
    assert report["stale"] is False
    assert report["actor"] == "Comparer"

    # The keyless live profile is recorded not_servable with the
    # named reason — not skipped, and with no benchmark behind it.
    keyless = rows["keyless-live"]
    assert keyless["status"] == "not_servable"
    assert "KEYLESS_LIVE_KEY" in keyless["reason"]
    assert keyless["benchmark_ids"] == []
    assert keyless["successes"] is None
    assert keyless["failure_classes"] == {"not_servable": 1}
    assert service.list_benchmarks("keyless-live") == []

    # The three servable profiles each ran the real benchmark
    # path once, and the matrix shows every task verified.
    stored = service.store.get_comparison(report["comparison_id"])
    assert stored is not None and len(stored["entries"]) == 4
    assert len(service.list_benchmarks()) == 3
    for profile_id in ("offline", "offline-copy", "fake-live"):
        row = rows[profile_id]
        assert row["status"] == "benchmarked"
        assert len(row["benchmark_ids"]) == 1
        assert row["successes"] == 4
        assert row["worst_successes"] == 4
        assert row["mean_successes"] == 4.0
        assert row["failure_classes"] == {}
        assert row["violations"] == 0
        assert [t["task_id"] for t in row["per_task"]] == report["tasks"]
        assert all(
            t["verified_successes"] == 1 and t["attempts"] == 1
            for t in row["per_task"]
        )

    # Usage and money: the fake live profile spent 3 calls per
    # task (plan + batched tools + finish), all priced; the
    # offline profiles fold as scripted accounting, labelled,
    # with no cost per success stated — never a zero price.
    fake = rows["fake-live"]
    assert fake["usage"]["calls"] == 12
    assert fake["usage"]["prompt_tokens"] == 12 * 120
    assert fake["usage"]["completion_tokens"] == 12 * 40
    assert fake["usage"]["total_tokens"] == 12 * 160
    assert fake["cost"]["pricing"] == "priced"
    assert Decimal(fake["cost"]["priced_cost"]) == CALL_COST * 12
    assert Decimal(fake["cost"]["cost_per_successful_task"]) == (
        CALL_COST * 12
    ) / 4
    for profile_id in ("offline", "offline-copy"):
        cost = rows[profile_id]["cost"]
        assert cost["pricing"] == "scripted"
        assert cost["cost_per_successful_task"] is None
        assert cost["unpriced_reasons"] == ["scripted_accounting"]
        assert cost["scripted_chars"] > 0

    # Promotion standing is read from the gate's records: nobody
    # here has been evaluated, and offline stands as the incumbent
    # default. The note on the report's face names the gate as
    # the only path that changes that.
    for profile_id, row in rows.items():
        assert row["promotion"]["evaluation_state"] == "none"
        assert row["promotion"]["is_default"] is (profile_id == "offline")
    assert "promotion gate is the only path" in report["note"]
    assert "does not rank" in report["note"]

    # Producing the comparison changed nothing governed: the
    # default did not move, no promotion events exist, and
    # re-reading the comparison folds the identical report.
    assert service.default_profile_id() == "offline"
    assert service.store.list_promotion_events() == []
    assert service.get_comparison(report["comparison_id"]) == report
    assert [c["comparison_id"] for c in service.list_comparisons()] == [
        report["comparison_id"]
    ]


def test_comparison_refuses_an_unknown_set(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    service = CodingService(comparison_settings(tmp_path))
    with pytest.raises(GateError) as exc:
        service.run_comparison(set_name="everything", actor="Comparer")
    assert exc.value.code == "unknown_benchmark_set"
    assert service.store.list_comparisons() == []


# -- the pure fold, on synthetic records (no runs) ------------------------------


def _outcome(task_id, *, success, failure=None):
    return {
        "task_id": task_id,
        "work_order_id": f"wo-{task_id}",
        "run_id": f"run-{task_id}",
        "status": "succeeded" if success else "failed",
        "verified_success": success,
        "out_of_scope_files": [],
        "failure_class": failure,
        "detail": "",
        "usage": {
            "calls": 2,
            "prompt_tokens": 240,
            "completion_tokens": 80,
            "latency_ms": 5,
        },
        "cost": {"priced_cost": "0", "unpriced_tokens": 0, "scripted_chars": 0},
    }


def _usage_row(work_order_id, *, prompt=120, completion=40):
    return {
        "run_id": "run-x",
        "work_order_id": work_order_id,
        "profile_id": "fake-live",
        "model": FAKE_SLUG,
        "source": "api",
        "unit": "tokens",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "latency_ms": 3,
        "price_table_version": 1,
    }


def test_the_fold_counts_worst_mean_matrix_failures_and_cost():
    tasks = list(BENCHMARK_SETS["tuning"])
    benches = [
        {
            "benchmark_id": "bench-1",
            "profile_id": "fake-live",
            "successes": 3,
            "violations": 0,
            "outcomes": [
                _outcome(t, success=(t != tasks[3]), failure=None if t != tasks[3] else "run_failed")
                for t in tasks
            ],
        },
        {
            "benchmark_id": "bench-2",
            "profile_id": "fake-live",
            "successes": 1,
            "violations": 1,
            "outcomes": [
                _outcome(t, success=(t == tasks[0]), failure=None if t == tasks[0] else "run_failed")
                for t in tasks
            ],
        },
    ]
    # One usage row per work order per benchmark — the fold joins
    # rows to profiles through the outcomes' work orders; the two
    # benchmarks name distinct orders for the same task ids.
    usage_rows = []
    for bench in benches:
        for outcome in bench["outcomes"]:
            outcome["work_order_id"] = f"{bench['benchmark_id']}-{outcome['task_id']}"
            usage_rows.append(_usage_row(outcome["work_order_id"]))
    comparison = {
        "comparison_id": "cmp-x",
        "set_name": "tuning",
        "task_set_hash": "hash-then",
        "tasks": tasks,
        "actor": "Comparer",
        "created_at": "2026-10-19T00:00:00+00:00",
        "note": "note",
        "entries": [
            {
                "profile_id": "fake-live",
                "model": FAKE_SLUG,
                "status": "benchmarked",
                "benchmark_ids": ["bench-1", "bench-2"],
                "reason": None,
            }
        ],
    }
    tables = {
        1: PriceTable(
            version=1,
            currency="USD",
            as_of="2026-10-19",
            source="unit-test table",
            prices={FAKE_SLUG: (Decimal("0.30"), Decimal("0.90"))},
        )
    }
    before = copy.deepcopy(comparison)
    report = build_report(
        comparison,
        benchmarks=benches,
        usage_rows=usage_rows,
        price_tables=tables,
        evaluations=[],
        default_profile_id="offline",
        current_set_hash="hash-then",
        held_out_hash="held-hash",
    )
    (row,) = report["profiles"]
    assert row["successes"] == 4  # 3 + 1 across the two benchmarks
    assert row["worst_successes"] == 1
    assert row["mean_successes"] == 2.0
    matrix = {t["task_id"]: t for t in row["per_task"]}
    assert matrix[tasks[0]]["verified_successes"] == 2
    assert matrix[tasks[0]]["attempts"] == 2
    assert matrix[tasks[3]]["verified_successes"] == 0
    assert row["failure_classes"] == {"run_failed": 4}
    assert row["violations"] == 1
    assert row["usage"]["calls"] == 8
    assert row["usage"]["total_tokens"] == 8 * 160
    # 8 priced calls over 4 verified successes.
    assert Decimal(row["cost"]["priced_cost"]) == CALL_COST * 8
    assert Decimal(row["cost"]["cost_per_successful_task"]) == (
        CALL_COST * 8
    ) / 4
    # The fold is pure: the comparison record it read is untouched.
    assert comparison == before
    assert report["stale"] is False


def test_the_fold_marks_stale_and_reads_promotion_standing():
    tasks = list(BENCHMARK_SETS["tuning"])
    comparison = {
        "comparison_id": "cmp-y",
        "set_name": "tuning",
        "task_set_hash": "hash-then",
        "tasks": tasks,
        "actor": "Comparer",
        "created_at": "2026-10-19T00:00:00+00:00",
        "note": "note",
        "entries": [
            {
                "profile_id": pid,
                "model": None,
                "status": "not_servable",
                "benchmark_ids": [],
                "reason": "no key",
            }
            for pid in ("passed-p", "failed-p", "stale-p", "plain-p")
        ],
    }
    evaluations = [
        {
            "evaluation_id": "eval-passed",
            "profile_id": "passed-p",
            "set_name": "held-out",
            "task_set_hash": "held-hash",
            "actor": "Evaluator",
            "worst_successes": 2,
            "passed": True,
        },
        {
            "evaluation_id": "eval-failed",
            "profile_id": "failed-p",
            "set_name": "held-out",
            "task_set_hash": "held-hash",
            "actor": "Evaluator",
            "worst_successes": 0,
            "passed": False,
        },
        {
            "evaluation_id": "eval-stale",
            "profile_id": "stale-p",
            "set_name": "held-out",
            "task_set_hash": "held-hash-then",
            "actor": "Evaluator",
            "worst_successes": 2,
            "passed": True,
        },
    ]
    report = build_report(
        comparison,
        benchmarks=[],
        usage_rows=[],
        price_tables={},
        evaluations=evaluations,
        default_profile_id="passed-p",
        current_set_hash="hash-now",
        held_out_hash="held-hash",
    )
    # The set moved after the comparison was recorded: stale on
    # read, with both hashes shown.
    assert report["stale"] is True
    assert report["task_set_hash"] == "hash-then"
    assert report["current_task_set_hash"] == "hash-now"
    rows = _by_profile(report)
    assert rows["passed-p"]["promotion"] == {
        "evaluation_state": "passed",
        "evaluation_id": "eval-passed",
        "worst_successes": 2,
        "is_default": True,
    }
    assert rows["failed-p"]["promotion"]["evaluation_state"] == "failed"
    assert rows["stale-p"]["promotion"]["evaluation_state"] == "stale"
    assert rows["plain-p"]["promotion"]["evaluation_state"] == "none"
    assert rows["plain-p"]["promotion"]["is_default"] is False


# -- staleness on read, at service level (zero benchmarks) ---------------------


def test_a_comparison_recorded_before_a_task_edit_reads_stale(tmp_path):
    settings = load_settings(
        sqlite_path=str(tmp_path / "codingagent.db"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        profiles_dir=write_profiles(
            tmp_path / "profiles", fake=False, copy_=False
        ),
        prices_dir=write_prices(tmp_path / "prices"),
    )
    # The shortlist is one keyless live profile plus offline —
    # restrict to the keyless profile only by using a directory
    # holding just it, so the comparison records zero benchmarks.
    only_keyless = tmp_path / "profiles-keyless"
    only_keyless.mkdir()
    shutil.copy(
        tmp_path / "profiles" / "keyless-live.json",
        only_keyless / "keyless-live.json",
    )
    settings.profiles_dir = only_keyless
    service = CodingService(settings)
    report = service.run_comparison(set_name="held-out", actor="Comparer")
    assert report["stale"] is False
    (row,) = report["profiles"]
    assert row["status"] == "not_servable"
    original_hash = report["task_set_hash"]

    moved = tmp_path / "gold-moved"
    shutil.copytree(GOLD_DIR, moved)
    overlay = moved / "broken" / "bulk-tier-boundary" / "workshop" / "pricing.py"
    overlay.write_text(overlay.read_text() + "\n# the set moved\n")
    service_b = CodingService(
        load_settings(
            sqlite_path=settings.sqlite_path,
            targets_dir=tmp_path / "targets-b",
            runs_dir=tmp_path / "runs-b",
            artifacts_dir=tmp_path / "artifacts-b",
            # The full temp shortlist (offline + keyless), so the
            # configured profile resolves; the report fold reads
            # the comparison record, not the directory.
            profiles_dir=tmp_path / "profiles",
            prices_dir=tmp_path / "prices",
            gold_dir=moved,
        )
    )
    reread = service_b.get_comparison(report["comparison_id"])
    assert reread["stale"] is True
    assert reread["task_set_hash"] == original_hash
    assert reread["current_task_set_hash"] != original_hash
    # Marked on read, not rewritten: the stored record still
    # carries the hash it was recorded under.
    stored = service_b.store.get_comparison(report["comparison_id"])
    assert stored["task_set_hash"] == original_hash


# -- the API surface -------------------------------------------------------------


def test_comparison_api(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "profiles" / "offline.json", profiles / "offline.json")
    _record(
        profiles,
        {
            "profile_id": "keyless-live",
            "version": 1,
            "label": "Live profile with no key in this environment",
            "kind": "baseten",
            "base_url": "https://inference.example.test/v1",
            "api_key_env": "KEYLESS_LIVE_KEY",
            "model": "other-org/Keyless-1",
            "allowed_models": ["other-org/Keyless-1"],
            "allowed_model_prefixes": ["other-org/"],
        },
    )
    settings = load_settings(
        sqlite_path=str(tmp_path / "codingagent.db"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        profiles_dir=profiles,
    )
    client = TestClient(create_app(settings))

    refused = client.post(
        "/api/comparisons", json={"set_name": "everything", "actor": "API"}
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "unknown_benchmark_set"

    created = client.post(
        "/api/comparisons", json={"set_name": "held-out", "actor": "API Tester"}
    )
    assert created.status_code == 201
    report = created.json()
    rows = _by_profile(report)
    assert rows["offline"]["successes"] == 2
    assert rows["offline"]["promotion"]["is_default"] is True
    assert rows["keyless-live"]["status"] == "not_servable"
    assert "KEYLESS_LIVE_KEY" in rows["keyless-live"]["reason"]
    assert "promotion gate is the only path" in report["note"]

    listed = client.get("/api/comparisons")
    assert listed.status_code == 200
    assert [c["comparison_id"] for c in listed.json()] == [
        report["comparison_id"]
    ]
    fetched = client.get(f"/api/comparisons/{report['comparison_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == report
    missing = client.get("/api/comparisons/cmp-nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "unknown_comparison"

    # The comparison moved nothing at the gate.
    state = client.get("/api/promotions").json()
    assert state["default_profile_id"] == "offline"
    assert state["events"] == []
