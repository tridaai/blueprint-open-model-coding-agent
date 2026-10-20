"""The MCP surface: machine clients submit work, never decisions.

    coding-agent mcp        # or: python -m open_model_coding_agent.mcp_server

An MCP host — an IDE, a desktop assistant, another agent — is a
machine client, and this shell's contract with machine clients is
the founding rule of the surface:

    A client may state work and read what the shell made of it.
    It may not approve a plan, start a run, decide a review,
    answer a challenge, promote a model, drive or cancel a batch,
    or publish a template. Those are human acts, taken in the
    console or the API, by a named person — no tool here leads
    to them, and a call to a decision-shaped name is refused by
    name, with the boundary restated.

The eight tools are the reads and the two preparatory acts:
``list_work_orders``, ``get_work_order``, ``get_review_packet``,
``get_review_queue``, ``get_operations_digest``, and
``get_cost_report`` read the same folds the console reads;
``create_work_order`` and ``draft_plan`` enter through doors on
the service (``create_work_order_mcp``, ``draft_plan_mcp``) that
perform the ordinary act under the caller's name plus one trail
entry carrying ``channel: "mcp"`` and the tool's name — so intake
and drafting judge an MCP submission exactly as they judge an API
one, and the order's audit trail shows where each act came from.
Every tool is a thin adapter onto the service the API serves;
there is no second implementation of any logic here.

A governance refusal is a result, not a protocol failure: the
tool result carries ``isError`` and the shell's refusal verbatim,
in the standing ``code: message`` form, so a client can tell
"the shell refused, and here is the rule" from "the call was
malformed". Malformed calls are protocol errors, named: an
unknown tool at -32602 (a decision-shaped name is refused as
``governed_act_not_a_tool``), a missing or mistyped argument at
-32602 naming the argument, an unknown method at -32601, a
non-JSON line at -32700 — and the loop survives all of them.

The protocol is the repo's own code, on purpose. The survey for
this round tried the official ``mcp`` Python SDK first: it is not
in this repo's locked environment at all; the 1.x line resolves
against the frozen pydantic 2.14.0 pin and then fails at import
(the failure blueprint #13 recorded — FastMCP reaches for a
private pydantic helper pydantic 2.14 removed), and the 2.x line
restructures the API wholesale and would add some twenty-eight
packages to an exactly-pinned environment to serve eight tools.
Moving a frozen pin, or adopting a shifting SDK, for three
JSON-RPC methods is the tail wagging the dog. What is implemented
here is exactly what is claimed, no more:

- transport: stdio, one JSON-RPC 2.0 message per line;
- methods: ``initialize``, ``ping``, ``tools/list``,
  ``tools/call`` (notifications are accepted and answered with
  silence);
- protocol versions: ``2024-11-05`` and ``2025-03-26`` — the
  message shapes this surface uses are identical in both; the
  server answers with the client's requested version when it is
  one of those, and with ``2024-11-05`` otherwise;
- tool results: one text content block carrying the JSON
  payload; refusals as ``isError`` results whose text is the
  shell's refusal, word for word.

No resources, no prompts, no sampling, no roots, and no HTTP
transport: a client that asks for another method is told, by
name, that the method is not here. There is no auth layer —
stdio is a local pipe, and the surface inherits the blueprint's
standing caveat that actors are recorded, not authenticated.
The surface is stateless per call beyond the store; two clients
share nothing but the records, like two console tabs.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from pydantic import ValidationError

from .config import load_settings
from .errors import GateError
from .models import WorkOrderIn
from .service import CodingService

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26")
SERVER_NAME = "trida-open-model-coding-agent"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "This surface takes work orders and reports standing. It has no "
    "tool that approves a plan, starts a run, decides a review, "
    "answers a challenge, promotes a model, drives a batch, or "
    "publishes a template: those are human acts, taken in the "
    "console or the API by a named person. A plan drafted here "
    "still awaits a person's approval before anything executes."
)

# Governed acts a client might reach for, named so the refusal can
# name them back. The surface's guarantee is not that these strings
# are absent from a list — it is that no handler stands behind any
# of them: the registry below is the whole surface, and these are
# not in it.
GOVERNED_ACT_NAMES = frozenset(
    {
        "decide_plan",
        "approve_plan",
        "reject_plan",
        "amend_plan",
        "start_run",
        "resume_run",
        "restart_run",
        "abandon_run",
        "decide_review",
        "accept_patch",
        "reject_patch",
        "request_changes",
        "answer_challenge",
        "promote",
        "promote_profile",
        "rollback",
        "rollback_promotion",
        "evaluate_profile",
        "run_benchmark",
        "run_comparison",
        "create_batch",
        "drive_batch",
        "cancel_batch",
        "release_batch_driver",
        "create_template",
        "publish_template",
        "instantiate_template",
    }
)
_GOVERNED_PREFIXES = (
    "approve_",
    "decide_",
    "execute_",
    "publish_",
    "drive_",
    "cancel_",
    "answer_",
    "promote_",
)


class _ProtocolError(Exception):
    """A malformed call, named: a JSON-RPC error code and the
    message that says what was wrong with the call itself (as
    against a governance refusal, which is a well-formed call the
    shell declined — that travels as an ``isError`` result)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# -- argument discipline ----------------------------------------------------


def _require_str(args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise _ProtocolError(
            -32602,
            f"invalid_params: argument {name!r} is required and must "
            "be a non-empty string",
        )
    return value


def _optional_str(args: dict, name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ProtocolError(
            -32602, f"invalid_params: argument {name!r} must be a string"
        )
    return value


def _require_str_list(args: dict, name: str) -> list[str]:
    value = args.get(name)
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise _ProtocolError(
            -32602,
            f"invalid_params: argument {name!r} is required and must "
            "be a list of strings",
        )
    return list(value)


def _optional_str_list(args: dict, name: str) -> list[str]:
    value = args.get(name)
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise _ProtocolError(
            -32602,
            f"invalid_params: argument {name!r} must be a list of strings",
        )
    return list(value)


def _require_order(service: CodingService, args: dict, read: Callable):
    work_order_id = _require_str(args, "work_order_id")
    result = read(work_order_id)
    if result is None:
        raise GateError(
            "unknown_work_order", f"no work order {work_order_id!r}"
        )
    return result


# -- tool handlers: thin adapters onto the service ---------------------------


def _list_work_orders(service: CodingService, args: dict):
    return service.list_work_orders()


def _get_work_order(service: CodingService, args: dict):
    return _require_order(service, args, service.work_order_detail)


def _get_review_packet(service: CodingService, args: dict):
    return _require_order(service, args, service.review_packet)


def _get_review_queue(service: CodingService, args: dict):
    return service.review_queue()


def _get_operations_digest(service: CodingService, args: dict):
    return service.operations_digest(
        since=_optional_str(args, "since"), until=_optional_str(args, "until")
    )


def _get_cost_report(service: CodingService, args: dict):
    return service.cost_report()


def _create_work_order(service: CodingService, args: dict):
    actor = _require_str(args, "actor")
    try:
        body = WorkOrderIn(
            title=_require_str(args, "title"),
            task_text=_require_str(args, "task_text"),
            file_scope=_require_str_list(args, "file_scope"),
            gold_task_id=_optional_str(args, "gold_task_id"),
            grading_tests=_optional_str_list(args, "grading_tests"),
            target_path=_optional_str(args, "target_path") or "",
            requested_by=actor,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "body"
        raise _ProtocolError(
            -32602, f"invalid_params: {where}: {first['msg']}"
        ) from exc
    return service.create_work_order_mcp(body, actor)


def _draft_plan(service: CodingService, args: dict):
    actor = _require_str(args, "actor")
    work_order_id = _require_str(args, "work_order_id")
    return service.draft_plan_mcp(work_order_id, actor)


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOLS: list[dict] = [
    {
        "name": "list_work_orders",
        "description": (
            "Every work order on record, oldest first — the same "
            "records the console lists."
        ),
        "inputSchema": _schema({}, []),
        "handler": _list_work_orders,
    },
    {
        "name": "get_work_order",
        "description": (
            "One work order in full: the order, its plans, its runs "
            "with usage, and its audit trail."
        ),
        "inputSchema": _schema(
            {"work_order_id": _STR}, ["work_order_id"]
        ),
        "handler": _get_work_order,
    },
    {
        "name": "get_review_packet",
        "description": (
            "The packet a reviewer decides from: the approved plan, "
            "the patch and its shape, the verification record with "
            "its risk flags, the challenge pass's records, cost, "
            "and prior decisions."
        ),
        "inputSchema": _schema(
            {"work_order_id": _STR}, ["work_order_id"]
        ),
        "handler": _get_review_packet,
    },
    {
        "name": "get_review_queue",
        "description": (
            "The reviewers' queue, derived on read: every order "
            "standing where a person must act, with its next act "
            "named. Reading it decides nothing."
        ),
        "inputSchema": _schema({}, []),
        "handler": _get_review_queue,
    },
    {
        "name": "get_operations_digest",
        "description": (
            "The operator digest folded from the stored records: "
            "runs by status and failure class, gate decisions, "
            "challenges, batches, usage, and cost per verified "
            "success, over an optional created-at window."
        ),
        "inputSchema": _schema(
            {"since": _STR, "until": _STR}, []
        ),
        "handler": _get_operations_digest,
    },
    {
        "name": "get_cost_report",
        "description": (
            "Cost per successful task by model profile, folded "
            "from every usage row under the governed price table."
        ),
        "inputSchema": _schema({}, []),
        "handler": _get_cost_report,
    },
    {
        "name": "create_work_order",
        "description": (
            "Submit a work order through ordinary intake: a scoped "
            "task (title, task text, approved file scope), ad hoc "
            "or against a gold task. Intake only — the order still "
            "needs a plan, a person's approval, and a review "
            "before anything lands. The actor is recorded as the "
            "order's requester."
        ),
        "inputSchema": _schema(
            {
                "actor": _STR,
                "title": _STR,
                "task_text": _STR,
                "file_scope": _STR_LIST,
                "gold_task_id": _STR,
                "grading_tests": _STR_LIST,
                "target_path": _STR,
            },
            ["actor", "title", "task_text", "file_scope"],
        ),
        "handler": _create_work_order,
    },
    {
        "name": "draft_plan",
        "description": (
            "Ask the serving model to draft a plan for a work "
            "order that stands in draft. Preparatory only: the "
            "draft still awaits a person's approval, and execution "
            "from an unapproved plan is refused in code. The actor "
            "is recorded on the draft."
        ),
        "inputSchema": _schema(
            {"actor": _STR, "work_order_id": _STR},
            ["actor", "work_order_id"],
        ),
        "handler": _draft_plan,
    },
]

_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def tool_definitions() -> list[dict]:
    """The surface as a client sees it: names, descriptions, and
    input schemas — the handlers stay on this side of the wire."""
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
        }
        for tool in TOOLS
    ]


def _is_governed_act_name(name: str) -> bool:
    return name in GOVERNED_ACT_NAMES or name.startswith(_GOVERNED_PREFIXES)


def _error_response(request_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class McpSurface:
    """The protocol over one service. ``handle`` takes a decoded
    JSON-RPC message and returns the response message, or ``None``
    for a notification — the silence the protocol prescribes."""

    def __init__(self, service: CodingService) -> None:
        self.service = service

    # -- message handling ------------------------------------------------

    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict):
            return _error_response(
                None,
                -32600,
                "invalid_request: a message must be a JSON-RPC object",
            )
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            if request_id is None:
                return None
            return _error_response(
                request_id,
                -32600,
                "invalid_request: 'method' must be a string",
            )
        if request_id is None:
            # A notification: accepted, answered with silence.
            return None
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error_response(
                request_id,
                -32602,
                "invalid_params: 'params' must be an object",
            )
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_definitions()}
            elif method == "tools/call":
                result = self._call(params)
            else:
                return _error_response(
                    request_id,
                    -32601,
                    f"unknown_method: this surface has no method "
                    f"{method!r}; it serves initialize, ping, "
                    "tools/list, and tools/call",
                )
        except _ProtocolError as exc:
            return _error_response(request_id, exc.code, exc.message)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def handle_line(self, line: str) -> str | None:
        """One line of the stdio stream in, one line out (or none).
        A line that is not JSON is a parse error with no id — and
        the loop goes on: one bad line never ends the surface."""
        text = line.strip()
        if not text:
            return None
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return json.dumps(
                _error_response(
                    None,
                    -32700,
                    "parse_error: the line was not JSON; the surface "
                    "speaks one JSON-RPC message per line",
                )
            )
        response = self.handle(message)
        return json.dumps(response) if response is not None else None

    # -- methods ----------------------------------------------------------

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = (
            requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _ProtocolError(
                -32602,
                "invalid_params: tools/call requires a tool 'name'",
            )
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _ProtocolError(
                -32602,
                "invalid_params: 'arguments' must be an object",
            )
        tool = _TOOLS_BY_NAME.get(name)
        if tool is None:
            names = ", ".join(_TOOLS_BY_NAME)
            if _is_governed_act_name(name):
                raise _ProtocolError(
                    -32602,
                    f"governed_act_not_a_tool: {name!r} is a human "
                    "act — a decision, an execution, or a publication "
                    "— and no tool on this surface exposes it; those "
                    "acts are taken in the console or the API by a "
                    f"named person. The tools here are: {names}",
                )
            raise _ProtocolError(
                -32602,
                f"unknown_tool: no tool named {name!r} on this "
                f"surface; the tools are: {names}",
            )
        try:
            payload = tool["handler"](self.service, arguments)
        except GateError as exc:
            # A governance refusal is a result: the call was
            # well-formed, the shell declined, and the refusal
            # travels verbatim in the shell's own code and words.
            return {
                "content": [
                    {"type": "text", "text": f"{exc.code}: {exc.message}"}
                ],
                "isError": True,
            }
        return {
            "content": [
                {"type": "text", "text": json.dumps(payload, default=str)}
            ],
            "isError": False,
        }


def serve_stdio(
    service: CodingService, instream=None, outstream=None
) -> int:
    """The stdio loop: lines in, lines out, until the stream ends."""
    surface = McpSurface(service)
    instream = instream if instream is not None else sys.stdin
    outstream = outstream if outstream is not None else sys.stdout
    for line in instream:
        response = surface.handle_line(line)
        if response is not None:
            outstream.write(response + "\n")
            outstream.flush()
    return 0


def main() -> int:
    """Serve the surface over stdio, on the deployment the
    environment names — the same settings the API boots with, so a
    deployment profile's rules bind this surface too. A
    misconfigured deployment refuses at load, by name, before
    anything is served."""
    settings = load_settings()
    service = CodingService(settings)
    return serve_stdio(service)


if __name__ == "__main__":
    raise SystemExit(main())
