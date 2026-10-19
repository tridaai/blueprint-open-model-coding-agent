"""The model port: where a model — real or scripted — meets the shell.

The shell never calls a model API directly. It asks a ModelPort for a
plan draft, then for one step at a time during execution. A step is a
tool call (``{"tool": ..., "args": {...}}``) or ``{"tool": "finish"}``.
Everything the shell enforces — scope, budgets, allowlisted commands —
sits between the model's steps and the world, so swapping the offline
scripted model for a live Baseten model changes nothing about the
governance: the loop consumes the port, never the backend.

Every port implementation accounts for its own calls. A live adapter
reports the provider's usage object (tokens); the offline model
reports its own scripted accounting (characters of input and output,
labelled ``scripted`` — never presented as API usage). The service
drains the accounting after each call and stores it against the work
order and, during execution, the run.

The offline model is a LABELLED scripted stand-in. For a gold task it
replays the task's scripted edits (the benchmark's reference repair),
the way a real model's knowledge would arrive as edits. For an ad-hoc
work order it inspects and runs the tests, then finishes without
edits — it does not pretend to solve work it has no script for.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError, GateError


@dataclass
class PlanDraft:
    approach: str
    files: list[str]
    tests: list[str]


@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)


def validate_plan_draft(draft: PlanDraft, *, file_scope: list[str]) -> PlanDraft:
    """The one validation every plan draft passes, whichever model
    produced it — the offline script, or a live model's JSON reply.
    A draft that fails is refused as ``malformed_plan`` by name."""
    if not isinstance(draft.approach, str) or not draft.approach.strip():
        raise GateError("malformed_plan", "the plan's approach is missing or empty")
    if not draft.files:
        raise GateError("malformed_plan", "the plan names no files")
    scope = set(file_scope)
    outside = [f for f in draft.files if f not in scope]
    if outside:
        raise GateError(
            "malformed_plan",
            f"the plan names files outside the approved file scope: {outside}",
        )
    if not all(isinstance(t, str) for t in draft.tests):
        raise GateError("malformed_plan", "the plan's tests must be a list of paths")
    return draft


class ModelPort:
    """Interface every model adapter implements."""

    profile_id: str = "abstract"

    # -- usage accounting ---------------------------------------------
    def _note_usage(self, **record) -> None:
        records = getattr(self, "_usage_records", None)
        if records is None:
            records = self._usage_records = []
        records.append(record)

    def drain_usage(self) -> list[dict]:
        """Usage noted since the last drain. The service calls this
        after every port call and persists what it gets."""
        records = getattr(self, "_usage_records", [])
        self._usage_records = []
        return records

    # -- the port -------------------------------------------------------
    def draft_plan(self, *, work_order: dict, repo_files: list[str]) -> PlanDraft:
        raise NotImplementedError

    def start_run(self, *, work_order: dict, plan: dict) -> None:
        """Called once before the first step of a run."""

    def next_step(self, *, transcript: list[dict]) -> Step:
        raise NotImplementedError


class OfflineScriptedModel(ModelPort):
    """The labelled offline stand-in.

    ``scripts`` maps a gold task id to its scripted edits. The service
    supplies them from the gold suite at run start; the model simply
    plays the part of a model that knows the repair: read the scoped
    files, apply the edits, run the grading tests, finish.

    Its usage accounting is its own: character counts of what it was
    shown and what it produced, recorded with source ``scripted``.
    """

    profile_id = "offline"

    def __init__(self, scripts: dict[str, list[dict]] | None = None) -> None:
        self._scripts = scripts or {}
        self._queue: list[Step] = []

    def _account(self, shown: object, produced: object) -> None:
        self._note_usage(
            source="scripted",
            unit="chars",
            model="offline-scripted",
            prompt_tokens=len(json.dumps(shown, default=str)),
            completion_tokens=len(json.dumps(produced, default=str)),
            latency_ms=0,
        )

    def draft_plan(self, *, work_order: dict, repo_files: list[str]) -> PlanDraft:
        gold_id = work_order.get("gold_task_id")
        approach = (
            work_order.get("_plan_approach")
            or "Inspect the files in scope, make the smallest change that "
            "satisfies the task text, and run the named tests."
        )
        tests = list(work_order.get("grading_tests") or [])
        if gold_id:
            tests = tests or ["(gold grading tests, staged at verification)"]
        draft = PlanDraft(
            approach=approach,
            files=list(work_order["file_scope"]),
            tests=tests,
        )
        self._account(
            {"task_text": work_order.get("task_text"), "repo_files": repo_files},
            {"approach": draft.approach, "files": draft.files, "tests": draft.tests},
        )
        return draft

    def start_run(self, *, work_order: dict, plan: dict) -> None:
        gold_id = work_order.get("gold_task_id")
        edits = self._scripts.get(gold_id or "", [])
        queue: list[Step] = []
        for path in plan["files"]:
            queue.append(Step("read_file", {"path": path}))
        for edit in edits:
            queue.append(
                Step(
                    "edit_file",
                    {"path": edit["path"], "old": edit["old"], "new": edit["new"]},
                )
            )
        if work_order.get("grading_tests"):
            queue.append(
                Step("run_tests", {"paths": list(work_order["grading_tests"])})
            )
        queue.append(Step("finish", {}))
        self._queue = queue

    def next_step(self, *, transcript: list[dict]) -> Step:
        step = self._queue.pop(0) if self._queue else Step("finish", {})
        self._account(
            transcript[-1] if transcript else {},
            {"tool": step.tool, "args": step.args},
        )
        return step


def make_model(
    profile_id: str,
    *,
    scripts: dict[str, list[dict]] | None = None,
    profiles_dir: Path | None = None,
    profile=None,
    transport=None,
    sleep=None,
) -> ModelPort:
    """Build the adapter a profile names. ``profile`` (the settings'
    resolved record, overrides applied) wins over a fresh load."""
    from .config import load_profile

    resolved = profile or load_profile(profile_id, profiles_dir)
    if resolved.kind == "offline":
        return OfflineScriptedModel(scripts=scripts)
    api_key = os.environ.get(resolved.api_key_env or "")
    if not api_key:
        raise ConfigError(
            f"model profile {resolved.profile_id!r} is live and needs "
            f"{resolved.api_key_env} set in the environment; refusing to "
            f"build its adapter without a key"
        )
    from .baseten import BasetenModel

    kwargs: dict = {}
    if transport is not None:
        kwargs["transport"] = transport
    if sleep is not None:
        kwargs["sleep"] = sleep
    return BasetenModel(resolved, api_key=api_key, **kwargs)
