"""Live wiring: the shortlist refreshed against the real catalog.

Round 2 shipped profiles and a price table from the provider's
documentation; this round refreshes both against Baseten's Model
APIs catalog read live on 2026-10-19. The defaults move to the
slugs that read verified, the allowlists shrink to exactly those
slugs (an override pinning a retired slug refuses), the five
verified prices land in price table v2 (v1 untouched), and the
Qwen profile — whose model the catalog does not list — is recorded
not servable with its reason, so every surface names that instead
of discovering a 404 mid-run.

The adapter also grows the reasoning-model request shape: every
call carries the profile record's max_tokens, and a reply that hit
that ceiling with nothing usable fails as model_output_truncated,
never as a malformed plan or a silent finish. And tools/live_smoke
is exercised the way a deployment runs it: keyless, it prints the
named refusal; with a transport injected, it runs a gold task on
the live path and prints the priced evidence.

No test touches the network: the wire is a fake transport, as in
every live-path test in this repo.
"""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from open_model_coding_agent.baseten import BasetenModel, HttpResponse
from open_model_coding_agent.config import REPO_ROOT, load_profile, load_settings
from open_model_coding_agent.errors import ConfigError, GateError
from open_model_coding_agent.gold import GoldSuite
from open_model_coding_agent.prices import (
    current_table,
    fold_usage,
    load_price_tables,
)
from open_model_coding_agent.profiles import apply_overrides, list_profiles

DEEPSEEK = "deepseek-ai/DeepSeek-V4-Flash-0731"


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for name in ("BASETEN_API_KEY", "BASETEN_MODEL", "BASETEN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def _load_smoke_tool():
    spec = importlib.util.spec_from_file_location(
        "live_smoke", REPO_ROOT / "tools" / "live_smoke.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTransport:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    def post(self, url, *, headers, json_body, timeout):
        self.requests.append(
            {"url": url, "headers": headers, "json": json_body, "timeout": timeout}
        )
        return self.script.pop(0)


def chat_response(content=None, tool_calls=None, usage=(120, 40), finish="stop",
                  reasoning=None):
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        },
    }
    return HttpResponse(200, json.dumps(body))


def tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# -- the refreshed records ---------------------------------------------------


def test_the_refreshed_profiles_name_the_verified_catalog():
    profiles = {p.profile_id: p for p in list_profiles(REPO_ROOT / "profiles")}
    deepseek = profiles["baseten-deepseek"]
    assert deepseek.model == DEEPSEEK and deepseek.version == 2
    assert set(deepseek.allowed_models) == {
        "deepseek-ai/DeepSeek-V4-Flash-0731",
        "deepseek-ai/DeepSeek-V4-Pro-0813",
        "deepseek-ai/DeepSeek-V4.1-Flash",
    }
    glm = profiles["baseten-glm"]
    assert glm.model == "zai-org/GLM-5.2" and glm.version == 2
    assert set(glm.allowed_models) == {"zai-org/GLM-5.2", "zai-org/GLM-5.3"}
    assert profiles["baseten-kimi"].model == "moonshotai/Kimi-K3"
    assert profiles["baseten-gpt-oss"].model == "openai/gpt-oss-120b"
    # Every live record carries its completion ceiling as data.
    assert all(
        p.max_tokens == 8192 for p in profiles.values() if p.is_live
    )


def test_retired_slugs_refuse_as_overrides():
    deepseek = load_profile("baseten-deepseek")
    pinned = apply_overrides(deepseek, model="deepseek-ai/DeepSeek-V4-Pro-0813")
    assert pinned.model == "deepseek-ai/DeepSeek-V4-Pro-0813"
    for slug in ("deepseek-ai/DeepSeek-V3.1",):
        with pytest.raises(ConfigError, match="allowlist"):
            apply_overrides(deepseek, model=slug)
    with pytest.raises(ConfigError, match="allowlist"):
        apply_overrides(load_profile("baseten-glm"), model="zai-org/GLM-5")
    with pytest.raises(ConfigError, match="allowlist"):
        apply_overrides(load_profile("baseten-kimi"), model="moonshotai/Kimi-K2.5")


def test_a_retired_slug_pinned_by_env_refuses_at_startup(monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    monkeypatch.setenv("BASETEN_MODEL", "deepseek-ai/DeepSeek-V3.1")
    with pytest.raises(ConfigError, match="allowlist"):
        load_settings(model_profile="baseten-deepseek")


# -- the new record fields, validated -----------------------------------------


def _write_profile(tmp_path, record):
    (tmp_path / "x.json").write_text(json.dumps(record))
    return tmp_path


_BASE_RECORD = {
    "profile_id": "x",
    "version": 1,
    "label": "X",
    "kind": "baseten",
    "base_url": "https://inference.baseten.co/v1",
    "api_key_env": "BASETEN_API_KEY",
    "model": "zai-org/GLM-5.2",
    "allowed_models": ["zai-org/GLM-5.2"],
}


def test_max_tokens_is_governed_data_with_a_default(tmp_path):
    profile = load_profile("x", _write_profile(tmp_path, dict(_BASE_RECORD)))
    assert profile.max_tokens == 8192  # the default, when unrecorded
    profile = load_profile(
        "x", _write_profile(tmp_path, {**_BASE_RECORD, "max_tokens": 4096})
    )
    assert profile.max_tokens == 4096
    for bad in (0, -5, "8192", 1.5, True):
        with pytest.raises(ConfigError, match="max_tokens"):
            load_profile(
                "x", _write_profile(tmp_path, {**_BASE_RECORD, "max_tokens": bad})
            )


def test_a_not_servable_record_must_carry_its_reason(tmp_path):
    with pytest.raises(ConfigError, match="not_servable_reason"):
        load_profile(
            "x", _write_profile(tmp_path, {**_BASE_RECORD, "servable": False})
        )
    with pytest.raises(ConfigError, match="contradicts"):
        load_profile(
            "x",
            _write_profile(
                tmp_path,
                {**_BASE_RECORD, "not_servable_reason": "no reason to refuse"},
            ),
        )
    with pytest.raises(ConfigError, match="boolean"):
        load_profile(
            "x", _write_profile(tmp_path, {**_BASE_RECORD, "servable": "yes"})
        )
    profile = load_profile(
        "x",
        _write_profile(
            tmp_path,
            {
                **_BASE_RECORD,
                "servable": False,
                "not_servable_reason": "the catalog does not list it",
            },
        ),
    )
    assert profile.servable is False
    assert "catalog" in profile.not_servable_reason


# -- qwen: not servable, by name, on every surface ------------------------------


def test_qwen_refuses_at_startup_with_its_recorded_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    with pytest.raises(ConfigError) as exc:
        load_settings(
            sqlite_path=str(tmp_path / "db.sqlite"),
            targets_dir=tmp_path / "targets",
            runs_dir=tmp_path / "runs",
            artifacts_dir=tmp_path / "artifacts",
            model_profile="baseten-qwen",
        )
    # The refusal is servability — the key IS set — and it carries
    # the record's own reason, dated from the catalog read.
    assert "not servable" in str(exc.value)
    assert "no Qwen model" in str(exc.value)


def test_qwen_benchmark_refuses_profile_not_servable(service):
    with pytest.raises(GateError) as exc:
        service.run_benchmark("baseten-qwen", set_name="tuning", actor="tester")
    assert exc.value.code == "profile_not_servable"
    assert "no Qwen model" in exc.value.message


def test_qwen_is_recorded_not_servable_on_a_comparison(tmp_path, settings):
    import shutil

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    shutil.copy(REPO_ROOT / "profiles" / "offline.json", profiles_dir / "offline.json")
    shutil.copy(
        REPO_ROOT / "profiles" / "baseten-qwen.json",
        profiles_dir / "baseten-qwen.json",
    )
    settings.profiles_dir = profiles_dir
    from open_model_coding_agent.service import CodingService

    report = CodingService(settings).run_comparison(
        set_name="held-out", actor="tester"
    )
    (entry,) = [p for p in report["profiles"] if p["profile_id"] == "baseten-qwen"]
    assert entry["status"] == "not_servable"
    assert "no Qwen model" in entry["reason"]
    assert entry["failure_classes"] == {"not_servable": 1}


# -- price table v2 folds -------------------------------------------------------


def _row(model, version, prompt=1_000_000, completion=500_000):
    return {
        "run_id": "run-x",
        "work_order_id": "wo-x",
        "profile_id": "baseten-deepseek",
        "model": model,
        "source": "api",
        "unit": "tokens",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "latency_ms": 10,
        "price_table_version": version,
    }


def test_price_table_v2_prices_deepseek_usage_exactly():
    tables = load_price_tables(REPO_ROOT / "prices")
    assert current_table(tables).version == 2
    fold = fold_usage(
        [
            _row(DEEPSEEK, 2),  # 1M x 0.13 + 0.5M x 0.26 = 0.26
            _row("openai/gpt-oss-120b", 2, prompt=1_000_000, completion=1_000_000),
            _row("deepseek-ai/DeepSeek-V4.1-Flash", 2),  # served, unpriced in v2
            _row("zai-org/GLM-5", 1),  # v1 history: recorded, still unpriced
        ],
        tables,
    )
    assert fold.priced_cost == Decimal("0.26") + Decimal("0.60")
    assert fold.priced_calls == 2
    assert fold.unpriced_calls == 2
    assert fold.price_table_versions == [2]
    assert fold.unpriced_reasons == ["no_price_recorded"]


# -- the reasoning-model request shape -----------------------------------------


def _deepseek_adapter(transport):
    return BasetenModel(
        load_profile("baseten-deepseek"),
        api_key="test-key",
        transport=transport,
        sleep=lambda _seconds: None,
    )


def test_a_truncated_plan_reply_fails_by_name_and_still_accounts_usage():
    transport = FakeTransport(
        [
            chat_response(
                content="",
                reasoning="still reasoning about the plan",
                finish="length",
            )
        ]
    )
    adapter = _deepseek_adapter(transport)
    order = {
        "title": "Fix reservations",
        "task_text": "Stop reservations overcommitting stock.",
        "file_scope": ["workshop/inventory.py"],
        "grading_tests": [],
    }
    with pytest.raises(GateError) as exc:
        adapter.draft_plan(work_order=order, repo_files=["workshop/inventory.py"])
    assert exc.value.code == "model_output_truncated"
    assert "8192" in exc.value.message  # the ceiling to raise is named
    # The truncated call still spent its tokens; they are accounted.
    usage = adapter.drain_usage()
    assert len(usage) == 1 and usage[0]["model"] == DEEPSEEK


def test_a_truncated_step_reply_fails_by_name_too():
    transport = FakeTransport(
        [chat_response(content=None, reasoning="reasoning", finish="length")]
    )
    adapter = _deepseek_adapter(transport)
    adapter.start_run(work_order={}, plan={"approach": "p", "files": [], "tests": []})
    with pytest.raises(GateError) as exc:
        adapter.next_step(transcript=[])
    assert exc.value.code == "model_output_truncated"


def test_length_with_content_is_not_a_truncation_error():
    # finish_reason 'length' but a visible answer arrived: the
    # reply is the model's answer, judged by the usual rules.
    transport = FakeTransport(
        [chat_response(content="stopping here", finish="length")]
    )
    adapter = _deepseek_adapter(transport)
    adapter.start_run(work_order={}, plan={"approach": "p", "files": [], "tests": []})
    step = adapter.next_step(transcript=[])
    assert step.tool == "finish" and step.args["summary"] == "stopping here"


def test_the_wire_ceiling_comes_from_the_profile_record(tmp_path):
    profile = load_profile(
        "x", _write_profile(tmp_path, {**_BASE_RECORD, "max_tokens": 1234})
    )
    transport = FakeTransport(
        [
            chat_response(
                content=json.dumps(
                    {
                        "approach": "a",
                        "steps": [
                            {
                                "intent": "do the work",
                                "files": ["workshop/inventory.py"],
                                "tests": [],
                            }
                        ],
                    }
                )
            )
        ]
    )
    adapter = BasetenModel(
        profile, api_key="test-key", transport=transport, sleep=lambda _s: None
    )
    adapter.draft_plan(
        work_order={
            "title": "t",
            "task_text": "x",
            "file_scope": ["workshop/inventory.py"],
            "grading_tests": [],
        },
        repo_files=[],
    )
    assert transport.requests[0]["json"]["max_tokens"] == 1234
    assert transport.requests[0]["json"]["model"] == "zai-org/GLM-5.2"


# -- the live smoke tool ---------------------------------------------------------


def test_the_smoke_tool_without_a_key_prints_the_named_refusal(capsys):
    smoke = _load_smoke_tool()
    assert smoke.main(["--profile", "baseten-deepseek"]) == 2
    out = capsys.readouterr().out
    assert "refused" in out and "BASETEN_API_KEY" in out
    assert "Traceback" not in out


def _gold_script(settings):
    task = GoldSuite(settings.gold_dir).get("reserve-overcommit")
    calls = [tool_call("c-read", "read_file", {"path": task.file_scope[0]})]
    for index, edit in enumerate(task.scripted_edits):
        calls.append(
            tool_call(
                f"c-edit-{index}",
                "edit_file",
                {"path": edit["path"], "old": edit["old"], "new": edit["new"]},
            )
        )
    return FakeTransport(
        [
            chat_response(
                content=json.dumps(
                    {"approach": task.plan_approach, "steps": task.plan_steps}
                )
            ),
            chat_response(tool_calls=calls),
            chat_response(content="the repair is complete"),
        ]
    )


def test_the_smoke_tool_runs_a_gold_task_and_prices_it(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-deepseek",
    )
    smoke = _load_smoke_tool()
    report = smoke.run_smoke(
        settings=settings, transport=_gold_script(settings)
    )
    assert report["profile_id"] == "baseten-deepseek"
    assert report["model"] == DEEPSEEK
    assert report["plan"]["decision"] == "approved"
    assert report["run_status"] == "succeeded"
    assert report["verification"]["passed"] is True
    assert report["usage"] and all(
        u["model"] == DEEPSEEK for u in report["usage"]
    )
    # Three calls at (120 in, 40 out) under table v2: per call
    # (120 x 0.13 + 40 x 0.26) / 1M = $0.000026 exactly.
    cost = report["cost"]
    assert Decimal(cost["plan"]) == Decimal("0.000026")
    assert Decimal(cost["execution"]) == Decimal("0.000052")
    assert Decimal(cost["total"]) == Decimal("0.000078")
    assert cost["price_table_versions"] == [2]
