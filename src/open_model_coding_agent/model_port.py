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
class PlanStepDraft:
    """One step of a plan: what it intends to change, the files it
    touches, and the tests it expects to run."""

    intent: str
    files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "intent": self.intent,
            "files": list(self.files),
            "tests": list(self.tests),
        }


@dataclass
class PlanDraft:
    """A plan: an approach, and the work as an ordered list of steps.

    ``files`` and ``tests`` are derived — the unions over the steps,
    in step order — because the steps are the plan; the flat lists
    are what the shell enforces scope against and what a reader
    scans first.
    """

    approach: str
    steps: list[PlanStepDraft] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        out: list[str] = []
        for step in self.steps:
            for path in step.files:
                if path not in out:
                    out.append(path)
        return out

    @property
    def tests(self) -> list[str]:
        out: list[str] = []
        for step in self.steps:
            for path in step.tests:
                if path not in out:
                    out.append(path)
        return out


@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)


def validate_plan_draft(
    draft: PlanDraft,
    *,
    file_scope: list[str],
    repo_files: list[str] | None = None,
    grading_tests: list[str] | None = None,
) -> PlanDraft:
    """The one validation every plan draft passes, whichever model
    produced it — the offline script, or a live model's JSON reply.
    A draft that fails is refused as ``malformed_plan`` by name.

    The structure: an approach, and at least one step; every step
    carries an intent and names at least one file, and every file it
    names sits inside the work order's approved scope. The tests a
    step names must resolve — to a file that exists in the target
    repo, or to the task's grading set. Resolution is checked when
    the caller supplies that context (the service always does); a
    context-free call checks structure and scope only.
    """
    if not isinstance(draft.approach, str) or not draft.approach.strip():
        raise GateError("malformed_plan", "the plan's approach is missing or empty")
    if not draft.steps:
        raise GateError("malformed_plan", "the plan has no steps")
    scope = set(file_scope)
    resolvable: set[str] | None = None
    if repo_files is not None or grading_tests is not None:
        exact = set(repo_files or []) | set(grading_tests or [])
        resolvable = exact | {Path(p).name for p in exact}
    for index, step in enumerate(draft.steps):
        label = f"plan step {index + 1}"
        if not isinstance(step.intent, str) or not step.intent.strip():
            raise GateError("malformed_plan", f"{label} has no intent")
        if not step.files:
            raise GateError("malformed_plan", f"{label} names no files")
        outside = [f for f in step.files if f not in scope]
        if outside:
            raise GateError(
                "malformed_plan",
                f"{label} names files outside the approved file scope: {outside}",
            )
        if not all(isinstance(t, str) for t in step.tests):
            raise GateError(
                "malformed_plan", f"{label}'s tests must be a list of paths"
            )
        if resolvable is not None:
            unresolved = [t for t in step.tests if t not in resolvable]
            if unresolved:
                raise GateError(
                    "malformed_plan",
                    f"{label} names tests that resolve to nothing in the "
                    f"target repo or the grading set: {unresolved}",
                )
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

    def prepare_resume(self, *, transcript: list[dict]) -> None:
        """Called on a fresh adapter when a run resumes, after
        :meth:`start_run`, with the run's stored transcript — the
        tool calls the earlier attempt already made, rebuilt from
        the audit records. Adapters that carry position or
        conversational state rebuild it from those records here, so
        completed work is not re-issued; adapters that derive
        everything from the transcript itself need do nothing."""

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
        # A gold task carries its plan steps in the suite; the scripted
        # model drafts the plan the suite says a competent plan has.
        steps = [
            PlanStepDraft(
                intent=str(spec.get("intent") or ""),
                files=list(spec.get("files") or []),
                tests=list(spec.get("tests") or []),
            )
            for spec in (work_order.get("_plan_steps") or [])
        ]
        if not steps:
            tests = list(work_order.get("grading_tests") or [])
            if gold_id and work_order.get("_grading_relpath"):
                tests = tests or [work_order["_grading_relpath"]]
            steps = [
                PlanStepDraft(
                    intent=(
                        "Make the smallest change that satisfies the task "
                        "text in the files in scope, then check it against "
                        "the named tests."
                    ),
                    files=list(work_order["file_scope"]),
                    tests=tests,
                )
            ]
        draft = PlanDraft(approach=approach, steps=steps)
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

    def prepare_resume(self, *, transcript: list[dict]) -> None:
        """The script is positional: one queue entry per step the
        earlier attempt consumed, and the transcript holds exactly
        one entry per consumed step. Drop that many — the work they
        did stands in the workspace and is not re-issued."""
        del self._queue[: len(transcript)]

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
    if not resolved.servable:
        # Backstop: settings resolution refuses a not-servable
        # profile before a model is ever built, but a direct caller
        # of the port gets the same named refusal, never an adapter
        # that will 404 against a catalog that does not list it.
        raise resolved.servability_refusal()
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
