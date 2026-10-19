"""Round 6: budgets enforced in the loop, and the cost ledger.

Budgets are ceilings in code, snapshotted onto the run at start: the
call ceiling stops a run before the call that would cross it, the
token ceiling stops it after the in-flight call that crossed it
(never further), the wall ceiling reads the run's active seconds.
A budget-stopped run lands INTERRUPTED, refuses resume by the
budget's own name, and restarts into a fresh budget.

The ledger prices usage with the governed price table — versioned
records, loaded fail-closed, Decimal throughout. Each row is priced
under the table version stamped on it. Unpriced slugs are counted,
never zeroed; the scripted model's character accounting is never
priced as tokens.
"""

from __future__ import annotations

import itertools
import json
from decimal import Decimal
from pathlib import Path

import pytest

from open_model_coding_agent.baseten import HttpResponse
from open_model_coding_agent.config import Policy, load_settings
from open_model_coding_agent.errors import ConfigError, GateError
from open_model_coding_agent.prices import fold_usage, load_price_tables
from open_model_coding_agent.service import CodingService

GLM = "zai-org/GLM-5"
# Per call in the live scripts below: 120 prompt + 40 completion
# tokens at $0.30 / $0.90 per million = $0.000072 exactly.
CALL_COST = Decimal("0.000072")


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for name in ("BASETEN_API_KEY", "BASETEN_MODEL", "BASETEN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def write_table(prices_dir: Path, version: int, prices: dict) -> Path:
    prices_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "version": version,
        "currency": "USD",
        "as_of": "2026-10-19",
        "source": "test-recorded prices",
        "prices": prices,
    }
    path = prices_dir / f"table-v{version}.json"
    path.write_text(json.dumps(record))
    return path


def priced_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "prices"
    write_table(folder, 1, {GLM: {"input_per_mtok": "0.30", "output_per_mtok": "0.90"}})
    return folder


class FakeTransport:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    def post(self, url, *, headers, json_body, timeout):
        self.requests.append({"url": url, "json": json_body})
        return self.script.pop(0)


def tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def chat_response(content=None, tool_calls=None, usage=(120, 40)):
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {
        "choices": [{"message": message}],
        "usage": {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        },
    }
    return HttpResponse(200, json.dumps(body))


def live_service(tmp_path, monkeypatch, script, **settings_kwargs):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-glm",
        model_transport=FakeTransport(script),
        **settings_kwargs,
    )
    return CodingService(settings)


def gold_script(service, *, succeed: bool = True):
    """Plan, then (for a success) the repair as one batched tool-call
    response, then a closing message — each response carrying the
    provider usage shape at (120, 40)."""
    task = service.gold.get("reserve-overcommit")
    script = [
        chat_response(
            content=json.dumps(
                {"approach": task.plan_approach, "steps": task.plan_steps}
            )
        )
    ]
    if succeed:
        calls = [tool_call("c-read", "read_file", {"path": task.file_scope[0]})]
        for index, edit in enumerate(task.scripted_edits):
            calls.append(
                tool_call(
                    f"c-edit-{index}",
                    "edit_file",
                    {"path": edit["path"], "old": edit["old"], "new": edit["new"]},
                )
            )
        script.append(chat_response(tool_calls=calls))
    script.append(chat_response(content="the work is done"))
    return script


def run_gold(service):
    order = service.create_gold_work_order("reserve-overcommit")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    return wid, service.start_run(wid)


# -- the price table ---------------------------------------------------------


def test_the_shipped_table_records_the_shortlist_unpriced():
    from open_model_coding_agent.config import REPO_ROOT

    tables = load_price_tables(REPO_ROOT / "prices")
    assert set(tables) == {1}
    table = tables[1]
    assert table.currency == "USD"
    assert "baseten.co" in table.source
    # Every shortlist slug is recorded; none carries a price this
    # repo has not verified — unverified is null, never invented.
    assert set(table.prices) == {
        "zai-org/GLM-5",
        "moonshotai/Kimi-K2.5",
        "deepseek-ai/DeepSeek-V3.1",
        "Qwen/Qwen3-Coder-480B-A35B-Instruct",
        "openai/gpt-oss-120b",
    }
    assert all(price is None for price in table.prices.values())
    assert table.price_for("some-org/Unlisted-1") is None  # unpriced, not an error


def test_the_price_loader_refuses_records_it_does_not_understand(tmp_path):
    folder = tmp_path / "prices"
    good = {GLM: {"input_per_mtok": "0.30", "output_per_mtok": "0.90"}}

    with pytest.raises(ConfigError, match="no price table"):
        load_price_tables(folder)

    write_table(folder, 1, {GLM: {"input_per_mtok": 0.30, "output_per_mtok": "0.90"}})
    with pytest.raises(ConfigError, match="decimal strings"):
        load_price_tables(folder)

    write_table(folder, 1, {GLM: {"input_per_mtok": "0.30", "output_per_mtok": None}})
    with pytest.raises(ConfigError, match="half-priced"):
        load_price_tables(folder)

    write_table(folder, 1, {GLM: {"input_per_mtok": "-0.30", "output_per_mtok": "0.90"}})
    with pytest.raises(ConfigError, match="non-negative"):
        load_price_tables(folder)

    write_table(folder, 1, good)
    record = json.loads((folder / "table-v1.json").read_text())
    record["surprise"] = 1
    (folder / "table-v1.json").write_text(json.dumps(record))
    with pytest.raises(ConfigError, match="unknown field"):
        load_price_tables(folder)

    record["surprise"] = None
    del record["surprise"]
    record["version"] = 2
    (folder / "table-v1.json").write_text(json.dumps(record))
    with pytest.raises(ConfigError, match="must agree"):
        load_price_tables(folder)


def _row(**overrides):
    row = {
        "run_id": "run-x",
        "work_order_id": "wo-x",
        "profile_id": "baseten-glm",
        "model": GLM,
        "source": "api",
        "unit": "tokens",
        "prompt_tokens": 1_000_000,
        "completion_tokens": 500_000,
        "latency_ms": 10,
        "price_table_version": 1,
    }
    row.update(overrides)
    return row


def test_the_fold_prices_each_row_under_its_own_version(tmp_path):
    folder = tmp_path / "prices"
    write_table(folder, 1, {GLM: {"input_per_mtok": "0.30", "output_per_mtok": "0.90"}})
    write_table(folder, 2, {GLM: {"input_per_mtok": "0.60", "output_per_mtok": "0.90"}})
    tables = load_price_tables(folder)

    fold = fold_usage(
        [
            _row(),  # v1: 0.30 + 0.45 = 0.75
            _row(price_table_version=2),  # v2: 0.60 + 0.45 = 1.05
            _row(model="other-org/Model-1"),  # unknown slug: unpriced
            _row(price_table_version=None),  # recorded before stamping
            _row(price_table_version=99),  # version no longer on disk
            _row(source="scripted", unit="chars", model="offline-scripted",
                 prompt_tokens=100, completion_tokens=50),
            # A scripted row naming a PRICED slug still folds as
            # scripted: the fold keys on the row's own label, never
            # the model name.
            _row(source="scripted", unit="chars",
                 prompt_tokens=10, completion_tokens=5),
        ],
        tables,
    )
    assert fold.priced_cost == Decimal("0.75") + Decimal("1.05")
    assert fold.priced_calls == 2 and fold.priced_tokens == 3_000_000
    assert fold.unpriced_calls == 3 and fold.unpriced_tokens == 4_500_000
    assert fold.scripted_calls == 2 and fold.scripted_chars == 165
    assert fold.price_table_versions == [1, 2]
    assert fold.unpriced_reasons == [
        "no_price_recorded",
        "price_table_version_missing",
        "price_table_version_unrecorded",
        "scripted_accounting",
    ]


# -- policy ------------------------------------------------------------------


def test_budget_ceilings_load_from_the_environment(monkeypatch):
    monkeypatch.setenv("CODINGAGENT_MAX_MODEL_CALLS", "7")
    monkeypatch.setenv("CODINGAGENT_MAX_TOKENS", "123456")
    monkeypatch.setenv("CODINGAGENT_MAX_WALL_SECONDS", "90")
    settings = load_settings()
    assert settings.policy.max_model_calls == 7
    assert settings.policy.max_tokens == 123456
    assert settings.policy.max_wall_seconds == 90
    monkeypatch.setenv("CODINGAGENT_MAX_TOKENS", "a lot")
    with pytest.raises(ConfigError, match="CODINGAGENT_MAX_TOKENS"):
        load_settings()


# -- enforcement ---------------------------------------------------------------


def test_the_call_ceiling_stops_the_run_and_restart_spends_fresh(settings):
    settings.policy = Policy(max_model_calls=2)
    service = CodingService(settings)
    order = service.create_gold_work_order("reserve-overcommit")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)

    assert run["status"] == "interrupted"
    assert run["budget_stop"] == "model_calls"
    assert "budget_exhausted" in run["interruption_reason"]
    assert run["budget"]["max_model_calls"] == 2  # the snapshot, on the record
    assert service.get_work_order(wid)["status"] == "interrupted"
    stops = [a for a in service.audit(wid) if a["event"] == "budget_exhausted"]
    assert stops and stops[0]["detail"]["ceiling"] == "model_calls"
    assert stops[0]["detail"]["limit"] == 2

    # Resume would overspend the same budget: refused by its name.
    with pytest.raises(GateError) as excinfo:
        service.resume_run(wid, run["run_id"], actor="tester")
    assert excinfo.value.code == "budget_exhausted"
    refusals = [a for a in service.audit(wid) if a["event"] == "resume_refused"]
    assert refusals and refusals[0]["detail"]["basis"] == "budget_exhausted"

    # Restart is the way back: a fresh run, a fresh snapshot.
    settings.policy = Policy(max_model_calls=200)
    fresh = service.restart_run(wid, run["run_id"], actor="tester")
    assert fresh["status"] == "succeeded"
    assert fresh["budget"]["max_model_calls"] == 200
    assert service.store.get_run(run["run_id"])["budget"]["max_model_calls"] == 2


def test_the_token_ceiling_stops_after_the_in_flight_call(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-glm",
        prices_dir=priced_dir(tmp_path),
        policy=Policy(max_tokens=20),
    )
    service = CodingService(settings)
    settings.model_transport = FakeTransport(gold_script(service))
    wid, run = run_gold(service)

    # The first run call carried 160 tokens against a ceiling of 20:
    # the run stopped the moment that call's usage landed, having
    # made no further call and dispatched nothing.
    assert run["status"] == "interrupted"
    assert run["budget_stop"] == "tokens"
    totals = run["usage_totals"]
    spent = totals["prompt_tokens"] + totals["completion_tokens"]
    assert spent == 160
    assert spent <= 20 + 160  # ceiling + the single in-flight call
    assert not [
        a for a in service.audit(wid) if a["event"] == "tool_call"
    ]
    # What the in-flight call cost is on the run's ledger.
    assert Decimal(run["cost"]["execution"]) == CALL_COST
    assert Decimal(run["cost"]["total"]) == CALL_COST * 2  # plan + execution


def test_the_wall_ceiling_reads_active_seconds(settings):
    settings.policy = Policy(max_wall_seconds=60)
    service = CodingService(settings)
    ticks = itertools.chain([100.0, 100.0, 100.0, 5000.0], itertools.repeat(5000.0))
    service._monotonic = lambda: next(ticks)
    order = service.create_gold_work_order("reserve-overcommit")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    assert run["status"] == "interrupted"
    assert run["budget_stop"] == "wall_seconds"
    assert run["elapsed_seconds"] >= 60


# -- the ledger, end to end ----------------------------------------------------


def test_a_priced_run_costs_plan_and_execution_separately(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-glm",
        prices_dir=priced_dir(tmp_path),
    )
    service = CodingService(settings)
    settings.model_transport = FakeTransport(gold_script(service))
    wid, run = run_gold(service)
    assert run["status"] == "succeeded"

    cost = run["cost"]
    assert Decimal(cost["plan"]) == CALL_COST
    assert Decimal(cost["execution"]) == CALL_COST * 2
    assert Decimal(cost["total"]) == CALL_COST * 3
    assert cost["priced_tokens"] == 480 and cost["priced_calls"] == 3
    assert cost["price_table_versions"] == [1]
    # Every row carries the version it was costed under.
    rows = service.store.list_usage(work_order_id=wid)
    assert rows and all(r["price_table_version"] == 1 for r in rows)

    report = service.cost_report()
    (profile,) = report["profiles"]
    assert profile["profile_id"] == "baseten-glm"
    assert profile["runs"] == 1 and profile["successful_tasks"] == 1
    assert Decimal(profile["priced_cost"]) == CALL_COST * 3
    assert Decimal(profile["cost_per_successful_task"]) == CALL_COST * 3


def test_cost_per_successful_task_counts_the_failures_spend(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-glm",
        prices_dir=priced_dir(tmp_path),
    )
    service = CodingService(settings)
    task = service.gold.get("reserve-overcommit")

    settings.model_transport = FakeTransport(gold_script(service))
    _wid_a, run_a = run_gold(service)
    assert run_a["status"] == "succeeded"

    # A second order whose model declares the work done without
    # touching a file: verification fails it (no patch), but its
    # plan and run calls were still spent.
    settings.model_transport = FakeTransport(
        [
            chat_response(
                content=json.dumps(
                    {"approach": task.plan_approach, "steps": task.plan_steps}
                )
            ),
            chat_response(content="nothing to change"),
        ]
    )
    _wid_b, run_b = run_gold(service)
    assert run_b["status"] == "failed"

    report = service.cost_report()
    (profile,) = report["profiles"]
    assert profile["runs"] == 2 and profile["successful_tasks"] == 1
    # A spent 3 calls, B spent 2; the metric divides ALL of it by
    # the one verified success.
    assert Decimal(profile["priced_cost"]) == CALL_COST * 5
    assert Decimal(profile["cost_per_successful_task"]) == CALL_COST * 5


def test_an_unpriced_run_counts_its_tokens_and_never_zeroes_them(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-glm",
        # the shipped table: GLM is recorded, deliberately unpriced
    )
    service = CodingService(settings)
    settings.model_transport = FakeTransport(gold_script(service))
    _wid, run = run_gold(service)
    assert run["status"] == "succeeded"
    cost = run["cost"]
    assert Decimal(cost["total"]) == 0
    assert cost["priced_tokens"] == 0
    assert cost["unpriced_tokens"] == 480  # plan + both run calls
    assert cost["unpriced_reasons"] == ["no_price_recorded"]
    (profile,) = service.cost_report()["profiles"]
    assert Decimal(profile["cost_per_successful_task"]) == 0
    assert profile["unpriced_tokens"] == 480


def test_scripted_accounting_is_never_priced_and_never_trips_the_token_ceiling(
    tmp_path, settings
):
    # A prices dir whose table prices nothing relevant; the point is
    # the fold and the budget both read the rows' own labels.
    folder = tmp_path / "prices"
    write_table(folder, 1, {})
    settings.prices_dir = folder
    settings.policy = Policy(max_tokens=1)
    service = CodingService(settings)
    order = service.create_gold_work_order("reserve-overcommit")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    # Chars are not tokens: a token ceiling of 1 does not stop the
    # offline model, and its accounting never enters the money.
    assert run["status"] == "succeeded"
    assert Decimal(run["cost"]["total"]) == 0
    assert run["cost"]["scripted_chars"] > 0
    assert run["cost"]["unpriced_reasons"] == ["scripted_accounting"]
