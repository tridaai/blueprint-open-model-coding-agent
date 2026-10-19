"""The model port: where a model — real or scripted — meets the shell.

The shell never calls a model API directly. It asks a ModelPort for a
plan draft, then for one step at a time during execution. A step is a
tool call (``{"tool": ..., "args": {...}}``) or ``{"tool": "finish"}``.
Everything the shell enforces — scope, budgets, allowlisted commands —
sits between the model's steps and the world, so swapping the offline
scripted model for a live Baseten model (round 2) changes nothing
about the governance.

The offline model is a LABELLED scripted stand-in. For a gold task it
replays the task's scripted edits (the benchmark's reference repair),
the way a real model's knowledge would arrive as edits. For an ad-hoc
work order it inspects and runs the tests, then finishes without
edits — it does not pretend to solve work it has no script for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


class ModelPort:
    """Interface every model adapter implements."""

    profile_id: str = "abstract"

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
    """

    profile_id = "offline"

    def __init__(self, scripts: dict[str, list[dict]] | None = None) -> None:
        self._scripts = scripts or {}
        self._queue: list[Step] = []

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
        return PlanDraft(
            approach=approach,
            files=list(work_order["file_scope"]),
            tests=tests,
        )

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
        if self._queue:
            return self._queue.pop(0)
        return Step("finish", {})


class BasetenModel(ModelPort):
    """The Baseten profile's adapter slot.

    The profile (endpoint ``https://inference.baseten.co/v1``, key env
    ``BASETEN_API_KEY``, default slug in config) exists from round 1
    so the deployment shape is real; the live adapter — tool calling
    over Baseten's OpenAI-compatible Model API — lands in round 2.
    Until then a run under this profile refuses, by name, rather than
    silently falling back to the scripted model.
    """

    profile_id = "baseten"

    def draft_plan(self, *, work_order: dict, repo_files: list[str]) -> PlanDraft:
        raise GateError(
            "live_model_not_available",
            "the Baseten live adapter lands in round 2; round 1 runs the "
            "offline profile",
        )

    def next_step(self, *, transcript: list[dict]) -> Step:
        raise GateError(
            "live_model_not_available",
            "the Baseten live adapter lands in round 2; round 1 runs the "
            "offline profile",
        )


def make_model(profile_id: str, *, scripts: dict[str, list[dict]] | None = None) -> ModelPort:
    if profile_id == "offline":
        return OfflineScriptedModel(scripts=scripts)
    if profile_id == "baseten":
        return BasetenModel()
    raise ConfigError(f"no model adapter for profile {profile_id!r}")
