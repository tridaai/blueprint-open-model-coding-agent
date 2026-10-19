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
WORK_ORDER_STATUSES = (
    "draft",
    "plan_ready",
    "plan_rejected",
    "approved",
    "running",
    "succeeded",
    "failed",
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
    status: str = "running"  # running | succeeded | failed
    workspace: str = ""
    changed_files: list[str] = Field(default_factory=list)
    patch: str = ""
    steps_used: int = 0
    failure_reason: str | None = None
    verification: dict = Field(default_factory=dict)
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
