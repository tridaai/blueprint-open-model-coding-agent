"""Round 17: the MCP surface — the protocol in-process, the tool
list as the governance boundary, the doors' audit parity, and the
refusals in the shell's own words. The suite drives the surface
the way a client does (decoded messages and raw stdio lines),
never through a subprocess."""

from __future__ import annotations

import io
import json

import pytest

from open_model_coding_agent.errors import GateError
from open_model_coding_agent.mcp_server import (
    GOVERNED_ACT_NAMES,
    McpSurface,
    serve_stdio,
    tool_definitions,
)

TOOL_NAMES = [tool["name"] for tool in tool_definitions()]


@pytest.fixture()
def surface(service):
    return McpSurface(service)


def rpc(surface, method, params=None, request_id=1):
    return surface.handle(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    )


def call(surface, name, arguments=None):
    return rpc(
        surface, "tools/call", {"name": name, "arguments": arguments or {}}
    )


def payload(response):
    assert response["result"]["isError"] is False, response
    return json.loads(response["result"]["content"][0]["text"])


def refusal_text(response):
    assert response["result"]["isError"] is True, response
    return response["result"]["content"][0]["text"]


# -- the handshake -----------------------------------------------------------


def test_initialize_answers_the_clients_supported_version(surface):
    result = rpc(surface, "initialize", {"protocolVersion": "2025-03-26"})[
        "result"
    ]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "trida-open-model-coding-agent"
    # The founding rule rides the handshake: a client learns the
    # boundary before it learns a single tool.
    assert "approv" in result["instructions"]


def test_initialize_falls_back_for_an_unknown_version(surface):
    result = rpc(surface, "initialize", {"protocolVersion": "1900-01-01"})[
        "result"
    ]
    assert result["protocolVersion"] == "2024-11-05"


def test_ping_notifications_and_unknown_methods(surface):
    assert rpc(surface, "ping")["result"] == {}
    # Notifications are accepted and answered with silence.
    assert (
        surface.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        is None
    )
    response = rpc(surface, "resources/list")
    assert response["error"]["code"] == -32601
    assert "unknown_method" in response["error"]["message"]


# -- the tool list is the boundary -------------------------------------------


def test_tools_list_is_exactly_the_governed_surface(surface):
    listed = rpc(surface, "tools/list")["result"]["tools"]
    assert [tool["name"] for tool in listed] == [
        "list_work_orders",
        "get_work_order",
        "get_review_packet",
        "get_review_queue",
        "get_operations_digest",
        "get_cost_report",
        "create_work_order",
        "draft_plan",
    ]
    for tool in listed:
        assert tool["inputSchema"]["type"] == "object"
    # Every tool is a read (get_/list_) or one of the two
    # preparatory acts. There is no third shape.
    for name in TOOL_NAMES:
        assert name.startswith(("get_", "list_")) or name in {
            "create_work_order",
            "draft_plan",
        }


def test_no_tool_stands_behind_a_governed_act(surface):
    for name in GOVERNED_ACT_NAMES:
        assert name not in TOOL_NAMES
    # And a call to a decision-shaped name is refused by name, with
    # the boundary restated — not answered as a generic bad call.
    for name in (
        "decide_plan",
        "start_run",
        "decide_review",
        "publish_template",
        "drive_batch",
    ):
        response = call(surface, name, {})
        assert response["error"]["code"] == -32602
        assert response["error"]["message"].startswith(
            f"governed_act_not_a_tool: {name!r}"
        )


# -- reads ride the same folds -------------------------------------------------


def test_read_tools_return_service_data(surface, service, gold_order):
    wid = gold_order["work_order_id"]
    orders = payload(call(surface, "list_work_orders"))
    assert [order["work_order_id"] for order in orders] == [wid]

    detail = payload(call(surface, "get_work_order", {"work_order_id": wid}))
    assert detail["work_order"]["work_order_id"] == wid
    assert detail["audit"], "the detail carries the order's trail"

    service.draft_plan(wid, actor="tester")
    queue = payload(call(surface, "get_review_queue"))
    assert [entry["work_order_id"] for entry in queue["entries"]] == [wid]
    assert queue["entries"][0]["category"] == "plan_awaiting_approval"

    packet = payload(
        call(surface, "get_review_packet", {"work_order_id": wid})
    )
    assert packet["plan"]["version"] == 1

    digest = payload(call(surface, "get_operations_digest"))
    assert isinstance(digest, dict) and digest
    costs = payload(call(surface, "get_cost_report"))
    assert isinstance(costs, dict) and "profiles" in costs


# -- the doors: ordinary acts, channel on the trail ----------------------------


def test_create_work_order_through_the_door(service, surface):
    order = payload(
        call(
            surface,
            "create_work_order",
            {
                "actor": "Mcp Client",
                "gold_task_id": "reserve-overcommit",
                "title": "Reserve must not overcommit stock",
                "task_text": "Refuse a reservation larger than availability.",
                "file_scope": ["workshop/inventory.py"],
            },
        )
    )
    wid = order["work_order_id"]
    # Audit parity: the ordinary event under the caller's name,
    # indistinguishable from an API submission — plus the door's
    # trail entry naming the channel.
    assert order["requested_by"] == "Mcp Client"
    events = service.audit(wid)
    created = next(e for e in events if e["event"] == "work_order_created")
    assert created["actor"] == "Mcp Client"
    assert created["detail"]["gold_task_id"] == "reserve-overcommit"
    door = next(e for e in events if e["event"] == "mcp_work_order_created")
    assert door["actor"] == "Mcp Client"
    assert door["detail"]["channel"] == "mcp"
    assert door["detail"]["tool"] == "create_work_order"


def test_draft_plan_through_the_door_still_awaits_a_human(
    service, surface, gold_order
):
    wid = gold_order["work_order_id"]
    plan = payload(
        call(
            surface,
            "draft_plan",
            {"actor": "Mcp Client", "work_order_id": wid},
        )
    )
    assert plan["version"] == 1
    assert plan["status"] == "draft"
    assert plan["decided_by"] is None
    assert service.get_work_order(wid)["status"] == "plan_ready"
    # The gate is unmoved by the door: execution still refuses.
    with pytest.raises(GateError) as refused:
        service.start_run(wid, actor="tester")
    assert refused.value.code == "plan_not_approved"
    events = service.audit(wid)
    door = next(e for e in events if e["event"] == "mcp_plan_drafted")
    assert door["detail"]["channel"] == "mcp"
    assert door["detail"]["tool"] == "draft_plan"


# -- refusals and protocol errors, each named ----------------------------------


def test_governance_refusals_surface_verbatim(surface, gold_order):
    wid = gold_order["work_order_id"]
    payload(
        call(surface, "draft_plan", {"actor": "a", "work_order_id": wid})
    )
    text = refusal_text(
        call(surface, "draft_plan", {"actor": "a", "work_order_id": wid})
    )
    assert text.startswith("plan_not_draftable: ")

    text = refusal_text(
        call(surface, "get_work_order", {"work_order_id": "wo-missing"})
    )
    assert text == "unknown_work_order: no work order 'wo-missing'"

    text = refusal_text(
        call(
            surface,
            "create_work_order",
            {
                "actor": "a",
                "gold_task_id": "no-such-task",
                "title": "t",
                "task_text": "x",
                "file_scope": [],
            },
        )
    )
    assert text.startswith("unknown_gold_task: ")


def test_protocol_errors_are_named(surface):
    response = call(surface, "teleport_work_order", {})
    assert response["error"]["code"] == -32602
    assert response["error"]["message"].startswith(
        "unknown_tool: no tool named 'teleport_work_order'"
    )

    # A missing actor is the call's fault, named as the argument.
    response = call(
        surface, "draft_plan", {"work_order_id": "wo-anything"}
    )
    assert response["error"]["code"] == -32602
    assert "'actor'" in response["error"]["message"]
    assert response["error"]["message"].startswith("invalid_params: ")

    response = rpc(
        surface,
        "tools/call",
        {"name": "list_work_orders", "arguments": ["not", "an", "object"]},
    )
    assert response["error"]["code"] == -32602
    assert "invalid_params" in response["error"]["message"]


def test_a_bad_line_never_kills_the_surface(surface):
    bad = surface.handle_line("{ this is not json")
    assert json.loads(bad)["error"]["code"] == -32700
    good = surface.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    )
    assert json.loads(good)["id"] == 9


def test_stdio_loop_round_trip(service):
    instream = io.StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05"},
                    }
                ),
                json.dumps(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                ),
                json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
                ),
            ]
        )
        + "\n"
    )
    outstream = io.StringIO()
    assert serve_stdio(service, instream, outstream) == 0
    lines = outstream.getvalue().splitlines()
    # Two requests, two responses; the notification stays silent.
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == 1
    assert json.loads(lines[1])["id"] == 2
