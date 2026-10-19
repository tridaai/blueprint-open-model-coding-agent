"""The Baseten live adapter: an open model, behind the model port.

Baseten's Model APIs are OpenAI Chat Completions-compatible, so this
adapter speaks that wire format to the profile's endpoint
(``https://inference.baseten.co/v1``) with the key sent the way the
OpenAI SDK sends it (``Authorization: Bearer``). Everything else is
the discipline the shell already has: the adapter translates between
the wire and the port's vocabulary — a plan draft, then one step at
a time — and the agent loop never learns which backend served it.

HTTP goes through a transport seam. Production uses a small urllib
transport (no new dependency; the adapter needs one POST shape).
Tests inject a fake transport replaying recorded-shape responses, so
the suite never touches the network.

Failures are named, bounded, and final: 401 is ``model_auth_failed``,
404 is ``model_unknown_slug``, 429/529/5xx retry a bounded number of
times with backoff and then fail by name. Nothing retries forever,
nothing falls back to another model, and a failure inside a run is a
GateError the service records as the run's failure reason.

Every request carries the profile's recorded ``max_tokens`` ceiling.
That number matters more than it looks: the models on the shortlist
are reasoning models, which spend completion tokens on
``reasoning_content`` before any visible answer exists, so a small
ceiling buys an empty reply. When that is exactly what comes back —
``finish_reason`` ``length`` and no content, no tool calls — the
adapter fails by name (``model_output_truncated``) instead of
handing the shell an empty string to misread as a malformed plan,
and the spent call's usage is still recorded first: the tokens were
spent, so the ledger says so.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .errors import GateError
from .model_port import ModelPort, PlanDraft, PlanStepDraft, Step
from .profiles import ModelProfile

_RETRYABLE = {429, 500, 502, 503, 529}

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files in the workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read one file from the workspace, optionally a 1-based "
                "line range (start_line/end_line). Large files come back "
                "as a labelled head naming the file's true size; binary "
                "files are refused by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search workspace files for a regex pattern. Matches "
                "return with line numbers and context. `glob` (default "
                "*.py) and `path` (a file or directory) scope the "
                "search inside the workspace; `case_sensitive` defaults "
                "to true; `limit` caps the matches returned, and a "
                "capped result says so with the true total."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "case_sensitive": {"type": "boolean"},
                    "glob": {"type": "string"},
                    "path": {"type": "string"},
                    "limit": {"type": "integer"},
                    "context": {"type": "integer"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace the anchor text `old` with `new` in one file — "
                "the only way to change code. `old` must occur exactly "
                "once: zero occurrences refuse as anchor_not_found, "
                "several as anchor_ambiguous, and both refusals come "
                "back as results you can act on (re-read, narrow the "
                "anchor). The file must be inside the approved file "
                "scope; the shell refuses anything outside it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run pytest over named test files in the workspace — the "
                "only command that exists. Non-test paths are refused. "
                "With only_affected=true, the shell runs the affected "
                "set it computes from the plan's steps and your edits "
                "so far; `paths` then names a subset of that set (empty "
                "means the whole set)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "only_affected": {"type": "boolean"},
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "End the run. Call when the change is complete.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
            },
        },
    },
]


# -- the transport seam ---------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    body: str


class TransportError(Exception):
    """The request never got an HTTP answer (DNS, TLS, timeout)."""


class Transport(Protocol):
    def post(
        self, url: str, *, headers: dict, json_body: dict, timeout: float
    ) -> HttpResponse: ...


class UrllibTransport:
    """The production transport: one POST of JSON via the stdlib."""

    def post(
        self, url: str, *, headers: dict, json_body: dict, timeout: float
    ) -> HttpResponse:
        data = json.dumps(json_body).encode()
        request = urllib.request.Request(url, data=data, method="POST")
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return HttpResponse(resp.status, resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(str(exc)) from exc


# -- plan parsing ------------------------------------------------------------


def parse_plan_content(content: str) -> PlanDraft:
    """Parse a model's plan reply into a draft. The wire shape is the
    plan's own shape: an approach and an ordered list of steps, each
    with an intent, its files, and the tests it expects to run.
    Anything malformed is a named refusal; the service's shared
    validator then checks the draft's semantics (scope, resolution)
    exactly as it does for the offline model's drafts."""
    try:
        raw = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        raise GateError(
            "malformed_plan", "the model's plan reply was not valid JSON"
        ) from None
    if not isinstance(raw, dict):
        raise GateError("malformed_plan", "the model's plan reply was not a JSON object")
    approach = raw.get("approach")
    if not isinstance(approach, str) or not approach.strip():
        raise GateError("malformed_plan", "the plan's approach is missing or empty")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise GateError(
            "malformed_plan", "the model's plan reply carried no steps"
        )
    steps: list[PlanStepDraft] = []
    for index, item in enumerate(raw_steps):
        label = f"plan step {index + 1}"
        if not isinstance(item, dict):
            raise GateError(
                "malformed_plan", f"the model's {label} was not a JSON object"
            )
        intent = item.get("intent")
        files = item.get("files")
        tests = item.get("tests", [])
        if not isinstance(intent, str) or not intent.strip():
            raise GateError("malformed_plan", f"the model's {label} has no intent")
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise GateError(
                "malformed_plan",
                f"the model's {label} files must be a list of paths",
            )
        if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
            raise GateError(
                "malformed_plan",
                f"the model's {label} tests must be a list of paths",
            )
        steps.append(
            PlanStepDraft(intent=intent, files=list(files), tests=list(tests))
        )
    return PlanDraft(approach=approach, steps=steps)


# -- the adapter -------------------------------------------------------------


class BasetenModel(ModelPort):
    """A live open model on Baseten's Model API, behind the port."""

    def __init__(
        self,
        profile: ModelProfile,
        *,
        api_key: str,
        transport: Transport | None = None,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        sleep=time.sleep,
        request_timeout: float = 120.0,
    ) -> None:
        super().__init__()
        self.profile = profile
        self.profile_id = profile.profile_id
        self._api_key = api_key
        self._transport: Transport = transport or UrllibTransport()
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._timeout = request_timeout
        self._messages: list[dict] = []
        self._queue: list[tuple[str, Step]] = []
        self._awaiting: list[str] = []
        self._seen = 0

    # -- wire ------------------------------------------------------------
    def _chat(self, messages: list[dict], *, tools: bool, json_mode: bool) -> dict:
        """One logical chat call: bounded retries, named failures,
        and the usage object folded into the port's accounting."""
        body: dict = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": 0,
            # The profile's recorded ceiling, never a constant in
            # code: a reasoning model's budget is a per-model fact.
            "max_tokens": self.profile.max_tokens,
        }
        if tools:
            body["tools"] = TOOL_SCHEMAS
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        url = f"{self.profile.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
        attempt = 0
        while True:
            try:
                resp = self._transport.post(
                    url, headers=headers, json_body=body, timeout=self._timeout
                )
            except TransportError as exc:
                if attempt < self._max_retries:
                    self._sleep(self._backoff_base * (2**attempt))
                    attempt += 1
                    continue
                raise GateError(
                    "model_unreachable",
                    f"the model endpoint did not answer after "
                    f"{attempt + 1} attempts: {exc}",
                ) from None
            if resp.status == 200:
                latency_ms = int((time.perf_counter() - started) * 1000)
                return self._parse_body(resp.body, latency_ms)
            if resp.status in _RETRYABLE and attempt < self._max_retries:
                self._sleep(self._backoff_base * (2**attempt))
                attempt += 1
                continue
            raise self._status_error(resp.status)

    def _status_error(self, status: int) -> GateError:
        slug = self.profile.model
        if status == 401:
            return GateError(
                "model_auth_failed",
                "the provider rejected the API key (HTTP 401); check "
                f"{self.profile.api_key_env}",
            )
        if status == 404:
            return GateError(
                "model_unknown_slug",
                f"model slug {slug!r} was not found at the endpoint (HTTP 404)",
            )
        if status == 429:
            return GateError(
                "model_rate_limited",
                f"the endpoint stayed rate-limited through "
                f"{self._max_retries + 1} attempts (HTTP 429)",
            )
        if status == 529:
            return GateError(
                "model_overloaded",
                f"the endpoint stayed overloaded through "
                f"{self._max_retries + 1} attempts (HTTP 529)",
            )
        if 500 <= status < 600:
            return GateError(
                "model_server_error",
                f"the endpoint kept failing through "
                f"{self._max_retries + 1} attempts (HTTP {status})",
            )
        return GateError(
            "model_request_rejected",
            f"the endpoint rejected the request (HTTP {status})",
        )

    def _parse_body(self, body: str, latency_ms: int) -> dict:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise GateError(
                "model_response_invalid", "the model endpoint returned non-JSON"
            ) from None
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices or not isinstance(choices[0], dict):
            raise GateError(
                "model_response_invalid", "the model response carried no choices"
            )
        usage = payload.get("usage") or {}
        self._note_usage(
            source="api",
            unit="tokens",
            model=self.profile.model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
        )
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise GateError(
                "model_response_invalid", "the model response carried no message"
            )
        # Usage is noted BEFORE this check on purpose: a truncated
        # call still spent its tokens, and the ledger records them.
        content = message.get("content")
        if (
            choices[0].get("finish_reason") == "length"
            and not message.get("tool_calls")
            and not (isinstance(content, str) and content.strip())
        ):
            raise GateError(
                "model_output_truncated",
                f"the model stopped at the profile's max_tokens "
                f"ceiling ({self.profile.max_tokens}) with nothing "
                f"usable returned — finish_reason 'length' and empty "
                f"content (a reasoning model can spend the whole "
                f"budget on reasoning before answering). Raise "
                f"max_tokens on the {self.profile.profile_id!r} "
                f"profile record, or shorten the request.",
            )
        return message

    # -- the port ----------------------------------------------------------
    def draft_plan(self, *, work_order: dict, repo_files: list[str]) -> PlanDraft:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the planning step of a governed coding agent. "
                    "Reply with one JSON object and nothing else: "
                    '{"approach": "...", "steps": [{"intent": "...", '
                    '"files": [...], "tests": [...]}]}. '
                    "steps are the work in order: each step says what it "
                    "intends to change, names files only from the work "
                    "order's approved file scope, and names the test "
                    "files that should prove it."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task: {work_order.get('title', '')}\n"
                    f"{work_order.get('task_text', '')}\n\n"
                    f"Approved file scope: {work_order.get('file_scope', [])}\n"
                    f"Files in the repository: {repo_files}\n"
                    f"Grading tests named on the order: "
                    f"{work_order.get('grading_tests', [])}"
                    + (
                        "\n\nA reviewer sent a previous attempt back "
                        "with this note — plan against it:\n"
                        f"{work_order['_review_note']}"
                        if work_order.get("_review_note")
                        else ""
                    )
                ),
            },
        ]
        message = self._chat(messages, tools=False, json_mode=True)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise GateError(
                "malformed_plan", "the model's plan reply carried no content"
            )
        return parse_plan_content(content)

    def start_run(self, *, work_order: dict, plan: dict) -> None:
        self._queue = []
        self._awaiting = []
        self._seen = 0
        step_lines = [
            f"{index + 1}. {step.get('intent', '')} "
            f"(files: {', '.join(step.get('files') or [])}; "
            f"tests: {', '.join(step.get('tests') or [])})"
            for index, step in enumerate(plan.get("steps") or [])
        ]
        steps_block = (
            "\nPlan steps:\n" + "\n".join(step_lines) if step_lines else ""
        )
        self._messages = [
            {
                "role": "system",
                "content": (
                    "You are a coding agent working inside a governed "
                    "sandbox: an isolated copy of the repository. Use the "
                    "tools to inspect and change code. Rules the shell "
                    "enforces regardless of what you ask: edits only "
                    "inside the approved file scope, made as anchored "
                    "replacements; the only command is run_tests over "
                    "named test files. Make the smallest change that "
                    "satisfies the task, run the named tests, then call "
                    "finish."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task: {work_order.get('title', '')}\n"
                    f"{work_order.get('task_text', '')}\n\n"
                    f"Approved plan: {plan.get('approach', '')}\n"
                    f"Approved file scope: {plan.get('files', [])}\n"
                    f"Tests to run: {plan.get('tests', [])}"
                    f"{steps_block}"
                ),
            },
        ]

    def prepare_resume(self, *, transcript: list[dict]) -> None:
        """Rebuild the conversation from the stored transcript.

        The wire history of the earlier attempt cannot be replayed
        call for call — the shell's audit records summarise a step's
        args rather than keep them verbatim — so the rebuild is a
        faithful rendering of those records as one message: what was
        called, how it came back, and the standing fact that the
        workspace already holds the results. Entries from the
        earlier attempt are marked seen, so the fold in next_step
        only takes up entries the resumed attempt produces."""
        self._seen = len(transcript)
        if not transcript:
            return
        lines = [
            "This run was interrupted and is now resuming. The tool "
            "calls below were already made in the earlier attempt; "
            "their results stand in the workspace. Do not redo work "
            "that succeeded — continue from where they leave off."
        ]
        for entry in transcript:
            args = json.dumps(entry.get("args") or {}, default=str)
            if len(args) > 300:
                args = args[:300] + "…"
            outcome = f"ok={entry.get('ok')} refused={entry.get('refused', False)}"
            if entry.get("code"):
                outcome += f" error={entry['code']}"
            output = str(entry.get("output") or "")
            if len(output) > 500:
                output = output[:500] + "…"
            lines.append(
                f"- {entry.get('tool')}({args}) -> {outcome}\n{output}"
            )
        self._messages.append({"role": "user", "content": "\n".join(lines)})

    def next_step(self, *, transcript: list[dict]) -> Step:
        # Fold the results of steps already returned into the wire
        # history before asking for anything new.
        for entry in transcript[self._seen :]:
            self._seen += 1
            if not self._awaiting:
                continue
            call_id = self._awaiting.pop(0)
            content = f"ok={entry.get('ok')} refused={entry.get('refused', False)}"
            if entry.get("code"):
                content += f" error={entry['code']}"
            content += f"\n{entry.get('output', '')}"
            self._messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": content}
            )
        if self._queue:
            call_id, step = self._queue.pop(0)
            self._awaiting.append(call_id)
            return step
        message = self._chat(self._messages, tools=True, json_mode=False)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return Step(
                "finish", {"summary": str(message.get("content") or "")[:500]}
            )
        wire_calls: list[dict] = []
        steps: list[tuple[str, Step]] = []
        for index, call in enumerate(tool_calls):
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            call_id = str(call.get("id") or f"call-{index}")
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                raise GateError(
                    "model_response_invalid",
                    f"tool call {name!r} carried arguments that are not JSON",
                ) from None
            if not isinstance(args, dict):
                raise GateError(
                    "model_response_invalid",
                    f"tool call {name!r} carried non-object arguments",
                )
            wire_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": function.get("arguments") or "{}",
                    },
                }
            )
            steps.append((call_id, Step(name, args)))
        self._messages.append(
            {"role": "assistant", "content": None, "tool_calls": wire_calls}
        )
        call_id, step = steps[0]
        self._queue = steps[1:]
        self._awaiting.append(call_id)
        return step
