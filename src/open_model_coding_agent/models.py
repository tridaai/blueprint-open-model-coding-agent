"""Record shapes for the governance shell.

Every state the system can be in is a stored record. API payloads,
store payloads, and service returns all use these shapes so a reader
of the database, the API, or the console sees the same facts.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# Who performs an act, as a request names them: stripped, and never
# empty. Actors are recorded, not authenticated — but a blank name
# records nobody, and a padded one would pass separation-of-duties
# checks as a second person.
ActorName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

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
#   Round 11 adds changes_requested: a review decision that sends a
#   succeeded run back — from succeeded, or from rejected (a
#   reviewer reversing a rejection into a send-back) — to a state a
#   fresh plan version can be drafted from, carrying the reviewer's
#   note on the order for that draft's context.
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
    "changes_requested",
)


class SkillMountIn(BaseModel):
    """One skill mount request (round 19): a skill by name, and
    the version to mount — an explicit version always names the
    text; an omitted one resolves at draft time to the latest
    published version. A mount is a request recorded on the
    order; whether the version stands published is judged when
    the plan is drafted, never assumed at intake."""

    name: str
    version: int | None = None


class WorkOrderIn(BaseModel):
    """How a work order arrives: from a gold task, or ad hoc."""

    title: str
    task_text: str
    file_scope: list[str]
    skills: list[SkillMountIn] = Field(default_factory=list)
    gold_task_id: str | None = None
    # Repo-relative test paths an ad-hoc order wants run as its
    # grading set. Gold tasks carry their grading tests in the suite.
    grading_tests: list[str] = Field(default_factory=list)
    # Ad-hoc orders only: the repo on disk to work against. Gold tasks
    # materialise their own target from the suite.
    target_path: str = ""
    requested_by: ActorName = "console"


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
    # Round 11: the latest request-changes note, stored on the order
    # by the review gate. The next plan draft is made with this note
    # in its context — it is how a reviewer's "send it back" reaches
    # the planner, as a stored record rather than a conversation.
    review_note: str | None = None
    # Round 12: when this order was instantiated from a task
    # template, its provenance travels on the record — the
    # template's name and version, the parameter values used, and
    # the substituted step skeleton the plan draft is shown. The
    # skeleton is a snapshot taken at instantiation: a later
    # template version can never rewrite what this order was
    # instantiated from.
    template: dict | None = None
    # Round 14: when this order was created as an item of a batch,
    # its provenance travels on the record too — the batch's id,
    # its name (a snapshot; batches are never renamed), and the
    # item's index in the submission. The batch's own state never
    # lands here: an order's status is its own, and a batch-level
    # mark (budget stopped, cancelled) lives on the batch record,
    # never written onto the order as a fake status.
    batch: dict | None = None
    # Round 19: the skill mounts this order asks for — each a
    # name and an optional version, recorded at intake exactly as
    # requested. The mounts are resolved at draft time against
    # the library and the publication record: only a published
    # version mounts, and the plan record then stamps the exact
    # version and content hash the model was shown. The request
    # is data; it can ask for context, never for scope, tools, or
    # a gate's silence.
    skills: list[dict] = Field(default_factory=list)
    # Round 19: set only by the skill gate's own evaluation path,
    # on the orders it creates to measure a candidate: those
    # orders may mount the candidate version before it stands
    # published — that measurement is what publication is judged
    # by. No intake surface can set it; it is the evaluation's
    # mark on its own work, recorded on the order like everything
    # else about it.
    skills_under_evaluation: bool = False
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
    # Round 12: when the draft was made with a task template's step
    # skeleton in its context, the plan carries the label — the
    # template's name and version, nothing more. The skeleton is
    # draft context only; this plan was still drafted for this
    # order and is still approved per order.
    template_context: dict | None = None
    # Round 19: when the draft was made with skills mounted, the
    # plan carries their stamps — each mounted skill's name,
    # version, and content hash. The stamp is the citation every
    # run of this plan is reproducible against: the exact text
    # the model was shown, named by its hash. The content itself
    # stays in the library, where the hash finds it.
    skills_context: list[dict] | None = None
    created_at: str = ""


class Run(BaseModel):
    run_id: str
    work_order_id: str
    plan_version: int
    model_profile: str
    # Round 16 — the deployment profile the run ran under, stamped
    # at launch from the settings in force: the record says which
    # governed shape the run executed under (local, hosted,
    # hosted-self). An additive payload field — no migration, the
    # payload is JSON in both dialects. Records written before
    # round 16 carry the default, and truthfully: every one of
    # them ran under what is now named local.
    deployment_profile: str = "local"
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
    actor: ActorName
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
    actor: ActorName


class RunStartIn(BaseModel):
    # Naming a plan version pins the run to it; anything but the
    # latest version is refused as plan_superseded.
    plan_version: int | None = None
    actor: ActorName = "console"


class ReviewDecisionIn(BaseModel):
    """A review decision arrives as one of three recorded acts:
    accept, reject, or request changes. ``decision`` names the act
    when given; ``accept`` remains for the two-act callers. A
    request-changes decision carries its note in ``reason`` — the
    note is required, and it is what the next plan draft sees."""

    accept: bool = True
    actor: ActorName
    reason: str | None = None
    decision: str | None = None  # accept | reject | request_changes


class RunActionIn(BaseModel):
    """Resume and restart arrive as a bare act: who is doing it."""

    actor: ActorName = "console"


class RunAbandonIn(BaseModel):
    """Abandoning a run is a recorded act: who, and why."""

    actor: ActorName
    reason: str


class BenchmarkIn(BaseModel):
    """A benchmark arrives as an act: which profile, over which
    set, recorded by whom."""

    profile_id: str
    set_name: str = "tuning"
    actor: ActorName = "console"


class EvaluateIn(BaseModel):
    """A promotion evaluation: the held-out set, run ``times``
    times (the gate's default is the worst of 3)."""

    profile_id: str
    actor: ActorName
    times: int = 3


class PromoteIn(BaseModel):
    """Promotion is a judged act: the profile, the judge, and the
    rationale the judgement stands on."""

    profile_id: str
    actor: ActorName
    rationale: str


class RollbackIn(BaseModel):
    """Rollback is a recorded act too: who restored the previous
    default, and why."""

    actor: ActorName
    rationale: str


class PromotionRejectIn(BaseModel):
    """The adverse close of a parked promotion-gate act (round 18):
    who rejects it, and why — both recorded beside the parked
    decision it closes."""

    actor: ActorName
    reason: str | None = None


class PromotionWithdrawIn(BaseModel):
    """Withdrawal of a parked promotion-gate act (round 18) by the
    actor who recorded it — the only actor who may."""

    actor: ActorName


class AcceptanceWithdrawIn(BaseModel):
    """Withdrawal of a parked review acceptance (round 18) by the
    reviewer who recorded it — the only actor who may."""

    actor: ActorName


class TaskTemplate(BaseModel):
    """A task template: a governed, versioned record in the task
    library. The patterns are the template — a task text with named
    parameters, a scope pattern naming the file shape an
    instantiated order will carry, and a step skeleton that
    arrives at plan drafting as labelled context. A version is
    usable only once published, and publishing is an act by an
    actor other than the version's author."""

    template_id: str
    name: str
    version: int
    description: str
    task_text: str
    parameters: list[str] = Field(default_factory=list)
    scope_pattern: list[str] = Field(default_factory=list)
    plan_steps: list[PlanStep] = Field(default_factory=list)
    status: str = "draft"  # draft | published
    author: str
    published_by: str | None = None
    published_at: str | None = None
    created_at: str = ""


class TemplateIn(BaseModel):
    """A template version arrives as a creation act: its author,
    its description of when to use it, and its patterns. The
    version number is the library's to assign — the next one for
    the name."""

    name: str
    description: str
    task_text: str
    parameters: list[str] = Field(default_factory=list)
    scope_pattern: list[str]
    steps: list[PlanStepIn]
    author: ActorName


class TemplatePublishIn(BaseModel):
    """Publishing is a recorded act: who publishes. The gate
    refuses the version's own author — publishing is a second
    person's judgement that the shape is sound."""

    actor: ActorName


class TemplateInstantiateIn(BaseModel):
    """Instantiation arrives as an act: which template (a named
    version, or the latest published when version is omitted), the
    parameter values, and where the order will work — the same
    target choices an ad-hoc order has. No plan arrives with it:
    a template can suggest a skeleton, never smuggle a plan."""

    version: int | None = None
    parameters: dict[str, str] = Field(default_factory=dict)
    actor: ActorName = "console"
    title: str | None = None
    target_path: str = ""
    grading_tests: list[str] = Field(default_factory=list)
    gold_task_id: str | None = None
    # A caller may restate the scope it expects; it must equal the
    # substituted scope pattern exactly. Anything else is an
    # attempt to widen the template's scope at intake, refused.
    file_scope: list[str] | None = None


class SkillEvaluateIn(BaseModel):
    """A skill evaluation arrives as a recorded act: who runs it,
    and how many paired passes over the tuning split — the
    candidate mounted against the unmounted baseline, each pass.
    One paired pass is the recorded minimum; more passes score
    by their worst, like the promotion gate's evaluations."""

    actor: ActorName
    times: int = Field(default=1, ge=1, le=5)


class SkillPublishIn(BaseModel):
    """Publishing a skill is a judged act: who publishes, and the
    rationale recorded with the act. The gate refuses the
    version's author and the evaluation's own recorder — the
    judgement must be a second person's, on evidence."""

    actor: ActorName
    rationale: str = ""


class SkillRollbackIn(BaseModel):
    """Rolling a skill back is a recorded act: who rolls back,
    and why. It un-publishes the latest published version, so
    mounts resolve to the previous published version again."""

    actor: ActorName
    rationale: str = ""


class ChallengeAnswerIn(BaseModel):
    """A challenge disposition arrives as a recorded act: which
    disposition, by whom, and its content — the rebuttal's text
    and the record it cites as counter-evidence, the revision
    note for an accept-and-revise, the downgrade's reason. Which
    fields a disposition requires is the gate's to judge, by
    kind."""

    kind: str  # accept_and_revise | rebut | downgrade
    actor: ActorName
    text: str | None = None
    cites: str | None = None
    reason: str | None = None


class ComparisonIn(BaseModel):
    """A shortlist comparison arrives as an act: which benchmark
    set every profile runs over (the tuning set by default — the
    breadth set; the held-out set may be named instead), recorded
    by whom. The shortlist itself is not named in the request: a
    comparison always covers every recorded profile."""

    set_name: str = "tuning"
    actor: ActorName = "console"


class Batch(BaseModel):
    """A batch: a named submission of work, stored as one record.

    A batch is NOT a comparison (round 8): a comparison benchmarks
    model profiles over a task set to inform the promotion gate
    and creates no work; a batch submits work to be done — a list
    of work-order specifications — and benchmarks nothing. Each
    item created one ordinary work order through ordinary intake
    and carries the batch-level state the item stands in:
    ``pending`` (nothing batch-level has been marked on it),
    ``budget_stopped`` (the batch budget fold crossed a ceiling
    before this item ever started), or ``cancelled`` (the batch
    was cancelled before this item started). Started-ness is
    never stored — it is folded from the order's own records, so
    the batch can never claim work began that did not.

    The batch's own fields move only by recorded acts: ``driver``
    stands while a drive pass is recorded active (and stays
    standing when a pass dies mid-flight, until a person releases
    it); ``budget_stop`` names the ceiling the fold crossed;
    cancellation records its actor, reason, and moment.
    """

    batch_id: str
    name: str
    actor: str  # the submitter
    status: str = "open"  # open | cancelled
    # The batch-level ceilings, on top of every run's own: any of
    # the three may be None, meaning that dimension is uncapped
    # for this batch.
    budget: dict = Field(default_factory=dict)
    items: list[dict] = Field(default_factory=list)
    driver: dict | None = None
    budget_stop: dict | None = None
    last_drive: dict | None = None
    cancelled_by: str | None = None
    cancel_reason: str | None = None
    cancelled_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


class BatchItemIn(BaseModel):
    """One item of a batch submission. ``kind`` names which
    intake it will go through — ``gold`` (a gold task id),
    ``adhoc`` (the WorkOrderIn fields), or ``template`` (a task
    library instantiation, the TemplateInstantiateIn fields) —
    and the fields of the other kinds are ignored. Validation is
    the service's: every item is validated before any is created,
    and the first invalid item refuses the whole batch by name."""

    kind: str  # gold | adhoc | template
    # gold
    gold_task_id: str | None = None
    # adhoc
    title: str | None = None
    task_text: str | None = None
    file_scope: list[str] | None = None
    grading_tests: list[str] = Field(default_factory=list)
    target_path: str = ""
    # template
    template_name: str | None = None
    template_version: int | None = None
    parameters: dict[str, str] = Field(default_factory=dict)


class BatchIn(BaseModel):
    """A batch arrives as a submission: its name, its submitter,
    its items, and optionally its budget ceilings — model calls,
    provider-measured tokens, and wall seconds for the batch as a
    whole, on top of each run's own ceilings."""

    name: str
    actor: str = "console"
    items: list[BatchItemIn]
    budget: dict | None = None


class BatchDriveIn(BaseModel):
    """Driving a batch is a recorded act: who drives this pass."""

    actor: ActorName = "console"


class BatchCancelIn(BaseModel):
    """Cancelling a batch is a recorded act: who, and why. Any
    named actor may cancel — cancellation stops work, it approves
    nothing — and the record names who did it."""

    actor: ActorName
    reason: str


class BatchReleaseDriverIn(BaseModel):
    """Releasing a dead driver mark is a recorded act: who, and
    why — the mark it clears names the pass that died holding it."""

    actor: ActorName
    reason: str
