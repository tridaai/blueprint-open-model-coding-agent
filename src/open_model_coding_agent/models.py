"""Record shapes for the governance shell.

Every state the system can be in is a stored record. API payloads,
store payloads, and service returns all use these shapes so a reader
of the database, the API, or the console sees the same facts.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Work order lifecycle:
#   draft -> plan_ready -> approved -> running -> succeeded -> accepted
#                  |            |           |             -> rejected
#                  -> plan_rejected         -> failed
#   approved -> plan_ready when a plan amendment lands (the new
#   version awaits its own decision), and approved -> draft when
#   execution refuses a stale plan (a fresh plan must be drafted).
#   Round 5 adds the recovery states: running -> interrupted when a
#   run dies mid-flight (resumable, or restartable, under the same
#   approval — an approval is not consumed by a run that did not
#   finish), and interrupted/failed -> abandoned when a human gives
#   the run up as a recorded act.
WORK_ORDER_STATUSES = (
    "draft",
    "plan_ready",
    "plan_rejected",
    "approved",
    "running",
    "interrupted",
    "succeeded",
    "failed",
    "abandoned",
    "accepted",
    "rejected",
)


class WorkOrderIn(BaseModel):
    """How a work order arrives: from a gold task, or ad hoc."""

    title: str
    task_text: str
    file_scope: list[str]
    gold_task_id: str | None = None
    # Repo-relative test paths an ad-hoc order wants run as its
    # grading set. Gold tasks carry their grading tests in the suite.
    grading_tests: list[str] = Field(default_factory=list)
    # Ad-hoc orders only: the repo on disk to work against. Gold tasks
    # materialise their own target from the suite.
    target_path: str = ""
    requested_by: str = "console"


class WorkOrder(BaseModel):
    work_order_id: str
    title: str
    task_text: str
    file_scope: list[str]
    gold_task_id: str | None = None
    grading_tests: list[str] = Field(default_factory=list)
    target_path: str = ""
    status: str = "draft"
    requested_by: str = "console"
    created_at: str = ""
    updated_at: str = ""


class PlanStep(BaseModel):
    """One step of a plan: its intent, the files it touches, and the
    tests it expects to run."""

    intent: str
    files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    work_order_id: str
    version: int = 1
    approach: str
    # The plan itself: the work as an ordered list of steps. files
    # and tests are the derived unions over the steps, stored so the
    # shell's scope checks and every reader see the same facts.
    steps: list[PlanStep] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    model_profile: str = "offline"
    status: str = "draft"  # draft | approved | rejected
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    # Recorded at approval: the fingerprint of the approved scope in
    # the target repo, and the per-file digests behind it. Execution
    # recomputes both and refuses a drifted world as stale_plan.
    scope_fingerprint: str | None = None
    scope_digests: dict | None = None
    # For an amendment (version > 1 written by amend_plan): the diff
    # against the version it supersedes, and who amended it.
    diff_from_previous: dict | None = None
    amended_by: str | None = None
    created_at: str = ""


class Run(BaseModel):
    run_id: str
    work_order_id: str
    plan_version: int
    model_profile: str
    # running | succeeded | failed | interrupted | abandoned
    status: str = "running"
    workspace: str = ""
    changed_files: list[str] = Field(default_factory=list)
    patch: str = ""
    steps_used: int = 0
    failure_reason: str | None = None
    verification: dict = Field(default_factory=dict)
    # Round 5 — failure recovery. An interrupted run names why it
    # stopped and the last checkpoint it recorded; the checkpoint is
    # the basis a resume is checked against. An abandoned run names
    # who gave it up and why, and carries the partial patch — the
    # diff as far as it got — when edits had landed. A partial patch
    # is evidence for the humans, never a submittable patch: the
    # review gate takes SUCCEEDED runs only.
    interruption_reason: str | None = None
    last_checkpoint: str | None = None
    partial_patch: str = ""
    partial_files: list[str] = Field(default_factory=list)
    partial_checkpoint: str | None = None
    abandoned_by: str | None = None
    abandon_reason: str | None = None
    # Round 6 — budgets and the cost ledger. The ceilings in force
    # are snapshotted onto the run at start (a policy change never
    # moves a run already launched, and a resume spends against the
    # same snapshot and the same accumulated usage). A run stopped
    # by its budget names the ceiling in budget_stop; it lands
    # INTERRUPTED, refuses resume by name, and restarts only into a
    # fresh budget. elapsed_seconds counts the run's ACTIVE seconds
    # across attempts — time spent interrupted, waiting on a human,
    # is not the run's wall clock. cost is the priced fold of the
    # run's usage, recomputed at every landing.
    budget: dict = Field(default_factory=dict)
    budget_stop: str | None = None
    elapsed_seconds: float = 0.0
    cost: dict = Field(default_factory=dict)
    created_at: str = ""
    finished_at: str | None = None


class PlanDecisionIn(BaseModel):
    approve: bool
    actor: str
    note: str | None = None


class PlanStepIn(BaseModel):
    intent: str
    files: list[str]
    tests: list[str] = Field(default_factory=list)


class AmendPlanIn(BaseModel):
    """An amendment arrives as a full replacement step list: the new
    version is a whole plan, diffed against the one it supersedes."""

    approach: str
    steps: list[PlanStepIn]
    actor: str


class RunStartIn(BaseModel):
    # Naming a plan version pins the run to it; anything but the
    # latest version is refused as plan_superseded.
    plan_version: int | None = None
    actor: str = "console"


class ReviewDecisionIn(BaseModel):
    accept: bool
    actor: str
    reason: str | None = None


class RunActionIn(BaseModel):
    """Resume and restart arrive as a bare act: who is doing it."""

    actor: str = "console"


class RunAbandonIn(BaseModel):
    """Abandoning a run is a recorded act: who, and why."""

    actor: str
    reason: str


class BenchmarkIn(BaseModel):
    """A benchmark arrives as an act: which profile, over which
    set, recorded by whom."""

    profile_id: str
    set_name: str = "tuning"
    actor: str = "console"


class EvaluateIn(BaseModel):
    """A promotion evaluation: the held-out set, run ``times``
    times (the gate's default is the worst of 3)."""

    profile_id: str
    actor: str
    times: int = 3


class PromoteIn(BaseModel):
    """Promotion is a judged act: the profile, the judge, and the
    rationale the judgement stands on."""

    profile_id: str
    actor: str
    rationale: str


class RollbackIn(BaseModel):
    """Rollback is a recorded act too: who restored the previous
    default, and why."""

    actor: str
    rationale: str


class ComparisonIn(BaseModel):
    """A shortlist comparison arrives as an act: which benchmark
    set every profile runs over (the tuning set by default — the
    breadth set; the held-out set may be named instead), recorded
    by whom. The shortlist itself is not named in the request: a
    comparison always covers every recorded profile."""

    set_name: str = "tuning"
    actor: str = "console"
