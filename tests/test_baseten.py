"""Round 2: the Baseten live adapter, the profile loader, usage.

Everything here runs against a fake transport replaying
recorded-shape responses. No test touches the network — the live
adapter's real endpoint is exercised only by a deployment holding a
key, exactly as the README states.
"""

from __future__ import annotations

import json

import pytest

from open_model_coding_agent.baseten import (
    BasetenModel,
    HttpResponse,
    TransportError,
)
from open_model_coding_agent.config import load_profile, load_settings
from open_model_coding_agent.errors import ConfigError, GateError
from open_model_coding_agent.profiles import apply_overrides, list_profiles


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for name in ("BASETEN_API_KEY", "BASETEN_MODEL", "BASETEN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


class FakeTransport:
    """Replays a script of HttpResponse / exception items, in order,
    and records every request it was asked to make."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    def post(self, url, *, headers, json_body, timeout):
        self.requests.append(
            {"url": url, "headers": headers, "json": json_body, "timeout": timeout}
        )
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def chat_response(content=None, tool_calls=None, usage=(11, 7)):
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


def make_adapter(transport, **kwargs):
    kwargs.setdefault("sleep", lambda _seconds: None)
    return BasetenModel(
        load_profile("baseten-glm"), api_key="test-key", transport=transport, **kwargs
    )


ORDER = {
    "title": "Fix reservations",
    "task_text": "Stop reservations overcommitting stock.",
    "file_scope": ["workshop/inventory.py"],
    "grading_tests": [],
}


# -- the adapter: plan drafting -------------------------------------------


def test_plan_draft_over_the_wire():
    transport = FakeTransport(
        [
            chat_response(
                content=json.dumps(
                    {
                        "approach": "Guard the reservation against availability.",
                        "steps": [
                            {
                                "intent": "Add the availability check to reserve.",
                                "files": ["workshop/inventory.py"],
                                "tests": ["workshop/tests/test_inventory.py"],
                            }
                        ],
                    }
                )
            )
        ]
    )
    adapter = make_adapter(transport)
    draft = adapter.draft_plan(work_order=ORDER, repo_files=["workshop/inventory.py"])
    assert draft.files == ["workshop/inventory.py"]
    assert draft.steps[0].intent == "Add the availability check to reserve."
    assert draft.tests == ["workshop/tests/test_inventory.py"]
    request = transport.requests[0]
    assert request["url"] == "https://inference.baseten.co/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["json"]["model"] == "zai-org/GLM-5"
    assert request["json"]["response_format"] == {"type": "json_object"}
    usage = adapter.drain_usage()
    assert len(usage) == 1
    assert usage[0]["source"] == "api" and usage[0]["unit"] == "tokens"
    assert usage[0]["prompt_tokens"] == 11 and usage[0]["completion_tokens"] == 7
    assert usage[0]["model"] == "zai-org/GLM-5"


def test_malformed_plan_replies_refuse_by_name():
    for content in (
        "not json at all",
        json.dumps({"files": "oops"}),
        json.dumps({"approach": "a", "steps": []}),
        json.dumps({"approach": "a", "steps": [{"files": ["x.py"]}]}),
        "",
    ):
        adapter = make_adapter(FakeTransport([chat_response(content=content)]))
        with pytest.raises(GateError) as exc:
            adapter.draft_plan(work_order=ORDER, repo_files=[])
        assert exc.value.code == "malformed_plan"


# -- the adapter: the step loop ---------------------------------------------


def test_steps_translate_tool_calls_and_fold_results():
    transport = FakeTransport(
        [
            chat_response(tool_calls=[tool_call("c1", "read_file", {"path": "a.py"})]),
            chat_response(
                tool_calls=[
                    tool_call(
                        "c2", "edit_file", {"path": "a.py", "old": "x", "new": "y"}
                    )
                ]
            ),
            chat_response(content="all done"),
        ]
    )
    adapter = make_adapter(transport)
    adapter.start_run(work_order=ORDER, plan={"approach": "p", "files": [], "tests": []})

    step = adapter.next_step(transcript=[])
    assert (step.tool, step.args) == ("read_file", {"path": "a.py"})
    transcript = [{"tool": "read_file", "ok": True, "output": "body", "refused": False}]
    step = adapter.next_step(transcript=transcript)
    assert step.tool == "edit_file"
    # The second request carries the first call's result as a tool message.
    messages = transport.requests[1]["json"]["messages"]
    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert tool_messages and tool_messages[0]["tool_call_id"] == "c1"
    assert "body" in tool_messages[0]["content"]
    # The tools offered on the wire are the shell's tool schemas.
    names = {t["function"]["name"] for t in transport.requests[0]["json"]["tools"]}
    assert names == {
        "list_files", "read_file", "search", "edit_file", "run_tests", "finish",
    }

    transcript.append(
        {"tool": "edit_file", "ok": True, "output": "edited a.py", "refused": False}
    )
    step = adapter.next_step(transcript=transcript)
    assert step.tool == "finish"
    assert len(adapter.drain_usage()) == 3


def test_a_batched_response_queues_its_steps():
    transport = FakeTransport(
        [
            chat_response(
                tool_calls=[
                    tool_call("c1", "list_files", {}),
                    tool_call("c2", "read_file", {"path": "a.py"}),
                ]
            ),
            chat_response(content="done"),
        ]
    )
    adapter = make_adapter(transport)
    adapter.start_run(work_order=ORDER, plan={"approach": "p", "files": [], "tests": []})
    assert adapter.next_step(transcript=[]).tool == "list_files"
    transcript = [{"tool": "list_files", "ok": True, "output": "a.py", "refused": False}]
    assert adapter.next_step(transcript=transcript).tool == "read_file"
    assert len(transport.requests) == 1  # no second call for the queued step
    transcript.append(
        {"tool": "read_file", "ok": True, "output": "body", "refused": False}
    )
    assert adapter.next_step(transcript=transcript).tool == "finish"
    assert len(transport.requests) == 2


def test_non_json_tool_arguments_are_a_named_failure():
    bad = {
        "id": "c1",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{nope"},
    }
    adapter = make_adapter(FakeTransport([chat_response(tool_calls=[bad])]))
    adapter.start_run(work_order=ORDER, plan={"approach": "p", "files": [], "tests": []})
    with pytest.raises(GateError) as exc:
        adapter.next_step(transcript=[])
    assert exc.value.code == "model_response_invalid"


# -- the adapter: errors, bounded retries ------------------------------------


def test_auth_and_unknown_slug_fail_without_retry():
    for status, code in ((401, "model_auth_failed"), (404, "model_unknown_slug")):
        transport = FakeTransport([HttpResponse(status, "{}")])
        adapter = make_adapter(transport)
        with pytest.raises(GateError) as exc:
            adapter.draft_plan(work_order=ORDER, repo_files=[])
        assert exc.value.code == code
        assert len(transport.requests) == 1


def test_rate_limit_retries_then_succeeds():
    sleeps: list[float] = []
    transport = FakeTransport(
        [
            HttpResponse(429, "{}"),
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
            ),
        ]
    )
    adapter = make_adapter(transport, sleep=sleeps.append)
    draft = adapter.draft_plan(work_order=ORDER, repo_files=[])
    assert draft.approach == "a"
    assert len(transport.requests) == 2
    assert len(sleeps) == 1


def test_server_error_exhausts_bounded_retries_by_name():
    transport = FakeTransport([HttpResponse(500, "{}")] * 5)
    adapter = make_adapter(transport, max_retries=2)
    with pytest.raises(GateError) as exc:
        adapter.draft_plan(work_order=ORDER, repo_files=[])
    assert exc.value.code == "model_server_error"
    assert len(transport.requests) == 3  # bounded: first try + 2 retries


def test_unreachable_endpoint_fails_by_name():
    transport = FakeTransport([TransportError("no route")] * 5)
    adapter = make_adapter(transport, max_retries=1)
    with pytest.raises(GateError) as exc:
        adapter.draft_plan(work_order=ORDER, repo_files=[])
    assert exc.value.code == "model_unreachable"
    assert len(transport.requests) == 2


# -- the profile loader --------------------------------------------------------


def test_shipped_profiles_load_and_the_offline_default_is_intact():
    profiles = {p.profile_id: p for p in list_profiles(_profiles_dir())}
    assert set(profiles) == {
        "offline",
        "baseten-glm",
        "baseten-kimi",
        "baseten-deepseek",
        "baseten-qwen",
        "baseten-gpt-oss",
    }
    assert profiles["offline"].kind == "offline"
    assert profiles["baseten-kimi"].model == "moonshotai/Kimi-K2.5"
    assert profiles["baseten-qwen"].model == "Qwen/Qwen3-Coder-480B-A35B-Instruct"


def _profiles_dir():
    from open_model_coding_agent.config import REPO_ROOT

    return REPO_ROOT / "profiles"


def _write_profile(tmp_path, record):
    (tmp_path / "x.json").write_text(json.dumps(record))
    return tmp_path


def test_loader_refuses_records_it_does_not_understand(tmp_path):
    base = {
        "profile_id": "x",
        "version": 1,
        "label": "X",
        "kind": "baseten",
        "base_url": "https://inference.baseten.co/v1",
        "api_key_env": "BASETEN_API_KEY",
        "model": "zai-org/GLM-5",
        "allowed_models": ["zai-org/GLM-5"],
        "allowed_model_prefixes": ["zai-org/"],
    }
    with pytest.raises(ConfigError, match="unknown field"):
        load_profile("x", _write_profile(tmp_path, {**base, "surprise": 1}))
    with pytest.raises(ConfigError, match="allowlist"):
        load_profile(
            "x", _write_profile(tmp_path, {**base, "model": "other-org/Model-1"})
        )
    with pytest.raises(ConfigError, match="allowlist"):
        load_profile("x", _write_profile(tmp_path, {**base, "model": "noslash"}))
    with pytest.raises(ConfigError, match="no live fields"):
        load_profile(
            "x",
            _write_profile(
                tmp_path,
                {
                    "profile_id": "x",
                    "version": 1,
                    "label": "X",
                    "kind": "offline",
                    "model": "zai-org/GLM-5",
                },
            ),
        )


def test_slug_overrides_are_bounded_by_the_profiles_own_allowlist(monkeypatch):
    glm = load_profile("baseten-glm")
    pinned = apply_overrides(glm, model="zai-org/GLM-5")
    assert pinned.model == "zai-org/GLM-5"
    with pytest.raises(ConfigError, match="allowlist"):
        apply_overrides(glm, model="moonshotai/Kimi-K2.5")

    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    monkeypatch.setenv("BASETEN_MODEL", "moonshotai/Kimi-K2.5")
    with pytest.raises(ConfigError, match="allowlist"):
        load_settings(model_profile="baseten-glm")


# -- the service on the live path (fake transport) ----------------------------


def _live_settings(tmp_path, transport, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    return load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-glm",
        model_transport=transport,
    )


def _gold_script(service, task_id):
    """The wire script a competent model would produce for a gold
    task: plan, read the scoped file, apply the repair, finish."""
    task = service.gold.get(task_id)
    script = [
        chat_response(
            content=json.dumps(
                {"approach": task.plan_approach, "steps": task.plan_steps}
            )
        )
    ]
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
    script.append(chat_response(content="the repair is complete"))
    return FakeTransport(script)


def test_a_gold_task_runs_end_to_end_on_the_live_path(tmp_path, monkeypatch):
    from open_model_coding_agent.service import CodingService

    settings = _live_settings(tmp_path, None, monkeypatch)
    service = CodingService(settings)
    # The transport is read when a model is built, not at startup;
    # the wire script needs the suite, so it is slotted in here.
    settings.model_transport = _gold_script(service, "reserve-overcommit")

    order = service.create_gold_work_order("reserve-overcommit")
    wid = order["work_order_id"]
    plan = service.draft_plan(wid)
    assert plan["model_profile"] == "baseten-glm"
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    assert run["status"] == "succeeded"
    assert run["verification"]["passed"] is True

    # Usage: stored per call, folded into the run read, all of it the
    # provider's accounting for this profile and slug.
    detail = service.work_order_detail(wid)
    plan_calls = [u for u in detail["usage"] if u["run_id"] is None]
    assert len(plan_calls) == 1 and plan_calls[0]["source"] == "api"
    run_usage = detail["runs"][0]["usage"]
    assert run_usage and all(u["source"] == "api" for u in run_usage)
    assert all(u["profile_id"] == "baseten-glm" for u in detail["usage"])
    assert all(u["model"] == "zai-org/GLM-5" for u in detail["usage"])
    assert detail["runs"][0]["usage_totals"]["prompt_tokens"] > 0


def _plan_ok_response():
    return chat_response(
        content=json.dumps(
            {
                "approach": "Guard the reservation.",
                "steps": [
                    {
                        "intent": "Add the availability check.",
                        "files": ["workshop/inventory.py"],
                        "tests": [],
                    }
                ],
            }
        )
    )


def test_a_transient_model_failure_mid_run_interrupts_the_run(
    tmp_path, monkeypatch
):
    """Round 5 semantics: a provider that stays down through the
    adapter's retries is the world hiccupping, not the work being
    wrong — the run lands INTERRUPTED (resumable), with the failure
    named in its interruption reason. In round 2 this same shape
    landed FAILED; the recovery round moved transient failures to
    the state that can resume."""
    from open_model_coding_agent.service import CodingService

    transport = FakeTransport([_plan_ok_response()] + [HttpResponse(500, "{}")] * 5)
    settings = _live_settings(tmp_path, transport, monkeypatch)
    service = CodingService(settings)
    order = service.create_gold_work_order("reserve-overcommit")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    assert run["status"] == "interrupted"
    assert "model_server_error" in run["interruption_reason"]
    assert run["failure_reason"] is None
    assert service.get_work_order(wid)["status"] == "interrupted"
    events = [a["event"] for a in service.audit(wid)]
    assert "model_call_failed" in events and "run_interrupted" in events
    assert "run_failed" not in events


def test_a_terminal_model_failure_mid_run_still_fails_the_run(
    tmp_path, monkeypatch
):
    """A rejected key will be rejected again on resume — terminal
    model errors keep round 2's semantics and end the run FAILED."""
    from open_model_coding_agent.service import CodingService

    transport = FakeTransport([_plan_ok_response()] + [HttpResponse(401, "{}")] * 3)
    settings = _live_settings(tmp_path, transport, monkeypatch)
    service = CodingService(settings)
    order = service.create_gold_work_order("reserve-overcommit")
    wid = order["work_order_id"]
    service.draft_plan(wid)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid)
    assert run["status"] == "failed"
    assert "model_auth_failed" in run["failure_reason"]
    assert service.get_work_order(wid)["status"] == "failed"
    events = [a["event"] for a in service.audit(wid)]
    assert "model_call_failed" in events and "run_failed" in events


def test_a_live_plan_outside_the_scope_is_refused_by_the_shared_validator(
    tmp_path, monkeypatch
):
    from open_model_coding_agent.service import CodingService

    transport = FakeTransport(
        [
            chat_response(
                content=json.dumps(
                    {
                        "approach": "Rewrite everything.",
                        "steps": [
                            {
                                "intent": "Rewrite the orders module.",
                                "files": ["workshop/orders.py"],
                                "tests": [],
                            }
                        ],
                    }
                )
            )
        ]
    )
    settings = _live_settings(tmp_path, transport, monkeypatch)
    service = CodingService(settings)
    order = service.create_gold_work_order("reserve-overcommit")
    with pytest.raises(GateError) as exc:
        service.draft_plan(order["work_order_id"])
    assert exc.value.code == "malformed_plan"


def test_offline_usage_is_labelled_scripted(service, approved_order):
    wid = approved_order["work_order_id"]
    run = service.start_run(wid)
    assert run["usage"] and all(u["source"] == "scripted" for u in run["usage"])
    assert all(u["unit"] == "chars" for u in run["usage"])
    detail = service.work_order_detail(wid)
    assert all(u["source"] == "scripted" for u in detail["usage"])
