"""Batch work orders (round 14): the pure parts.

A batch is a named submission of work — a list of work-order
specifications — stored as one record. This module holds the
parts that need no store: the budget shape, the item shapes, and
the derivation of what an item waits on. The service's batch
acts lean on these the way the task library's acts lean on
:mod:`.templates`.

Two distinctions the whole round stands on:

- A batch is not a comparison (round 8). A comparison runs
  model profiles over a benchmark set to produce evidence for
  the promotion gate; it creates no work orders of anyone's
  work and decides nothing about any task. A batch submits
  work to be done: each item becomes an ordinary work order
  through ordinary intake, and the batch adds a submission, a
  budget, a sequential driver, and a folded report around
  orders that remain governed exactly as if they had arrived
  one at a time. A batch never benchmarks, and a comparison
  never creates work.
- A batch approves nothing. There is no bulk plan approval, no
  bulk review, no batch-level execution shortcut anywhere in
  this round: the driver advances each item only through the
  acts that need no person (drafting a plan, running an approved
  plan) and stops at every gate a person owns.
"""

from __future__ import annotations

from .errors import GateError

# The batch budget's dimensions, in the order a breach is named:
# calls first (the count every usage row lands in), then tokens,
# then wall seconds — the same three ceilings a run snapshots
# from policy (round 6), here for the batch as a whole.
BUDGET_KEYS = ("max_model_calls", "max_tokens", "max_wall_seconds")

ITEM_KINDS = ("gold", "adhoc", "template")


def validate_batch_budget(raw: dict | None) -> dict:
    """A batch's budget, normalised: every dimension present,
    each either None (uncapped for this batch) or a positive
    integer. An unknown key or a bad value refuses by name — a
    ceiling the submitter did not mean must never silently
    become one they did."""
    budget = {key: None for key in BUDGET_KEYS}
    if raw is None:
        return budget
    if not isinstance(raw, dict):
        raise GateError(
            "invalid_batch_budget",
            f"a batch budget is a mapping over {list(BUDGET_KEYS)}, "
            f"got {type(raw).__name__}",
        )
    for key, value in raw.items():
        if key not in BUDGET_KEYS:
            raise GateError(
                "invalid_batch_budget",
                f"unknown batch budget dimension {key!r}; the "
                f"dimensions are {list(BUDGET_KEYS)}",
            )
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GateError(
                "invalid_batch_budget",
                f"batch budget {key} must be a positive integer "
                f"or null, got {value!r}",
            )
        budget[key] = value
    return budget


def item_shape_problem(spec: dict) -> str | None:
    """The structural check on one item specification, before
    any semantic validation (which needs the gold suite and the
    template library): the kind is known, and the fields that
    kind's intake cannot proceed without are present. Returns a
    problem sentence, or None when the shape stands."""
    kind = spec.get("kind")
    if kind not in ITEM_KINDS:
        return (
            f"kind {kind!r} is not one of {list(ITEM_KINDS)} — an "
            f"item names the intake it will go through"
        )
    if kind == "gold":
        if not (spec.get("gold_task_id") or "").strip():
            return "a gold item names its gold_task_id"
    elif kind == "template":
        if not (spec.get("template_name") or "").strip():
            return "a template item names its template_name"
    elif kind == "adhoc":
        missing = [
            field
            for field in ("title", "task_text")
            if not (spec.get(field) or "").strip()
        ]
        if not spec.get("file_scope"):
            missing.append("file_scope")
        if missing:
            return (
                f"an adhoc item names its work: missing "
                f"{', '.join(missing)} — the same fields an ad-hoc "
                f"work order cannot be created without"
            )
    return None


# What an item waits on, by its order's status — the same next
# acts the review workbench names (round 11), with the two the
# queue does not carry: drafting (an operator act the driver
# performs) and starting a run on an approved plan. A terminal
# order waits on nothing.
NEXT_ACT_BY_STATUS = {
    "draft": "draft_plan",
    "plan_rejected": "draft_plan",
    "changes_requested": "draft_plan",
    "plan_ready": "decide_plan",
    "approved": "start_run",
    "succeeded": "review",
    "interrupted": "resume_run",
    "failed": "restart_run",
}

# The order statuses at which an item waits on a person, and the
# workbench category each waits under. Drafting and running are
# not here: they are the driver's work, not a person's.
AWAITING_PERSON = {
    "plan_ready": "plan_awaiting_approval",
    "succeeded": "awaiting_review",
    "interrupted": "run_interrupted",
    "failed": "run_failed",
}

TERMINAL_ORDER_STATUSES = ("accepted", "rejected", "abandoned")
