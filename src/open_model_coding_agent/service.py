"""The service: work orders, the plan gate, runs, verification, review.

This is the governance shell. The model proposes; the shell disposes:
every plan draft passes the port's shared validator, whichever model
produced it; every model call's usage is drained from the port and
stored against the work order (and the run, during execution); and a
model-side failure during a run ends that run FAILED with the named
reason recorded, rather than crashing the shell.

- A work order is a stored record with a state machine. Nothing runs
  without a plan, and nothing runs from an unapproved plan —
  :meth:`start_run` refuses with a GateError unless the latest plan is
  approved. A plan is a versioned step list; approval records the
  scope fingerprint execution is checked against (drift is
  `stale_plan`), and an amendment is a new version with a recorded
  diff whose approval never carries from the version it supersedes
  (`plan_superseded`).
- A run works on an isolated copy of the target repo. The model's
  steps pass through the tool port, where scope and command allowlists
  are enforced in code. Every tool call is an audit record, and the
  record carries the tool's structured result verbatim — a refusal's
  stable code, an edit's anchor and digests, a search's truncation
  state — so the run's history shows not just what was asked but
  exactly what the tools answered. The affected-test set a run may
  ask for is computed by the tools from the plan's steps and the
  run's actual edits; the model can request it, never define it.
- The run's verdict is computed by the shell, never reported by the
  agent: the patch is applied to a fresh copy of the target and the
  grading tests run there. SUCCEEDED means exactly that the patch
  applies cleanly, touches only in-scope files, and the grading tests
  pass on the fresh copy.
- Verification has depth (round 10). Every verdict record carries
  the patch's shape facts (files, lines added/removed per file,
  binary, whitespace-only), whether the patch touched the
  test/grading surface, and which changed files no step of the
  approved plan named. For a gold task a test-surface or unplanned
  patch fails by name, and a visible pass is re-graded against the
  task's hidden grading set — staged only at that moment, into the
  verification copy — with a hidden failure flipping the verdict
  to FAILED as ``hidden_grading_failed``. The record distinguishes
  the visible and hidden verdicts.
- A human reviews a SUCCEEDED run: accept exports the .patch file,
  reject records the reason. Nothing is ever applied to the source
  repo by the agent's hand.
- Before that review can accept, the challenge pass (round 13)
  takes a second, independent look at the verified patch —
  deterministic rules computed from the run's stored records
  alone (the patch, the approved plan, the task text, the run's
  retained pristine snapshot; never the model's transcript or
  self-report): did the patch stray inside the files it was
  allowed to touch (``intent_creep``), introduce a figure no
  record carries (``single_source_figures``), or grow public
  surface nobody asked for (``unrequested_surface``)? Each fired
  rule stores a challenge record with its evidence. An
  unanswered must-answer challenge refuses acceptance by name;
  a reviewer answers each challenge with a recorded disposition
  — rebut it (naming the counter-evidence record), downgrade it
  (with a reason; the original severity stays on the record), or
  accept and revise (the order returns through the
  request-changes path, the challenge cited in the note).
  Challenges are never deleted: when a newer run supersedes the
  run they were raised on, they are marked superseded, and the
  pass itself is idempotent — re-running it over the same run
  stores nothing new.
- The review workbench (round 11) is folds, not state. The review
  queue is derived on read from the stored records — orders whose
  plans await a decision, whose runs stand SUCCEEDED awaiting
  review, FAILED with their failure class, or INTERRUPTED awaiting
  a recovery decision — each entry naming the one next act and its
  age; there is no queue table and no bulk decision anywhere, by
  construction. The review packet assembles everything a decision
  stands on for one order — task and scope, the approved plan the
  run ran on, the patch and its shape, the verification record
  with its risk flags distinct, usage and priced cost, the run
  history, and the prior decisions — and, once an order has been
  decided on an earlier run, names what changed since that packet.
  A third decision joins accept and reject: request changes,
  which stores the reviewer's note on the order and returns it to
  ``changes_requested``, a state a fresh plan version drafts from
  with the note in its context. Reviewer history folds the
  decision records by actor — counts by kind, the orders decided,
  and the median time from a run's landing to its decision.
- Runs record checkpoints as they go — one after each landed edit,
  one at each completed plan step — naming the workspace's digests,
  the tool calls so far, and the usage so far. A run that dies
  mid-flight (a transient model failure after retries, or a
  process-level crash) lands INTERRUPTED with the last checkpoint
  named, never a zombie 'running' row and never a silent FAILED;
  terminal model errors (a bad key, an unknown slug) still end a
  run FAILED, as resuming into the same wall would fail identically.
  Resume is decided in code against three bases — the plan version
  is still the approved one, the target repo still fingerprints as
  approved, and the workspace still digests to the last checkpoint
  — and any basis that moved refuses by name and offers restart
  instead. Restart discards the workspace and runs fresh under the
  same approval: an approval is not consumed by a run that did not
  finish. Abandoning a run is a recorded act that names the partial
  patch — the diff as far as it got — and a partial patch can never
  reach the review gate.
- A run spends against a budget, enforced in the shell — ceilings
  on model calls, provider-measured tokens, and active wall-clock
  seconds, snapshotted onto the run at start. The loop checks the
  budget before every model call and after every usage record
  lands; crossing a ceiling stops the run INTERRUPTED with the
  ceiling named, and a budget-stopped run cannot resume (a resume
  would overspend the same budget) — restart is its way back, into
  a fresh budget. Every run's usage is priced by the governed price
  table, in Decimal, and stored on the run as its cost: the drafting
  of its plan, and its execution. A slug the table leaves unpriced
  folds as unpriced — counted, with the reason named, never zero —
  and the scripted model's character accounting is never priced as
  tokens. The suite folds the same rows per profile into cost per
  successful task: all of a profile's priced spend over its
  verified successes.
- Which profile serves is itself a governed record. The gold suite
  is split into a tuning set and a held-out set (see
  :mod:`.benchmarks`), each named by a content hash over its tasks.
  A profile is benchmarked through this same service path — work
  orders, plan gate, runs, verification in code — and the records
  are stored. A promotion evaluation runs the held-out set N times
  and is scored by its worst run; any out-of-scope edit or
  unverified success in any run fails it outright. Promotion is a
  judged act: it needs a passing, current-hash evaluation, no
  regression against the incumbent default's own recorded
  baseline, and an actor other than the evaluation's. The default
  is the latest promotion event's target; rollback is a new event
  restoring the previous default. Nothing else can move it.
- The shortlist comparison (round 8) runs every recorded profile
  once over the same benchmark set through that same benchmark
  path, under one comparison actor, and stores the comparison as
  a record. A profile that cannot serve in this environment is
  recorded ``not_servable`` with the refusal's reason — never
  skipped, never faked. The report is a pure fold over the stored
  records (the per-task matrix, usage, priced cost and cost per
  successful task where the slug is priced, failure classes, and
  each profile's promotion standing read from the gate's own
  records); recomputing it changes nothing, and a comparison
  whose set has since moved reads as stale. The report ranks
  nothing and recommends nothing: the promotion gate remains the
  only path that moves the default, and the comparison record
  states that on its face.
- The task library (round 12) holds the shapes of recurring work
  as governed records: a template is a task-text pattern with
  named parameters, a scope pattern, and a step skeleton, stored
  versioned like a plan — an edit is a new version, never a
  mutation. A version is usable only once published, and
  publishing is an act by an actor other than its author:
  separation of duties, as at the promotion gate. Instantiating a
  template creates an ordinary work order through ordinary
  intake — parameters are substituted and validated in code (a
  value that lands in a path is a single path-segment token, so
  it can widen nothing; the substituted scope is re-checked entry
  by entry), the order carries the template's provenance and a
  snapshot of the substituted skeleton, and from there the order
  is indistinguishable from any other: same plan gate, same
  sandbox, same verification. The skeleton arrives at drafting as
  labelled context only — a template can suggest steps, never
  smuggle a plan; the plan is still drafted, and still approved,
  per order. Template health is a fold over the orders
  instantiated from a version, computed on read, never a stored
  counter. Publishing here is a human governed act — unlike the
  model profiles, templates are not promoted through benchmark
  evaluations; the evidence a template earns is its health fold,
  and the judgement to publish stays a person's.
- Batch work orders (round 14) submit many pieces of work as
  one named record — gold tasks, ad-hoc orders, and template
  instantiations, mixed — and change nothing about how any one
  of them is governed. A batch is not a comparison (round 8): a
  comparison benchmarks profiles to inform the promotion gate
  and creates no work; a batch creates work and benchmarks
  nothing. Creation validates every item first, and the first
  invalid item refuses the whole batch by name — no partial
  submission. Each item becomes an ordinary work order through
  ordinary intake, carrying the batch's provenance. A sequential
  driver then advances items one at a time through the acts
  that need no person — drafting a plan, running an approved
  plan — and stops at every gate a person owns, recording where
  it stopped on the batch record; it never runs two items
  concurrently, and a second pass while one is recorded active
  refuses by name (a pass that dies mid-flight leaves its mark
  standing until a person releases it, as a recorded act). The
  batch carries a budget of its own, folded on read from the
  items' usage records; when the fold crosses a ceiling, the
  unstarted items are marked ``budget_stopped`` on the batch
  record — the orders themselves stay in their true state — and
  the driver refuses to start further items by that name.
  Cancellation likewise marks only unstarted items, at batch
  level; started work is untouched and finishes through its own
  gates. The batch report is a fold like the workbench's:
  items by state, runs by outcome and failure class, challenges
  outstanding, priced cost and cost per verified success across
  the batch, and the items awaiting a person.
"""

from __future__ import annotations

import os
import shutil
import statistics
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .batches import (
    AWAITING_PERSON,
    NEXT_ACT_BY_STATUS,
    TERMINAL_ORDER_STATUSES,
    item_shape_problem,
    validate_batch_budget,
)
from .benchmarks import (
    BENCHMARK_SETS,
    DEFAULT_EVALUATION_RUNS,
    SET_HELD_OUT,
    normalise_actor,
    split_problems,
    task_set_hash,
)
from .challenges import compute_challenges
from .config import Settings, load_settings, resolve_profile
from .deployments import evaluate_rules, violated_rules
from .errors import ConfigError, GateError
from .gold import GoldSuite
from .logging_setup import log_event
from .model_port import Step, make_model, validate_plan_draft
from .models import (
    Batch,
    BatchIn,
    Plan,
    PlanStep,
    Run,
    TaskTemplate,
    TemplateIn,
    WorkOrder,
    WorkOrderIn,
)
from .patch import PatchError, apply_patch, build_patch, patch_shape
from .verification import planned_files, test_surface_files, unplanned_files
from .plans import diff_plans, drifted_files, file_digests, scope_fingerprint
from .prices import current_table, fold_usage, load_price_tables
from .profiles import list_profiles
from .store import SqlStore, utcnow
from .taxonomy import load_taxonomy
from .templates import (
    SEED_AUTHOR,
    SEED_PUBLISHER,
    check_substituted_scope,
    path_parameters,
    seed_template_specs,
    substitute,
    substitute_steps,
    validate_parameter_values,
    validate_template_shape,
)
from .tools import SandboxTools, dispatch

_IGNORE = shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "*.pyc")


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    """What a tree copy leaves behind: the usual debris, and every
    symlink. ``copytree`` dereferences links by default, which would
    pull whatever a target repo's links point at — files outside the
    repo — into the workspace, the pristine snapshot, and the
    verification copy as if they were repo content. Links are
    dropped at the copy, so nothing downstream ever sees their
    targets."""
    ignored = set(_IGNORE(directory, names))
    for name in names:
        if Path(directory, name).is_symlink():
            ignored.add(name)
    return ignored

# Model failures that mean "the world hiccupped", not "the work is
# wrong": the provider stayed rate-limited, overloaded, erroring, or
# unreachable through the adapter's bounded retries. A run that dies
# of one lands INTERRUPTED and may resume when the world recovers.
# Terminal model errors — auth, an unknown slug, a rejected or
# malformed exchange — are not in this set: they end the run FAILED,
# as they did in round 2.
_TRANSIENT_MODEL_CODES = frozenset(
    {
        "model_rate_limited",
        "model_overloaded",
        "model_server_error",
        "model_unreachable",
    }
)


class _BudgetStop(Exception):
    """The run crossed a budget ceiling. Carries the ceiling's name
    (``model_calls`` | ``tokens`` | ``wall_seconds``), the ceiling,
    and what the run had spent when the loop stopped it — the facts
    the interruption record and the audit trail name."""

    def __init__(self, ceiling: str, limit: int, used: int) -> None:
        super().__init__(f"budget_exhausted: {ceiling} ceiling {limit} reached at {used}")
        self.ceiling = ceiling
        self.limit = limit
        self.used = used


class _RunProgress:
    """Where a run has got to, tracked across attempts: the tool
    calls made, the files landed edits touched, the plan steps those
    edits completed, and the next checkpoint's index. A fresh run
    starts empty; a resumed run is seeded from stored records."""

    def __init__(
        self,
        *,
        tool_calls: int = 0,
        edited_files: list[str] | None = None,
        stepped: set[int] | None = None,
        next_index: int = 0,
    ) -> None:
        self.tool_calls = tool_calls
        self.edited_files = list(edited_files or [])
        self.stepped = set(stepped or set())
        self.next_index = next_index


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class CodingService:
    def __init__(self, settings: Settings, *, follow_default: bool = True) -> None:
        self.settings = settings
        # A primary service serves the governed default: the
        # configured profile until a promotion event moves it.
        # Services the benchmark runner builds to serve one named
        # candidate pass follow_default=False — they are pinned to
        # their settings' profile and never chase the default they
        # are being measured for.
        self._follow_default = follow_default
        self.store = SqlStore(settings)
        self.gold = GoldSuite(settings.gold_dir)
        # The price table is governed data, loaded fail-closed at
        # startup like the profiles: a deployment with no table, or
        # a malformed one, does not start half-priced.
        self.price_tables = load_price_tables(settings.prices_dir)
        self.price_table = current_table(self.price_tables)
        # The failure taxonomy is governed data too (round 15),
        # loaded fail-closed at startup like the price table: a
        # deployment classifies its failures under a recorded
        # version, or it does not start.
        self.taxonomy = load_taxonomy(settings.taxonomy_dir)
        # Test seam: the wall clock budgets read. Production uses
        # time.monotonic; tests may substitute a scripted clock.
        self._monotonic = time.monotonic
        for folder in (settings.targets_dir, settings.runs_dir, settings.artifacts_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self._reconcile_orphaned_runs()
        self._seed_templates()

    # -- audit ---------------------------------------------------------
    def _audit(self, entity: str, entity_id: str, event: str, actor: str, **detail):
        self.store.append_audit(entity, entity_id, event, actor, detail)

    # -- the serving profile ------------------------------------------------
    def default_profile_id(self) -> str:
        """The governed default: the latest promotion event's
        target, or the configured profile when no event exists.
        The configured profile is the seed a deployment starts
        with; from the first promotion on, the record rules."""
        events = self.store.list_promotion_events()
        return events[-1]["to_profile"] if events else self.settings.model_profile

    def serving_profile_id(self) -> str:
        """The profile this service serves work under: the governed
        default for a primary service; the settings' own profile
        for a pinned service (a benchmark candidate being measured
        serves itself, never the default it is measured against)."""
        if not self._follow_default:
            return self.settings.model_profile
        return self.default_profile_id()

    def serving_profile(self):
        """The serving profile's effective record. The configured
        profile resolves through the settings (its overrides were
        applied at load); any other profile — a promoted default —
        resolves through the same rules at serve time."""
        profile_id = self.serving_profile_id()
        if profile_id == self.settings.model_profile:
            return self.settings.profile
        return resolve_profile(self.settings, profile_id)

    # -- model usage -----------------------------------------------------
    def _persist_usage(self, model, *, work_order_id: str, run_id: str | None) -> None:
        """Drain the model's accounting into stored usage records,
        stamped with the call's context, the serving profile, and
        the price-table version in force — the version the row will
        be costed under, however the table moves later."""
        for record in model.drain_usage():
            self.store.append_usage(
                {
                    **record,
                    "run_id": run_id,
                    "work_order_id": work_order_id,
                    "profile_id": self.serving_profile_id(),
                    "price_table_version": self.price_table.version,
                }
            )

    def _attach_usage(self, run: dict) -> dict:
        """A run read carries its evidence with it: the usage rows
        and totals, the checkpoint history it recorded, and its
        priced cost. The cost fold is recomputed while the run is
        not terminal, so a run still standing interrupted shows what
        it has spent so far; a terminal run keeps the fold stored
        when it landed."""
        rows = self.store.list_usage(run_id=run["run_id"])
        run["usage"] = rows
        run["usage_totals"] = {
            "calls": len(rows),
            "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
            "completion_tokens": sum(r["completion_tokens"] for r in rows),
            "latency_ms": sum(r["latency_ms"] for r in rows),
        }
        run["checkpoints"] = self.store.list_checkpoints(run["run_id"])
        if run["status"] not in ("succeeded", "failed", "abandoned"):
            run["cost"] = self._cost_for_run(run)
        return run

    def _make_model(self, *, scripts=None):
        return make_model(
            self.serving_profile_id(),
            scripts=scripts,
            profile=self.serving_profile(),
            transport=self.settings.model_transport,
        )

    # -- budgets and the cost ledger --------------------------------------
    def _budget_snapshot(self) -> dict:
        """The ceilings a new run starts under, copied from policy
        onto the run record. From then on the run's snapshot rules
        it — not the policy, which a deployment may retune whenever
        it likes without moving runs already launched."""
        policy = self.settings.policy
        return {
            "max_model_calls": policy.max_model_calls,
            "max_tokens": policy.max_tokens,
            "max_wall_seconds": policy.max_wall_seconds,
        }

    def _budget_usage(self, run_id: str) -> tuple[int, int]:
        """What a run has spent, from its stored usage rows: the
        model calls recorded, and the provider-measured tokens.
        Scripted character accounting is not tokens and never counts
        toward the token ceiling."""
        rows = self.store.list_usage(run_id=run_id)
        tokens = sum(
            r["prompt_tokens"] + r["completion_tokens"]
            for r in rows
            if r["unit"] == "tokens"
        )
        return len(rows), tokens

    def _budget_breach(
        self, run: Run, *, after_call: bool, attempt_started: float
    ) -> _BudgetStop | None:
        """Has this run crossed a ceiling? Checked before each model
        call (``after_call=False``: at a ceiling, no further call is
        made) and after each usage record lands (``after_call=True``:
        the call that just landed may itself have crossed the token
        ceiling — the run stops there, so its overshoot is bounded
        by that single in-flight call)."""
        budget = run.budget or self._budget_snapshot()
        calls, tokens = self._budget_usage(run.run_id)
        elapsed = run.elapsed_seconds + (self._monotonic() - attempt_started)
        if calls > budget["max_model_calls"] or (
            not after_call and calls >= budget["max_model_calls"]
        ):
            return _BudgetStop("model_calls", budget["max_model_calls"], calls)
        if tokens > budget["max_tokens"] or (
            not after_call and tokens >= budget["max_tokens"]
        ):
            return _BudgetStop("tokens", budget["max_tokens"], tokens)
        if elapsed >= budget["max_wall_seconds"]:
            return _BudgetStop(
                "wall_seconds",
                budget["max_wall_seconds"],
                int(elapsed),
            )
        return None

    def _cost_for_run(self, run: dict) -> dict:
        """A run's priced cost, folded from its usage rows — each
        row priced under the table version stamped on it. The split:
        plan is the cost of drafting the plan this run's work order
        carries (the order's plan-phase rows; a work order's runs
        share the one drafting), execution is this run's own rows.
        Unpriced usage is counted with its reasons and kept out of
        the money, never zeroed."""
        execution_rows = self.store.list_usage(run_id=run["run_id"])
        order_rows = self.store.list_usage(work_order_id=run["work_order_id"])
        plan_rows = [r for r in order_rows if r["run_id"] is None]
        plan_fold = fold_usage(plan_rows, self.price_tables)
        execution_fold = fold_usage(execution_rows, self.price_tables)
        total = fold_usage(plan_rows + execution_rows, self.price_tables)
        return {
            "currency": "USD",
            "total": str(total.priced_cost),
            "plan": str(plan_fold.priced_cost),
            "execution": str(execution_fold.priced_cost),
            "priced_tokens": total.priced_tokens,
            "priced_calls": total.priced_calls,
            "unpriced_tokens": total.unpriced_tokens,
            "unpriced_calls": total.unpriced_calls,
            "scripted_calls": total.scripted_calls,
            "scripted_chars": total.scripted_chars,
            "price_table_versions": total.price_table_versions,
            "unpriced_reasons": total.unpriced_reasons,
        }

    def cost_report(self) -> dict:
        """The suite fold: per profile, what the work cost and what
        it bought. Runs and verified successes are counted from the
        run records; money is folded from ALL of the profile's usage
        rows — drafting, execution, successes, failures, and
        interruptions alike — because cost per successful task is
        honest only when the failures are in the numerator. Each
        row is priced under the table version stamped on it."""
        runs = self.store.all_runs()
        usage = self.store.list_usage()
        profiles: list[dict] = []
        seen = {r["model_profile"] for r in runs} | {u["profile_id"] for u in usage}
        for profile_id in sorted(seen):
            profile_runs = [r for r in runs if r["model_profile"] == profile_id]
            successes = sum(
                1 for r in profile_runs if r["status"] == "succeeded"
            )
            rows = [u for u in usage if u["profile_id"] == profile_id]
            fold = fold_usage(rows, self.price_tables)
            entry = fold.as_dict(successes=successes)
            entry = {
                "profile_id": profile_id,
                "runs": len(profile_runs),
                **entry,
            }
            profiles.append(entry)
        total_fold = fold_usage(usage, self.price_tables)
        total_successes = sum(1 for r in runs if r["status"] == "succeeded")
        return {
            "profiles": profiles,
            "total": {
                "runs": len(runs),
                **total_fold.as_dict(successes=total_successes),
            },
        }

    # -- observability (round 15) -------------------------------------------

    def operations_digest(
        self, *, since: str | None = None, until: str | None = None
    ) -> dict:
        """The operator digest: the deployment's standing, folded
        from the stored records on every read — no metrics table,
        no stored counters, so the digest can be re-derived and
        argued with like every other fold in this shell.

        The window filters each record family by its own
        ``created_at`` (runs, usage rows, audit records, challenges,
        batches); the default is all records. Challenge and batch
        standings are as they stand at read time — the fold reports
        what the records say now about the window's work, and says
        so in its derivation. Failures are counted by name (the
        workbench's failure class off the run record) and classified
        under the governed taxonomy; a name the taxonomy does not
        carry counts as ``unclassified`` under its own name — never
        dropped, because records outlive the build that wrote them.
        """
        since_dt = _parse_ts(since) if since else None
        until_dt = _parse_ts(until) if until else None
        if (since and since_dt is None) or (until and until_dt is None):
            raise GateError(
                "invalid_digest_window",
                "the digest window's timestamps must be ISO "
                "timestamps the shell can read",
            )
        if since_dt is not None and until_dt is not None and since_dt > until_dt:
            raise GateError(
                "invalid_digest_window",
                "the digest window's start stands after its end",
            )

        def in_window(record: dict) -> bool:
            stamp = _parse_ts(record.get("created_at"))
            if stamp is None:
                return since_dt is None and until_dt is None
            if since_dt is not None and stamp < since_dt:
                return False
            if until_dt is not None and stamp > until_dt:
                return False
            return True

        runs = [run for run in self.store.all_runs() if in_window(run)]
        runs_by_status: dict[str, int] = {}
        failures_by_name: dict[str, int] = {}
        verified_successes = 0
        for run in runs:
            runs_by_status[run["status"]] = (
                runs_by_status.get(run["status"], 0) + 1
            )
            if run["status"] == "succeeded":
                verified_successes += 1
            if run["status"] in ("failed", "interrupted"):
                name, _detail = self._run_failure_class(run)
                failures_by_name[name] = failures_by_name.get(name, 0) + 1
        failures_by_class: dict[str, int] = {}
        unclassified: dict[str, int] = {}
        classified: dict[str, dict] = {}
        for name, count in failures_by_name.items():
            entry = self.taxonomy.classify(name)
            classified[name] = entry
            failures_by_class[entry["class"]] = (
                failures_by_class.get(entry["class"], 0) + count
            )
            if entry["class"] == "unclassified":
                unclassified[name] = count
        top_failures = [
            {
                "name": name,
                "count": count,
                "class": classified[name]["class"],
                "class_label": self.taxonomy.class_label(
                    classified[name]["class"]
                ),
                "cause": classified[name]["cause"],
                "action": classified[name]["action"],
            }
            for name, count in sorted(
                failures_by_name.items(), key=lambda item: (-item[1], item[0])
            )[:10]
        ]
        names_by_class: dict[str, list[str]] = {}
        for name in failures_by_name:
            names_by_class.setdefault(classified[name]["class"], []).append(name)
        failure_classes = [
            {
                "id": class_id,
                "label": self.taxonomy.class_label(class_id),
                "count": count,
                "names": sorted(names_by_class.get(class_id, [])),
            }
            for class_id, count in sorted(
                failures_by_class.items(), key=lambda item: (-item[1], item[0])
            )
        ]

        decision_events = self.store.list_audit_events(
            [
                "plan_drafted",
                "plan_approved",
                "plan_rejected",
                "plan_amended",
                "review_accepted",
                "review_rejected",
                "review_changes_requested",
            ]
        )
        decisions = [row for row in decision_events if in_window(row)]

        def count_event(event: str) -> int:
            return sum(1 for row in decisions if row["event"] == event)

        challenges = [
            challenge
            for challenge in self.store.list_challenges()
            if in_window(challenge)
        ]
        challenges_by_rule: dict[str, int] = {}
        challenges_by_status: dict[str, int] = {}
        open_must = 0
        open_advisory = 0
        for challenge in challenges:
            challenges_by_rule[challenge["rule"]] = (
                challenges_by_rule.get(challenge["rule"], 0) + 1
            )
            challenges_by_status[challenge["status"]] = (
                challenges_by_status.get(challenge["status"], 0) + 1
            )
            if challenge["status"] == "open":
                if challenge["current_severity"] == "must_answer":
                    open_must += 1
                else:
                    open_advisory += 1

        batches = [
            batch for batch in self.store.list_batches() if in_window(batch)
        ]
        batches_by_standing: dict[str, int] = {}
        batch_items = 0
        for batch in batches:
            standing = self._batch_report(batch)["status"]
            batches_by_standing[standing] = (
                batches_by_standing.get(standing, 0) + 1
            )
            batch_items += len(batch["items"])

        usage_rows = [row for row in self.store.list_usage() if in_window(row)]
        fold = fold_usage(usage_rows, self.price_tables)

        return {
            "window": {"since": since, "until": until},
            "taxonomy_version": self.taxonomy.version,
            "deployment_profile": self.settings.deployment.name,
            "deployment_profile_version": self.settings.deployment.version,
            "runs": {"total": len(runs), "by_status": runs_by_status},
            "failures": {
                "total": sum(failures_by_name.values()),
                "by_name": failures_by_name,
                "by_class": failures_by_class,
                "classes": failure_classes,
                "unclassified": unclassified,
                "top": top_failures,
            },
            "plan_decisions": {
                "drafted": count_event("plan_drafted"),
                "approved": count_event("plan_approved"),
                "rejected": count_event("plan_rejected"),
                "amended": count_event("plan_amended"),
            },
            "review_decisions": {
                "accepted": count_event("review_accepted"),
                "rejected": count_event("review_rejected"),
                "changes_requested": count_event("review_changes_requested"),
            },
            "challenges": {
                "raised": len(challenges),
                "by_rule": challenges_by_rule,
                "by_status": challenges_by_status,
                "outstanding": {
                    "open": open_must + open_advisory,
                    "must_answer": open_must,
                    "advisory": open_advisory,
                },
            },
            "batches": {
                "total": len(batches),
                "by_standing": batches_by_standing,
                "items": batch_items,
            },
            "usage": {
                "calls": len(usage_rows),
                "tokens": sum(
                    row["prompt_tokens"] + row["completion_tokens"]
                    for row in usage_rows
                    if row["unit"] == "tokens"
                ),
                "scripted_chars": sum(
                    row["prompt_tokens"] + row["completion_tokens"]
                    for row in usage_rows
                    if row["unit"] == "chars"
                ),
            },
            "cost": fold.as_dict(successes=verified_successes),
            "derivation": (
                "folded on read from the stored records — runs, "
                "usage, audit, challenges, batches — over the "
                "window each record's created_at falls in; nothing "
                "is stored and no counter is kept. Failures are "
                f"classified under failure taxonomy "
                f"v{self.taxonomy.version}; challenge and batch "
                "standings are as they stand at read time."
            ),
        }

    def readiness(self) -> dict:
        """The deployment's readiness, probed live and reported by
        name — never as a stack trace. Four probings: the store
        answers and its migrations stand current against the ones
        on disk; the serving profile can actually serve (a live
        profile recorded not servable, or missing its key, is
        not-ready with the reason named); the governed data in
        force (the price table's version; the taxonomy's, in the
        digest); and the deployment profile (round 16) — its name,
        version, and content hash, with every declared rule
        re-evaluated live and any violated rule named as a problem.
        ``/health`` stays the cheap liveness check; this is the
        deployment check."""
        problems: list[str] = []
        try:
            migrations = self.store.migration_status()
            store_reachable = True
            store_error = None
        except Exception as exc:  # the probe reports; it never raises
            migrations = None
            store_reachable = False
            store_error = f"{type(exc).__name__}: {exc}"
            problems.append("the store did not answer its probe")
        if migrations is not None and not migrations["current"]:
            problems.append(
                "the store's migrations stand behind the ones on "
                f"disk: {migrations['behind']}"
            )

        profile = None
        profile_reason: str | None = None
        try:
            profile = self.serving_profile()
        except ConfigError as exc:
            profile_reason = str(exc)
        if profile is not None and profile.is_live:
            if not profile.servable:
                profile_reason = (
                    profile.not_servable_reason
                    or "recorded servable: false"
                )
            elif not os.environ.get(profile.api_key_env or ""):
                profile_reason = (
                    f"model profile {profile.profile_id!r} is live "
                    f"and needs {profile.api_key_env} set in the "
                    f"environment"
                )
        profile_state = (
            "ready" if profile is not None and profile_reason is None
            else "not_ready"
        )
        if profile_reason is not None:
            problems.append(f"the serving profile cannot serve: "
                            f"{profile_reason}")

        # Round 16: the deployment profile in force, with its rules
        # re-evaluated live — the same evaluation settings load
        # enforced, probed again here so the read reports the
        # states as they stand, per rule, with the record's version
        # and content hash naming exactly which record governs.
        deployment = self.settings.deployment
        deployment_rules = evaluate_rules(
            deployment,
            model_profile=(
                profile if profile is not None else self.settings.profile
            ),
            store_kind=self.settings.store_kind,
            environ=os.environ,
        )
        for state in violated_rules(deployment_rules):
            problems.append(
                f"deployment profile {deployment.name!r} rule "
                f"{state['rule']!r} is violated: {state['detail']}"
            )

        return {
            "status": "ready" if not problems else "not_ready",
            "store": self.settings.store_kind,
            "store_reachable": store_reachable,
            "store_error": store_error,
            "migrations": migrations,
            "deployment_profile": deployment.name,
            "deployment_profile_version": deployment.version,
            "deployment_profile_hash": deployment.content_hash,
            "deployment_rules": deployment_rules,
            "model_profile": self.serving_profile_id(),
            "model_label": profile.label if profile is not None else None,
            "model_state": {
                "state": profile_state,
                "reason": profile_reason,
            },
            "default_model_profile": self.default_profile_id(),
            "baseten_base_url": profile.base_url if profile is not None else None,
            "baseten_model": profile.model if profile is not None else None,
            "price_table_version": self.price_table.version,
            "taxonomy_version": self.taxonomy.version,
            "problems": problems,
        }

    # -- the task library (round 12) ---------------------------------------
    def _seed_templates(self) -> None:
        """Ship the library's seed templates through the same
        creation path as any template: created by the seed author,
        published by the seed reviewer — separation of duties
        holds for the seeds too. A name the deployment already has
        (any version, any state) is left entirely alone: the seed
        yields to the deployment's own template of that name."""
        for spec in seed_template_specs():
            if self.store.list_templates(spec["name"]):
                continue
            body = TemplateIn(
                name=spec["name"],
                description=spec["description"],
                task_text=spec["task_text"],
                parameters=list(spec["parameters"]),
                scope_pattern=list(spec["scope_pattern"]),
                steps=spec["steps"],
                author=SEED_AUTHOR,
            )
            record = self.create_template(body)
            self.publish_template(
                record["name"], record["version"], actor=SEED_PUBLISHER
            )

    def create_template(self, body: TemplateIn) -> dict:
        """Create a template version — a recorded act by its
        author. The version number is the library's to assign: the
        next for the name. The record starts unpublished; creating
        a version never edits an earlier one."""
        steps = [step.model_dump() for step in body.steps]
        validate_template_shape(
            name=body.name,
            description=body.description,
            task_text=body.task_text,
            parameters=list(body.parameters),
            scope_pattern=list(body.scope_pattern),
            steps=steps,
        )
        existing = self.store.list_templates(body.name)
        version = (existing[-1]["version"] + 1) if existing else 1
        record = TaskTemplate(
            template_id=_new_id("tpl"),
            name=body.name,
            version=version,
            description=body.description,
            task_text=body.task_text,
            parameters=list(body.parameters),
            scope_pattern=list(body.scope_pattern),
            plan_steps=[PlanStep(**step) for step in steps],
            author=body.author,
            created_at=utcnow(),
        )
        self.store.put_template(record.model_dump())
        self._audit(
            "task_template",
            record.name,
            "template_created",
            body.author,
            template_id=record.template_id,
            version=version,
            parameters=record.parameters,
        )
        log_event(
            "template_created", template=record.name, version=version,
            actor=body.author,
        )
        return record.model_dump()

    def publish_template(
        self, name: str, version: int, *, actor: str
    ) -> dict:
        """Publish a template version — the act that makes it
        instantiable. Publishing is a second person's judgement:
        the version's own author is refused by name, on normalised
        names, as at the promotion gate."""
        record = self.store.get_template(name, version)
        if record is None:
            raise GateError(
                "unknown_template",
                f"no template {name!r} version {version} is recorded",
            )
        if record["status"] == "published":
            raise GateError(
                "template_already_published",
                f"template {name!r} version {version} is already "
                f"published (by {record.get('published_by')!r}) — "
                f"publishing is an act, not a state to re-assert",
            )
        if normalise_actor(actor) == normalise_actor(record["author"]):
            raise GateError(
                "template_self_publish",
                f"publishing refused: {actor!r} authored template "
                f"{name!r} version {version} and cannot also "
                f"publish it; publishing is a second person's "
                f"judgement that the shape is sound",
            )
        record["status"] = "published"
        record["published_by"] = actor
        record["published_at"] = utcnow()
        self.store.put_template(record)
        self._audit(
            "task_template",
            name,
            "template_published",
            actor,
            template_id=record["template_id"],
            version=version,
            author=record["author"],
        )
        log_event(
            "template_published", template=name, version=version, actor=actor
        )
        return record

    def get_template(
        self, name: str, version: int | None = None
    ) -> dict | None:
        """One template record — the named version, or the latest
        version of the name — with its health folded in on read."""
        record = self.store.get_template(name, version)
        if record is None:
            return None
        return {**record, "health": self.template_health(name, record["version"])}

    def list_templates(self) -> list[dict]:
        """Every template version on record, each with its health
        folded in on read."""
        return [
            {
                **record,
                "health": self.template_health(
                    record["name"], record["version"]
                ),
            }
            for record in self.store.list_templates()
        ]

    def template_audit(self, name: str) -> list[dict]:
        return self.store.list_audit("task_template", name)

    def template_health(self, name: str, version: int) -> dict:
        """A template version's health, folded on read from the
        orders instantiated from it — never a stored counter, so
        the fold can be re-derived and argued with. Counts: the
        orders created; their runs by outcome (succeeded, failed,
        interrupted, abandoned, still running); failures by the
        same classes the review workbench names; review decisions
        and the acceptance rate among decided reviews; and the
        median priced cost of a verified success, over the
        successes whose usage the price table actually priced —
        unpriced successes are counted and kept out of the money."""
        orders = [
            order
            for order in self.store.list_work_orders()
            if (order.get("template") or {}).get("name") == name
            and (order.get("template") or {}).get("version") == version
        ]
        runs: list[dict] = []
        decisions = {"accepted": 0, "rejected": 0, "changes_requested": 0}
        for order in orders:
            runs.extend(self.store.list_runs(order["work_order_id"]))
            for decision in self._review_decisions(order["work_order_id"]):
                if decision["kind"] in decisions:
                    decisions[decision["kind"]] += 1
        by_status: dict[str, int] = {}
        failures_by_class: dict[str, int] = {}
        priced_costs: list[Decimal] = []
        successes = 0
        for run in runs:
            by_status[run["status"]] = by_status.get(run["status"], 0) + 1
            if run["status"] in ("failed", "interrupted"):
                failure_class, _detail = self._run_failure_class(run)
                failures_by_class[failure_class] = (
                    failures_by_class.get(failure_class, 0) + 1
                )
            if run["status"] == "succeeded":
                successes += 1
                cost = run.get("cost") or {}
                if int(cost.get("priced_calls") or 0) > 0:
                    priced_costs.append(Decimal(str(cost.get("total", "0"))))
        decided = decisions["accepted"] + decisions["rejected"]
        return {
            "orders_created": len(orders),
            "runs": len(runs),
            "runs_by_status": by_status,
            "runs_succeeded": successes,
            "failures_by_class": failures_by_class,
            "reviews": decisions,
            "acceptance_rate": (
                decisions["accepted"] / decided if decided else None
            ),
            "priced_successes": len(priced_costs),
            "median_cost_per_success": (
                str(statistics.median(priced_costs)) if priced_costs else None
            ),
        }

    def _resolve_template_version(
        self, name: str, version: int | None
    ) -> dict:
        """The template version an instantiation will use. An
        explicit version always serves if it is published; an
        omitted one resolves to the latest published version.
        Anything else refuses by name — an unpublished version can
        be read and compared, never instantiated."""
        versions = self.store.list_templates(name)
        if not versions:
            raise GateError(
                "unknown_template", f"no template named {name!r} is recorded"
            )
        if version is not None:
            record = next(
                (r for r in versions if r["version"] == version), None
            )
            if record is None:
                raise GateError(
                    "unknown_template_version",
                    f"template {name!r} has no version {version} — "
                    f"recorded versions: {[r['version'] for r in versions]}",
                )
            if record["status"] != "published":
                raise GateError(
                    "template_not_published",
                    f"instantiation refused: template {name!r} "
                    f"version {version} is recorded but not "
                    f"published; an unpublished version can be read "
                    f"and compared, never instantiated",
                )
            return record
        published = [r for r in versions if r["status"] == "published"]
        if not published:
            raise GateError(
                "template_not_published",
                f"instantiation refused: template {name!r} has no "
                f"published version — publishing is the act that "
                f"makes a version usable",
            )
        return published[-1]

    def _plan_template_instantiation(
        self,
        name: str,
        *,
        version: int | None = None,
        parameters: dict | None = None,
        file_scope: list[str] | None = None,
    ) -> tuple[dict, dict, str, list[str], list[dict]]:
        """Everything an instantiation computes before it creates
        anything: the resolved published version, the validated
        parameter values, the substituted task text, the
        substituted and re-checked scope, and the substituted
        step skeleton. Pure reads and named refusals only — the
        batch round (14) validates a whole submission by planning
        each template item through this and creating nothing, so
        what creation would refuse is refused before the first
        item of the batch exists."""
        record = self._resolve_template_version(name, version)
        steps_as_dicts = [dict(step) for step in record["plan_steps"]]
        values = validate_parameter_values(
            list(record["parameters"]),
            dict(parameters or {}),
            path_params=path_parameters(
                list(record["scope_pattern"]), steps_as_dicts
            ),
        )
        task_text = substitute(record["task_text"], values)
        scope = [
            substitute(entry, values) for entry in record["scope_pattern"]
        ]
        check_substituted_scope(scope)
        skeleton = substitute_steps(steps_as_dicts, values)
        if file_scope is not None and list(file_scope) != scope:
            raise GateError(
                "template_scope_widening_refused",
                f"instantiation refused: the requested file scope "
                f"{list(file_scope)} is not the scope the template "
                f"pattern produces {scope} — a template's scope is "
                f"part of the published record; widening it at "
                f"intake would make the parameter check theatre",
            )
        return record, values, task_text, scope, skeleton

    def instantiate_template(
        self,
        name: str,
        *,
        version: int | None = None,
        parameters: dict | None = None,
        actor: str = "console",
        title: str | None = None,
        target_path: str = "",
        grading_tests: list[str] | None = None,
        gold_task_id: str | None = None,
        file_scope: list[str] | None = None,
        batch_provenance: dict | None = None,
    ) -> dict:
        """Instantiate a template into an ordinary work order,
        through ordinary intake. The parameters are substituted
        into the template's patterns and validated on the way —
        values that land in paths are single path-segment tokens,
        the substituted scope is re-checked entry by entry, and a
        caller restating a different scope is refused as widening.
        The order carries the provenance and a snapshot of the
        substituted skeleton; from creation on it is an ordinary
        order — same plan gate, same sandbox, same verification.
        No plan arrives with it: the skeleton will be shown to the
        plan draft as labelled context, and the plan is still
        drafted and approved per order."""
        record, values, task_text, scope, skeleton = (
            self._plan_template_instantiation(
                name,
                version=version,
                parameters=parameters,
                file_scope=file_scope,
            )
        )
        provenance = {
            "name": record["name"],
            "version": record["version"],
            "parameters": values,
            "steps": skeleton,
        }
        default_title = record["name"] + (
            " — "
            + ", ".join(
                f"{key}={values[key]}" for key in sorted(values)
            )
            if values
            else ""
        )
        order = self.create_work_order(
            WorkOrderIn(
                title=title or default_title,
                task_text=task_text,
                file_scope=scope,
                gold_task_id=gold_task_id,
                grading_tests=list(grading_tests or []),
                target_path=target_path,
                requested_by=actor,
            ),
            actor=actor,
            template_provenance=provenance,
            batch_provenance=batch_provenance,
        )
        self._audit(
            "task_template",
            name,
            "template_instantiated",
            actor,
            template_id=record["template_id"],
            version=record["version"],
            work_order_id=order["work_order_id"],
            parameters=values,
        )
        log_event(
            "template_instantiated", template=name,
            version=record["version"], work_order_id=order["work_order_id"],
            actor=actor,
        )
        return order

    # -- benchmarks and the promotion gate ----------------------------------
    def benchmark_sets(self) -> dict:
        """The named benchmark sets and what each measures now:
        its tasks and its current content hash."""
        self._require_sound_split()
        return {
            name: {
                "tasks": list(task_ids),
                "task_set_hash": task_set_hash(self.gold, name),
            }
            for name, task_ids in BENCHMARK_SETS.items()
        }

    def _require_sound_split(self) -> None:
        problems = split_problems(self.gold)
        if problems:
            raise ConfigError(
                "benchmark split does not partition the gold suite: "
                + "; ".join(problems)
            )

    def _benchmark_service(self, profile_id: str) -> "CodingService":
        """A sibling service over the same store, pinned to one
        candidate profile: benchmarks run through the REAL service
        path — work orders, plan gate, sandboxed runs, verification
        in code — with the candidate serving, and their records
        land in the same system of record as all other work. The
        sibling is pinned (follow_default=False): being measured
        for the default must never mean serving it."""
        try:
            settings = load_settings(
                sqlite_path=self.settings.sqlite_path,
                database_url=self.settings.database_url,
                profiles_dir=self.settings.profiles_dir,
                prices_dir=self.settings.prices_dir,
                migrations_dir=self.settings.migrations_dir,
                gold_dir=self.settings.gold_dir,
                targets_dir=self.settings.targets_dir,
                runs_dir=self.settings.runs_dir,
                artifacts_dir=self.settings.artifacts_dir,
                policy=self.settings.policy,
                model_profile=profile_id,
                model_transport=self.settings.model_transport,
            )
        except ConfigError as exc:
            raise GateError("profile_not_servable", str(exc)) from None
        return CodingService(settings, follow_default=False)

    def run_benchmark(
        self, profile_id: str, *, set_name: str, actor: str
    ) -> dict:
        """Run one benchmark: the named set, once, under the named
        profile, through the real service path. Every task becomes
        a gold work order — drafted, approved by the benchmark
        actor, run — and the verdict is the shell's verification,
        never the model's word. The record stores per-task outcomes
        (verified success, or the failure class), the usage, the
        priced cost, and the set's content hash, so it can only
        ever be compared against the identical set."""
        if set_name not in BENCHMARK_SETS:
            raise GateError(
                "unknown_benchmark_set",
                f"no benchmark set named {set_name!r}; the sets are "
                f"{sorted(BENCHMARK_SETS)}",
            )
        self._require_sound_split()
        bench = self._benchmark_service(profile_id)
        profile = bench.serving_profile()
        set_hash = task_set_hash(self.gold, set_name)
        outcomes: list[dict] = []
        for task_id in BENCHMARK_SETS[set_name]:
            task = self.gold.get(task_id)
            assert task is not None
            outcomes.append(
                self._benchmark_task(bench, task, actor=actor)
            )
        record = {
            "benchmark_id": _new_id("bench"),
            "profile_id": profile_id,
            "model": profile.model,
            "set_name": set_name,
            "task_set_hash": set_hash,
            "actor": actor,
            "outcomes": outcomes,
            "successes": sum(
                1 for o in outcomes if o["verified_success"]
            ),
            "tasks": len(outcomes),
            "violations": sum(
                1 for o in outcomes if o["failure_class"] in (
                    "out_of_scope_edit", "unverified_success",
                )
            ),
            "priced_cost": str(
                sum(
                    (Decimal(o["cost"]["priced_cost"]) for o in outcomes),
                    Decimal(0),
                )
            ),
            "created_at": utcnow(),
        }
        self.store.append_benchmark_run(record)
        log_event(
            "benchmark_recorded", profile_id=profile_id, set_name=set_name,
            successes=record["successes"], tasks=record["tasks"],
        )
        return record

    def _benchmark_task(self, bench: "CodingService", task, *, actor: str) -> dict:
        """One task of a benchmark, end to end. A governance
        refusal anywhere in the path is recorded as the task's
        outcome — a benchmark reports what happened, it does not
        stop at the first refusal."""
        outcome: dict = {
            "task_id": task.id,
            "work_order_id": None,
            "run_id": None,
            "status": "refused",
            "verified_success": False,
            "out_of_scope_files": [],
            "failure_class": None,
            "detail": "",
            "usage": {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latency_ms": 0,
            },
            "cost": {
                "priced_cost": "0",
                "unpriced_tokens": 0,
                "scripted_chars": 0,
            },
        }
        try:
            order = bench.create_gold_work_order(task.id, actor=actor)
            wid = order["work_order_id"]
            outcome["work_order_id"] = wid
            bench.draft_plan(wid, actor=actor)
            bench.decide_plan(wid, approve=True, actor=actor)
            run = bench.start_run(wid, actor=actor)
        except GateError as exc:
            outcome["failure_class"] = f"refused_{exc.code}"
            outcome["detail"] = exc.message
            return outcome
        outcome["run_id"] = run["run_id"]
        outcome["status"] = run["status"]
        totals = run.get("usage_totals") or {}
        outcome["usage"] = {
            "calls": totals.get("calls", 0),
            "prompt_tokens": totals.get("prompt_tokens", 0),
            "completion_tokens": totals.get("completion_tokens", 0),
            "latency_ms": totals.get("latency_ms", 0),
        }
        cost = run.get("cost") or {}
        outcome["cost"] = {
            "priced_cost": cost.get("total", "0"),
            "unpriced_tokens": cost.get("unpriced_tokens", 0),
            "scripted_chars": cost.get("scripted_chars", 0),
        }
        out_of_scope = [
            f for f in run.get("changed_files") or []
            if f not in task.file_scope
        ]
        outcome["out_of_scope_files"] = out_of_scope
        verified = bool((run.get("verification") or {}).get("passed"))
        if out_of_scope:
            # Zero tolerance. The tool port and the verification
            # already make this structurally unreachable — scope is
            # enforced at the edit, and a patch touching out-of-scope
            # files fails verification — but the record is checked
            # anyway: the gate trusts stored records, not arguments
            # about why they must be fine.
            outcome["failure_class"] = "out_of_scope_edit"
            outcome["detail"] = (
                f"patch touched files outside the approved scope: "
                f"{out_of_scope}"
            )
        elif run["status"] == "succeeded" and not verified:
            outcome["failure_class"] = "unverified_success"
            outcome["detail"] = (
                "run stands succeeded without a passing verification"
            )
        elif (
            run["status"] == "succeeded"
            and (run.get("verification") or {}).get("hidden_verdict") != "pass"
        ):
            # Round 10: a gold run's success stands on both verdicts.
            # Verification already makes this unreachable (a hidden
            # failure flips passed to False, and SUCCEEDED follows
            # passed) — the record is checked anyway, in the gate's
            # usual mould.
            outcome["failure_class"] = "unverified_success"
            outcome["detail"] = (
                "run stands succeeded without a passing hidden re-grade "
                f"(hidden verdict: "
                f"{(run.get('verification') or {}).get('hidden_verdict')})"
            )
        elif run["status"] == "succeeded" and verified:
            outcome["verified_success"] = True
        elif run["status"] == "interrupted":
            outcome["failure_class"] = "run_interrupted"
            outcome["detail"] = run.get("interruption_reason") or ""
        else:
            outcome["failure_class"] = "run_failed"
            outcome["detail"] = (
                (run.get("verification") or {}).get("reason")
                or run.get("failure_reason")
                or ""
            )
        return outcome

    def evaluate_profile(
        self,
        profile_id: str,
        *,
        actor: str,
        times: int = DEFAULT_EVALUATION_RUNS,
        set_name: str = SET_HELD_OUT,
    ) -> dict:
        """A promotion evaluation: the held-out set, run ``times``
        times, folded into one stored record. The score is the
        WORST run's verified successes — one good run is weather;
        the worst of N is the claim. Any out-of-scope edit or
        unverified success in any run fails the evaluation
        outright, whatever the counts say."""
        if times < 1:
            raise GateError(
                "invalid_evaluation_runs",
                f"an evaluation runs the set at least once, got {times}",
            )
        benches = [
            self.run_benchmark(profile_id, set_name=set_name, actor=actor)
            for _ in range(times)
        ]
        violations: list[dict] = []
        for bench in benches:
            for outcome in bench["outcomes"]:
                if outcome["failure_class"] in (
                    "out_of_scope_edit",
                    "unverified_success",
                ):
                    violations.append(
                        {
                            "benchmark_id": bench["benchmark_id"],
                            "task_id": outcome["task_id"],
                            "kind": outcome["failure_class"],
                        }
                    )
        record = {
            "evaluation_id": _new_id("eval"),
            "profile_id": profile_id,
            "model": benches[0]["model"],
            "set_name": set_name,
            "task_set_hash": benches[0]["task_set_hash"],
            "actor": actor,
            "runs": times,
            "benchmark_ids": [b["benchmark_id"] for b in benches],
            "successes_per_run": [b["successes"] for b in benches],
            "worst_successes": min(b["successes"] for b in benches),
            "set_size": benches[0]["tasks"],
            "violations": violations,
            "passed": not violations,
            "priced_cost": str(
                sum(
                    (Decimal(b["priced_cost"]) for b in benches),
                    Decimal(0),
                )
            ),
            "created_at": utcnow(),
        }
        self.store.append_promotion_evaluation(record)
        log_event(
            "promotion_evaluated", profile_id=profile_id,
            worst_successes=record["worst_successes"],
            set_size=record["set_size"], passed=record["passed"],
        )
        return record

    def _latest_evaluation(self, profile_id: str) -> dict | None:
        """The profile's latest held-out evaluation — the one a
        promotion judges by. A newer evaluation supersedes an older
        one by being later; results cannot be shopped."""
        evaluations = [
            e
            for e in self.store.list_promotion_evaluations(profile_id)
            if e["set_name"] == SET_HELD_OUT
        ]
        return evaluations[-1] if evaluations else None

    def promote(
        self, profile_id: str, *, actor: str, rationale: str
    ) -> dict:
        """Make a profile the default — a judged act, not a config
        edit. The gate, in order: a held-out evaluation for the
        candidate exists, is current (the set's hash has not
        moved), and passed; the actor is not the actor who recorded
        that evaluation; and the candidate's worst run does not
        regress against the incumbent default's own recorded
        baseline. Every refusal names its rule."""
        current = self.default_profile_id()
        current_hash = task_set_hash(self.gold, SET_HELD_OUT)
        evaluation = self._latest_evaluation(profile_id)
        if evaluation is None:
            raise GateError(
                "no_passing_evaluation",
                f"promotion refused: no held-out evaluation is recorded "
                f"for profile {profile_id!r}; run evaluate_profile first "
                f"— a profile becomes the default on evidence, not on "
                f"nomination",
            )
        if evaluation["task_set_hash"] != current_hash:
            raise GateError(
                "evaluation_stale",
                f"promotion refused: the evaluation of {profile_id!r} "
                f"was recorded against task-set hash "
                f"{evaluation['task_set_hash'][:12]}…, but the held-out "
                f"set now hashes to {current_hash[:12]}… — the set "
                f"moved, so the result no longer describes it; "
                f"re-running the benchmark is the only way back",
            )
        if not evaluation["passed"]:
            raise GateError(
                "no_passing_evaluation",
                f"promotion refused: the latest held-out evaluation of "
                f"{profile_id!r} did not pass — zero-tolerance "
                f"violations are on its record "
                f"({len(evaluation['violations'])}); a candidate with "
                f"a violation in any run cannot become the default",
            )
        if normalise_actor(actor) == normalise_actor(evaluation["actor"]):
            raise GateError(
                "self_promotion",
                f"promotion refused: {actor!r} recorded the evaluation "
                f"of {profile_id!r} and cannot also promote it; "
                f"promotion is a second person's judgement on the "
                f"evidence",
            )
        if profile_id != current:
            baseline = self._latest_evaluation(current)
            if baseline is None:
                raise GateError(
                    "no_passing_evaluation",
                    f"promotion refused: the incumbent default "
                    f"{current!r} has no recorded held-out evaluation "
                    f"to judge regression against; record its baseline "
                    f"(evaluate_profile on the incumbent) first — the "
                    f"bar a candidate must clear is a measured one, "
                    f"never a remembered one",
                )
            if baseline["task_set_hash"] != current_hash:
                raise GateError(
                    "evaluation_stale",
                    f"promotion refused: the incumbent default "
                    f"{current!r}'s baseline evaluation was recorded "
                    f"against a task-set hash that has since moved; "
                    f"re-run its benchmark before judging a candidate "
                    f"against it",
                )
            if evaluation["worst_successes"] < baseline["worst_successes"]:
                raise GateError(
                    "regression",
                    f"promotion refused: candidate {profile_id!r}'s "
                    f"worst held-out run verified "
                    f"{evaluation['worst_successes']} of "
                    f"{evaluation['set_size']} tasks, below the "
                    f"incumbent {current!r}'s recorded baseline of "
                    f"{baseline['worst_successes']}; an equal score "
                    f"promotes — no regression is the bar, not "
                    f"must-beat — but a worse one does not",
                )
        event = {
            "event_id": _new_id("promo"),
            "kind": "promotion",
            "from_profile": current,
            "to_profile": profile_id,
            "actor": actor,
            "rationale": rationale,
            "evaluation_id": evaluation["evaluation_id"],
            "task_set_hash": current_hash,
            "created_at": utcnow(),
        }
        self.store.append_promotion_event(event)
        log_event(
            "profile_promoted", profile_id=profile_id,
            from_profile=current, actor=actor,
        )
        return {**event, "default_profile_id": profile_id}

    def rollback(self, *, actor: str, rationale: str) -> dict:
        """Restore the previous default — its own recorded act.
        Rollback needs no evaluation: it returns to a state that
        was itself promoted on evidence. It is refused only when
        there is no history to roll back to."""
        events = self.store.list_promotion_events()
        if not events:
            raise GateError(
                "no_promotion_history",
                "rollback refused: no promotion has been recorded, so "
                "there is no previous default to restore; the "
                "configured profile stands",
            )
        last = events[-1]
        current = last["to_profile"]
        target = last["from_profile"]
        event = {
            "event_id": _new_id("promo"),
            "kind": "rollback",
            "from_profile": current,
            "to_profile": target,
            "actor": actor,
            "rationale": rationale,
            "evaluation_id": None,
            "task_set_hash": task_set_hash(self.gold, SET_HELD_OUT),
            "created_at": utcnow(),
        }
        self.store.append_promotion_event(event)
        log_event(
            "profile_rolled_back", profile_id=target,
            from_profile=current, actor=actor,
        )
        return {**event, "default_profile_id": target}

    def promotion_state(self) -> dict:
        """The gate's whole position, as recorded: the default and
        where it came from, the sets and their current hashes,
        every evaluation, and the full promotion history."""
        return {
            "default_profile_id": self.default_profile_id(),
            "configured_profile_id": self.settings.model_profile,
            "serving_profile_id": self.serving_profile_id(),
            "sets": self.benchmark_sets(),
            "evaluations": self.store.list_promotion_evaluations(),
            "events": self.store.list_promotion_events(),
        }

    def list_benchmarks(self, profile_id: str | None = None) -> list[dict]:
        return self.store.list_benchmark_runs(profile_id)

    # -- shortlist comparisons (round 8) ------------------------------------
    def run_comparison(
        self, *, set_name: str = "tuning", actor: str = "console"
    ) -> dict:
        """Run one shortlist comparison: EVERY recorded profile,
        once, over the same benchmark set, through the benchmark
        path above, all under one comparison actor — and store the
        comparison as a record naming each profile's benchmark.

        The shortlist is the profiles directory as recorded: the
        five Baseten family profiles and the offline profile in
        the shipped set, whatever a deployment's directory holds
        in its place. A profile that cannot serve in this
        environment — a live profile whose key is not set, an
        override outside its allowlist — is recorded on the
        comparison as ``not_servable`` with the refusal's own
        reason: never silently skipped, and never faked with a
        substitute's results. The comparison is evidence only —
        producing one never moves the default; the promotion gate
        above is the only path that does, and the record states
        that on its face. Returns the comparison report, folded
        from the stored records."""
        if set_name not in BENCHMARK_SETS:
            raise GateError(
                "unknown_benchmark_set",
                f"no benchmark set named {set_name!r}; the sets are "
                f"{sorted(BENCHMARK_SETS)}",
            )
        self._require_sound_split()
        from .comparisons import COMPARISON_NOTE

        entries: list[dict] = []
        for profile in list_profiles(self.settings.profiles_dir):
            profile_id = profile.profile_id
            try:
                bench = self.run_benchmark(
                    profile_id, set_name=set_name, actor=actor
                )
            except GateError as exc:
                if exc.code != "profile_not_servable":
                    raise
                entries.append(
                    {
                        "profile_id": profile_id,
                        "model": profile.model,
                        "status": "not_servable",
                        "benchmark_ids": [],
                        "reason": exc.message,
                    }
                )
                continue
            entries.append(
                {
                    "profile_id": profile_id,
                    "model": bench["model"],
                    "status": "benchmarked",
                    "benchmark_ids": [bench["benchmark_id"]],
                    "reason": None,
                }
            )
        record = {
            "comparison_id": _new_id("cmp"),
            "set_name": set_name,
            "task_set_hash": task_set_hash(self.gold, set_name),
            "tasks": list(BENCHMARK_SETS[set_name]),
            "actor": actor,
            "entries": entries,
            "note": COMPARISON_NOTE,
            "created_at": utcnow(),
        }
        self.store.append_comparison(record)
        log_event(
            "comparison_recorded",
            comparison_id=record["comparison_id"],
            set_name=set_name,
            actor=actor,
            benchmarked=sum(
                1 for e in entries if e["status"] == "benchmarked"
            ),
            not_servable=sum(
                1 for e in entries if e["status"] == "not_servable"
            ),
        )
        return self._comparison_report(record)

    def _comparison_report(self, record: dict) -> dict:
        """One comparison's report: a pure fold over the stored
        records — the comparison, the benchmarks it names, those
        benchmarks' usage rows, and the promotion records. The
        set's CURRENT hash is computed here, at read time, so a
        comparison recorded before a task edit reads as stale;
        the stored record itself is never rewritten."""
        from .comparisons import build_report

        benchmark_ids = [
            bid
            for entry in record["entries"]
            for bid in entry["benchmark_ids"]
        ]
        stored = {b["benchmark_id"]: b for b in self.store.list_benchmark_runs()}
        benchmarks = [stored[bid] for bid in benchmark_ids if bid in stored]
        usage_rows: list[dict] = []
        seen_orders: set[str] = set()
        for bench in benchmarks:
            for outcome in bench["outcomes"]:
                wid = outcome.get("work_order_id")
                if wid and wid not in seen_orders:
                    seen_orders.add(wid)
                    usage_rows.extend(
                        self.store.list_usage(work_order_id=wid)
                    )
        return build_report(
            record,
            benchmarks=benchmarks,
            usage_rows=usage_rows,
            price_tables=self.price_tables,
            evaluations=self.store.list_promotion_evaluations(),
            default_profile_id=self.default_profile_id(),
            current_set_hash=task_set_hash(self.gold, record["set_name"]),
            held_out_hash=task_set_hash(self.gold, SET_HELD_OUT),
        )

    def get_comparison(self, comparison_id: str) -> dict | None:
        """One comparison's report, or None — the record is read
        fresh and folded on every call, so the report always
        shows the promotion standing and staleness of right now."""
        record = self.store.get_comparison(comparison_id)
        return self._comparison_report(record) if record else None

    def list_comparisons(self) -> list[dict]:
        """Every recorded comparison, each folded into its report."""
        return [
            self._comparison_report(record)
            for record in self.store.list_comparisons()
        ]

    # -- batch work orders (round 14) --------------------------------------
    def _require_batch(self, batch_id: str) -> dict:
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise GateError(
                "unknown_batch", f"no batch {batch_id!r} is recorded"
            )
        return batch

    def _save_batch(self, batch: dict) -> None:
        batch["updated_at"] = utcnow()
        self.store.put_batch(batch)

    def create_batch(self, body: BatchIn) -> dict:
        """Submit a batch: a named list of work-order
        specifications, validated whole before any of it exists.

        Validation is two passes over the submission and writes
        nothing: the shape of every item (its kind, the fields
        that kind's intake cannot proceed without), then the
        semantics — the gold task exists, the template resolves
        to a published version with its parameters validated and
        its scope substituted and re-checked (the same planning
        an instantiation performs, creating nothing). The first
        invalid item refuses the whole batch by name, with the
        item's index in the refusal. Only then does creation run,
        item by item, through the ordinary intakes — gold,
        ad-hoc, template — each order carrying the batch's
        provenance, each governed exactly as if it had arrived
        alone. Returns the batch report, folded on read."""
        name = (body.name or "").strip()
        if not name:
            raise GateError(
                "invalid_batch",
                "a batch is a named submission — this one carries "
                "no name",
            )
        actor = (body.actor or "").strip()
        if not actor:
            raise GateError(
                "invalid_batch",
                "a batch names its submitter — the actor is "
                "missing",
            )
        if not body.items:
            raise GateError(
                "batch_empty",
                f"batch {name!r} submits no items — a batch with "
                f"no items is not a submission",
            )
        budget = validate_batch_budget(body.budget)
        specs = [
            self._validate_batch_item(raw, index)
            for index, raw in enumerate(body.items)
        ]
        # Validation is complete; nothing above this line has
        # written a record. Creation goes through ordinary intake.
        batch_id = _new_id("bat")
        items: list[dict] = []
        for index, spec in enumerate(specs):
            provenance = {
                "batch_id": batch_id,
                "name": name,
                "index": index,
            }
            order = self._create_batch_item(
                spec, actor=actor, batch_provenance=provenance
            )
            items.append(
                {
                    "index": index,
                    "kind": spec["kind"],
                    "spec": spec,
                    "work_order_id": order["work_order_id"],
                    "item_state": "pending",
                }
            )
        record = Batch(
            batch_id=batch_id,
            name=name,
            actor=actor,
            status="open",
            budget=budget,
            items=items,
            created_at=utcnow(),
            updated_at=utcnow(),
        ).model_dump()
        self.store.put_batch(record)
        self._audit(
            "batch", batch_id, "batch_created", actor,
            name=name, items=len(items),
            kinds=[spec["kind"] for spec in specs], budget=budget,
        )
        log_event(
            "batch_created", batch_id=batch_id, name=name,
            items=len(items), actor=actor,
        )
        return self._batch_report(record)

    def _validate_batch_item(self, raw, index: int) -> dict:
        """One item of a submission, validated and normalised —
        the shape first, then the semantics its intake will meet.
        Every refusal carries the item's index and keeps the
        intake's own refusal name: the batch refuses as the item
        would, named for the item."""
        shape = {
            "kind": raw.kind,
            "gold_task_id": raw.gold_task_id,
            "template_name": raw.template_name,
            "title": raw.title,
            "task_text": raw.task_text,
            "file_scope": (
                list(raw.file_scope) if raw.file_scope is not None else None
            ),
        }
        problem = item_shape_problem(shape)
        if problem:
            raise GateError(
                "invalid_batch_item", f"batch item {index}: {problem}"
            )
        if raw.gold_task_id and self.gold.get(raw.gold_task_id) is None:
            raise GateError(
                "unknown_gold_task",
                f"batch item {index}: no gold task named "
                f"{raw.gold_task_id!r}",
            )
        if raw.kind == "gold":
            return {"kind": "gold", "gold_task_id": raw.gold_task_id}
        if raw.kind == "template":
            try:
                record, values, _text, _scope, _skeleton = (
                    self._plan_template_instantiation(
                        raw.template_name,
                        version=raw.template_version,
                        parameters=dict(raw.parameters or {}),
                        file_scope=(
                            list(raw.file_scope)
                            if raw.file_scope is not None
                            else None
                        ),
                    )
                )
            except GateError as exc:
                raise GateError(
                    exc.code, f"batch item {index}: {exc.message}"
                ) from None
            return {
                "kind": "template",
                "template_name": record["name"],
                "template_version": record["version"],
                "parameters": values,
                "title": raw.title,
                "target_path": raw.target_path,
                "grading_tests": list(raw.grading_tests or []),
                "gold_task_id": raw.gold_task_id,
                "file_scope": (
                    list(raw.file_scope)
                    if raw.file_scope is not None
                    else None
                ),
            }
        return {
            "kind": "adhoc",
            "title": raw.title,
            "task_text": raw.task_text,
            "file_scope": list(raw.file_scope or []),
            "grading_tests": list(raw.grading_tests or []),
            "target_path": raw.target_path,
            "gold_task_id": raw.gold_task_id,
        }

    def _create_batch_item(
        self, spec: dict, *, actor: str, batch_provenance: dict
    ) -> dict:
        """Create one validated item's work order through the
        ordinary intake its kind names. Nothing here is a batch
        shortcut: the order that comes back is the order that
        intake makes, with the batch's provenance added."""
        if spec["kind"] == "gold":
            return self.create_gold_work_order(
                spec["gold_task_id"],
                actor=actor,
                batch_provenance=batch_provenance,
            )
        if spec["kind"] == "template":
            return self.instantiate_template(
                spec["template_name"],
                version=spec["template_version"],
                parameters=spec["parameters"],
                actor=actor,
                title=spec["title"],
                target_path=spec["target_path"],
                grading_tests=spec["grading_tests"],
                gold_task_id=spec["gold_task_id"],
                file_scope=spec["file_scope"],
                batch_provenance=batch_provenance,
            )
        return self.create_work_order(
            WorkOrderIn(
                title=spec["title"],
                task_text=spec["task_text"],
                file_scope=spec["file_scope"],
                gold_task_id=spec["gold_task_id"],
                grading_tests=spec["grading_tests"],
                target_path=spec["target_path"],
                requested_by=actor,
            ),
            actor=actor,
            batch_provenance=batch_provenance,
        )

    def get_batch(self, batch_id: str) -> dict | None:
        """One batch's report, folded on read, or None."""
        record = self.store.get_batch(batch_id)
        return self._batch_report(record) if record else None

    def list_batches(self) -> list[dict]:
        """Every recorded batch, each folded into its report."""
        return [
            self._batch_report(record)
            for record in self.store.list_batches()
        ]

    def batch_audit(self, batch_id: str) -> list[dict]:
        return self.store.list_audit("batch", batch_id)

    def _batch_item_started(self, work_order_id: str) -> bool:
        """Has work begun on this order? Folded from its own
        records — a plan, a run, or a single usage row (a plan
        draft that was refused still spent the call). The batch
        never stores this judgement: a batch-level mark may only
        land on an item this fold calls unstarted, and the fold
        is recomputed at the moment of the mark."""
        return (
            self.store.get_plan(work_order_id) is not None
            or bool(self.store.list_runs(work_order_id))
            or bool(self.store.list_usage(work_order_id=work_order_id))
        )

    def _batch_spend(self, batch: dict) -> dict:
        """What the batch has spent, folded from its items' usage
        records: the model calls (every usage row the orders
        recorded, drafting included), the provider-measured
        tokens (scripted character accounting is not tokens and
        never counts toward the token ceiling — round 6's rule),
        and the wall seconds (the sum of its runs' active
        seconds, the quantity a run's own ceiling measures;
        drafting is model latency recorded per call, not a run,
        and carries no wall record of its own)."""
        calls = 0
        tokens = 0
        wall = 0.0
        for item in batch["items"]:
            wid = item["work_order_id"]
            rows = self.store.list_usage(work_order_id=wid)
            calls += len(rows)
            tokens += sum(
                r["prompt_tokens"] + r["completion_tokens"]
                for r in rows
                if r["unit"] == "tokens"
            )
            wall += sum(
                float(run.get("elapsed_seconds") or 0)
                for run in self.store.list_runs(wid)
            )
        return {
            "model_calls": calls,
            "tokens": tokens,
            "wall_seconds": wall,
        }

    @staticmethod
    def _batch_budget_breach(
        spend: dict, budget: dict
    ) -> tuple[str, int, float] | None:
        """The first batch ceiling the spend fold has reached, in
        the budget's own order — calls, tokens, wall seconds — or
        None. Reaching a ceiling is crossing it: the fold is
        checked before an item starts, so a fold at the ceiling
        starts nothing further."""
        dimensions = (
            ("max_model_calls", "model_calls"),
            ("max_tokens", "tokens"),
            ("max_wall_seconds", "wall_seconds"),
        )
        for ceiling, key in dimensions:
            limit = (budget or {}).get(ceiling)
            if limit is not None and spend[key] >= limit:
                return ceiling, limit, spend[key]
        return None

    def _mark_batch_budget_stopped(
        self,
        batch: dict,
        *,
        from_index: int,
        ceiling: str,
        limit: int,
        used: float,
        actor: str,
    ) -> None:
        """Mark every unstarted item from ``from_index`` on as
        budget_stopped, on the batch record only — the orders
        keep their true state (they stand at draft, unplanned,
        unrun), and the mark names why they will not start under
        this batch. Recorded per item and for the batch."""
        marked: list[int] = []
        for item in batch["items"]:
            if item["index"] < from_index:
                continue
            if item["item_state"] != "pending":
                continue
            if self._batch_item_started(item["work_order_id"]):
                continue
            item["item_state"] = "budget_stopped"
            marked.append(item["index"])
            self._audit(
                "batch", batch["batch_id"],
                "batch_item_budget_stopped", actor,
                index=item["index"],
                work_order_id=item["work_order_id"],
                ceiling=ceiling, limit=limit, used=used,
            )
        batch["budget_stop"] = {
            "ceiling": ceiling,
            "limit": limit,
            "used": used,
            "at": utcnow(),
            "items": marked,
        }
        self._save_batch(batch)
        self._audit(
            "batch", batch["batch_id"], "batch_budget_stopped",
            actor, ceiling=ceiling, limit=limit, used=used,
            items=marked,
        )
        log_event(
            "batch_budget_stopped", batch_id=batch["batch_id"],
            ceiling=ceiling, items=len(marked),
        )

    def drive_batch(self, batch_id: str, actor: str = "console") -> dict:
        """One drive pass: advance the batch's items, one at a
        time, in submission order, through the acts that need no
        person — and stop each item at the gate a person owns.

        The pass does only this per item: draft a plan where one
        is draftable, run an approved plan, and otherwise record
        where the item waits. It never approves a plan, never
        reviews a run, never resumes or restarts anything — those
        are recorded acts by named people, and the batch does not
        borrow their authority. Items run strictly one at a time:
        sequential is the design, not a limitation (the house
        rule for verification), and there is no parallel path.

        The pass is itself a recorded act. It marks itself active
        on the batch record before it starts and clears the mark
        when it completes, storing the pass's run sheet as the
        batch's last drive; a second pass while a mark stands
        refuses ``batch_driver_active``. A pass that dies
        mid-flight (an exception that is not a governance
        refusal) leaves its mark standing — the record says a
        driver was here and did not finish — until a person
        releases it with :meth:`release_batch_driver`. When the
        budget fold has reached a ceiling, the pass marks the
        unstarted items from the item it reached onward as
        budget_stopped, completes, and then refuses by name —
        the refusal is the pass's outcome, and the marks stand
        either way."""
        batch = self._require_batch(batch_id)
        if batch["status"] == "cancelled":
            raise GateError(
                "batch_cancelled",
                f"batch {batch['name']!r} stands cancelled (by "
                f"{batch.get('cancelled_by')}: "
                f"{batch.get('cancel_reason')}); a cancelled batch "
                f"drives nothing",
            )
        standing = batch.get("driver")
        if standing:
            raise GateError(
                "batch_driver_active",
                f"a drive pass on batch {batch['name']!r} is "
                f"already recorded active (actor "
                f"{standing.get('actor')}, since "
                f"{standing.get('started_at')}); if that pass "
                f"died, release its mark as a recorded act before "
                f"driving again",
            )
        batch["driver"] = {"actor": actor, "started_at": utcnow()}
        self._save_batch(batch)
        self._audit(
            "batch", batch_id, "batch_drive_started", actor,
            items=len(batch["items"]),
        )
        entries: list[dict] = []
        stopped_reason: str | None = None
        refusal: GateError | None = None
        completed = False
        try:
            for item in batch["items"]:
                entry, item_refusal = self._drive_item(
                    batch, item, actor
                )
                entries.append(entry)
                if item_refusal is not None:
                    stopped_reason = "batch_budget_exhausted"
                    refusal = item_refusal
                    break
            completed = True
        finally:
            if completed:
                batch["driver"] = None
                batch["last_drive"] = {
                    "actor": actor,
                    "at": utcnow(),
                    "entries": entries,
                    "stopped_reason": stopped_reason,
                }
                self._save_batch(batch)
                self._audit(
                    "batch", batch_id, "batch_drive_stopped", actor,
                    entries=len(entries),
                    stopped_reason=stopped_reason,
                    drafted=sum(
                        1
                        for e in entries
                        if e.get("action") == "drafted_plan"
                    ),
                    ran=sum(
                        1 for e in entries if e.get("action") == "ran"
                    ),
                )
        if refusal is not None:
            raise refusal
        return batch["last_drive"]

    def _drive_item(
        self, batch: dict, item: dict, actor: str
    ) -> tuple[dict, GateError | None]:
        """Advance one item as far as a person-free act takes it.
        Returns the run-sheet entry, and the pass's refusal when
        the budget stops the pass at this item."""
        entry: dict = {
            "index": item["index"],
            "work_order_id": item["work_order_id"],
            "kind": item["kind"],
        }
        if item["item_state"] == "cancelled":
            entry.update(
                action="skipped",
                detail="cancelled at batch level before it started",
            )
            return entry, None
        if item["item_state"] == "budget_stopped":
            entry.update(
                action="skipped",
                detail="stopped at batch level by the batch budget",
            )
            return entry, None
        order = self.store.get_work_order(item["work_order_id"])
        if order is None:
            entry.update(
                action="skipped",
                detail="the item's work order is not on record",
            )
            return entry, None
        entry["from_status"] = order["status"]
        if not self._batch_item_started(order["work_order_id"]):
            breach = self._batch_budget_breach(
                self._batch_spend(batch), batch["budget"]
            )
            if breach is not None:
                ceiling, limit, used = breach
                self._mark_batch_budget_stopped(
                    batch,
                    from_index=item["index"],
                    ceiling=ceiling,
                    limit=limit,
                    used=used,
                    actor=actor,
                )
                entry.update(
                    action="refused_budget",
                    detail=(
                        f"the batch budget's {ceiling} ceiling "
                        f"({limit}) is already spent ({used}); this "
                        f"item and every unstarted item after it "
                        f"are marked budget_stopped"
                    ),
                )
                return entry, GateError(
                    "batch_budget_exhausted",
                    f"driving batch {batch['name']!r} refuses to "
                    f"start item {item['index']}: the batch "
                    f"budget's {ceiling} ceiling ({limit}) is "
                    f"already spent ({used}) — the unstarted "
                    f"items are marked budget_stopped on the "
                    f"batch record, and their orders stand "
                    f"untouched in their true state",
                )
        status = order["status"]
        try:
            if status in ("draft", "plan_rejected", "changes_requested"):
                self.draft_plan(order["work_order_id"], actor=actor)
                entry.update(
                    action="drafted_plan", stopped_at="plan_gate"
                )
            elif status == "approved":
                run = self.start_run(order["work_order_id"], actor=actor)
                entry.update(
                    action="ran",
                    run_id=run["run_id"],
                    run_status=run["status"],
                    stopped_at=(
                        "review_gate"
                        if run["status"] == "succeeded"
                        else None
                    ),
                )
            elif status == "running":
                entry.update(
                    action="waiting",
                    detail="a run is in flight on this order",
                )
            elif status in TERMINAL_ORDER_STATUSES:
                entry.update(action="done")
            else:
                entry.update(
                    action="waiting",
                    stopped_at=(
                        "plan_gate"
                        if status == "plan_ready"
                        else "review_gate"
                        if status == "succeeded"
                        else None
                    ),
                )
        except GateError as exc:
            entry.update(
                action="stopped", error=exc.code, detail=exc.message
            )
        fresh = self.store.get_work_order(order["work_order_id"])
        entry["to_status"] = fresh["status"] if fresh else status
        entry["next_act"] = NEXT_ACT_BY_STATUS.get(entry["to_status"])
        return entry, None

    def cancel_batch(
        self, batch_id: str, *, actor: str, reason: str
    ) -> dict:
        """Cancel a batch, as a recorded act. The rule: any named
        actor may cancel — cancellation stops work, it approves
        nothing, and the record names who did it and why. Only
        unstarted items are marked, cancelled at batch level on
        the batch record; their orders remain readable in their
        true state (draft, unplanned) and are never deleted.
        Started items are untouched: work a person or the driver
        has begun finishes through its own gates, under its own
        approvals, exactly as if the batch were not there."""
        if not reason or not reason.strip():
            raise GateError(
                "batch_cancel_reason_required",
                "cancelling a batch is a recorded act — it names "
                "its reason",
            )
        batch = self._require_batch(batch_id)
        if batch["status"] == "cancelled":
            raise GateError(
                "batch_already_cancelled",
                f"batch {batch['name']!r} already stands "
                f"cancelled (by {batch.get('cancelled_by')})",
            )
        marked: list[int] = []
        for item in batch["items"]:
            if item["item_state"] != "pending":
                continue
            if self._batch_item_started(item["work_order_id"]):
                continue
            item["item_state"] = "cancelled"
            marked.append(item["index"])
        batch["status"] = "cancelled"
        batch["cancelled_by"] = actor
        batch["cancel_reason"] = reason
        batch["cancelled_at"] = utcnow()
        self._save_batch(batch)
        self._audit(
            "batch", batch_id, "batch_cancelled", actor,
            reason=reason, items_cancelled=marked,
        )
        log_event(
            "batch_cancelled", batch_id=batch_id, actor=actor,
            items_cancelled=len(marked),
        )
        return self._batch_report(batch)

    def release_batch_driver(
        self, batch_id: str, *, actor: str, reason: str
    ) -> dict:
        """Release a drive pass's mark that never cleared — the
        pass died mid-flight — as a recorded act naming who
        releases it and why. The mark it clears stays in the
        audit trail (the pass's start, and this release, with
        the dead pass's actor and start time); nothing pretends
        the pass finished."""
        if not reason or not reason.strip():
            raise GateError(
                "batch_driver_release_reason_required",
                "releasing a driver mark is a recorded act — it "
                "names its reason",
            )
        batch = self._require_batch(batch_id)
        standing = batch.get("driver")
        if not standing:
            raise GateError(
                "batch_driver_not_active",
                f"no drive pass is recorded active on batch "
                f"{batch['name']!r}; there is no mark to release",
            )
        batch["driver"] = None
        self._save_batch(batch)
        self._audit(
            "batch", batch_id, "batch_driver_released", actor,
            reason=reason,
            released_actor=standing.get("actor"),
            released_started_at=standing.get("started_at"),
        )
        log_event(
            "batch_driver_released", batch_id=batch_id, actor=actor
        )
        return self._batch_report(batch)

    def _batch_report(self, batch: dict) -> dict:
        """One batch, folded on read into its report — the run
        sheet a reader decides from. The stored marks (each
        item's batch-level state, the budget stop, the last
        drive) come from the batch record; everything else —
        order states, runs and their failure classes, challenges
        outstanding, spend, priced cost — is folded at read time
        from the items' own records, so the report can be
        re-derived and argued with like every other fold here.
        The batch's standing reads derived too: cancelled when
        the record says so, complete when every item is either
        batch-marked or its order terminal, open otherwise."""
        items_out: list[dict] = []
        awaiting: list[dict] = []
        by_order_status: dict[str, int] = {}
        by_batch_state: dict[str, int] = {}
        runs_total = 0
        runs_by_status: dict[str, int] = {}
        failures_by_class: dict[str, int] = {}
        successes = 0
        open_must = 0
        open_advisory = 0
        usage_rows: list[dict] = []
        wall = 0.0
        complete = bool(batch["items"])
        for item in batch["items"]:
            wid = item["work_order_id"]
            order = self.store.get_work_order(wid)
            order_status = order["status"] if order else None
            runs = self.store.list_runs(wid)
            rows = self.store.list_usage(work_order_id=wid)
            usage_rows.extend(rows)
            wall += sum(
                float(run.get("elapsed_seconds") or 0) for run in runs
            )
            latest = runs[-1] if runs else None
            started = bool(runs) or bool(rows) or (
                self.store.get_plan(wid) is not None
            )
            stored_state = item["item_state"]
            if stored_state in ("budget_stopped", "cancelled"):
                effective = stored_state
            elif started:
                effective = "started"
            else:
                effective = "pending"
            by_batch_state[effective] = (
                by_batch_state.get(effective, 0) + 1
            )
            if order_status is not None:
                by_order_status[order_status] = (
                    by_order_status.get(order_status, 0) + 1
                )
            for run in runs:
                runs_total += 1
                runs_by_status[run["status"]] = (
                    runs_by_status.get(run["status"], 0) + 1
                )
                if run["status"] == "succeeded":
                    successes += 1
                if run["status"] in ("failed", "interrupted"):
                    failure_class, _detail = self._run_failure_class(
                        run
                    )
                    failures_by_class[failure_class] = (
                        failures_by_class.get(failure_class, 0) + 1
                    )
            for challenge in self.store.list_challenges(wid):
                if challenge["status"] != "open":
                    continue
                if challenge["current_severity"] == "must_answer":
                    open_must += 1
                else:
                    open_advisory += 1
            failure_class = None
            if latest is not None and latest["status"] in (
                "failed",
                "interrupted",
            ):
                failure_class, _detail = self._run_failure_class(
                    latest
                )
            next_act = (
                NEXT_ACT_BY_STATUS.get(order_status)
                if order_status
                else None
            )
            items_out.append(
                {
                    "index": item["index"],
                    "kind": item["kind"],
                    "work_order_id": wid,
                    "batch_state": effective,
                    "order_status": order_status,
                    "latest_run": (
                        {
                            "run_id": latest["run_id"],
                            "status": latest["status"],
                        }
                        if latest
                        else None
                    ),
                    "failure_class": failure_class,
                    "next_act": next_act,
                }
            )
            if (
                effective not in ("budget_stopped", "cancelled")
                and order_status not in TERMINAL_ORDER_STATUSES
            ):
                complete = False
            category = (
                AWAITING_PERSON.get(order_status)
                if order_status
                else None
            )
            if category is not None:
                awaiting.append(
                    {
                        "index": item["index"],
                        "work_order_id": wid,
                        "category": category,
                        "next_act": next_act,
                    }
                )
        spend = {
            "model_calls": len(usage_rows),
            "tokens": sum(
                r["prompt_tokens"] + r["completion_tokens"]
                for r in usage_rows
                if r["unit"] == "tokens"
            ),
            "wall_seconds": wall,
            "budget": batch["budget"],
            "budget_stop": batch.get("budget_stop"),
        }
        fold = fold_usage(usage_rows, self.price_tables)
        status = (
            "cancelled"
            if batch["status"] == "cancelled"
            else "complete"
            if complete
            else "open"
        )
        return {
            "batch": batch,
            "status": status,
            "items": items_out,
            "counts": {
                "items": len(items_out),
                "by_batch_state": by_batch_state,
                "by_order_status": by_order_status,
            },
            "runs": {
                "total": runs_total,
                "by_status": runs_by_status,
                "succeeded": successes,
                "failures_by_class": failures_by_class,
            },
            "challenges_outstanding": {
                "open_must_answer": open_must,
                "open_advisory": open_advisory,
            },
            "spend": spend,
            "cost": fold.as_dict(successes=successes),
            "awaiting_person": awaiting,
            "derivation": (
                "folded on read from the batch record and its "
                "items' own records — work orders, plans, runs, "
                "usage, challenges; the item states "
                "budget_stopped and cancelled are the batch "
                "record's own marks, and spend is the items' "
                "usage rows folded whole (drafting included), "
                "with wall seconds as the sum of the runs' "
                "active seconds"
            ),
        }

    def audit(self, work_order_id: str) -> list[dict]:
        return self.store.list_audit("work_order", work_order_id)

    # -- work orders ------------------------------------------------------
    def create_work_order(
        self,
        body: WorkOrderIn,
        actor: str = "console",
        *,
        template_provenance: dict | None = None,
        batch_provenance: dict | None = None,
    ) -> dict:
        order = WorkOrder(
            work_order_id=_new_id("wo"),
            title=body.title,
            task_text=body.task_text,
            file_scope=list(body.file_scope),
            gold_task_id=body.gold_task_id,
            grading_tests=list(body.grading_tests),
            requested_by=body.requested_by,
            template=template_provenance,
            batch=batch_provenance,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        if body.gold_task_id:
            task = self.gold.get(body.gold_task_id)
            if task is None:
                raise GateError(
                    "unknown_gold_task", f"no gold task named {body.gold_task_id!r}"
                )
            order.target_path = str(self._materialise_target(task))
        else:
            order.target_path = body.target_path
        self.store.put_work_order(order.model_dump())
        self._audit(
            "work_order",
            order.work_order_id,
            "work_order_created",
            actor,
            title=order.title,
            gold_task_id=order.gold_task_id,
            file_scope=order.file_scope,
            template=(
                {
                    "name": template_provenance["name"],
                    "version": template_provenance["version"],
                }
                if template_provenance
                else None
            ),
            batch=(
                {
                    "batch_id": batch_provenance["batch_id"],
                    "name": batch_provenance["name"],
                    "index": batch_provenance["index"],
                }
                if batch_provenance
                else None
            ),
        )
        log_event("work_order_created", work_order_id=order.work_order_id)
        return order.model_dump()

    def create_gold_work_order(
        self,
        gold_task_id: str,
        actor: str = "console",
        *,
        batch_provenance: dict | None = None,
    ) -> dict:
        task = self.gold.get(gold_task_id)
        if task is None:
            raise GateError("unknown_gold_task", f"no gold task named {gold_task_id!r}")
        return self.create_work_order(
            WorkOrderIn(
                title=task.title,
                task_text=task.task_text,
                file_scope=list(task.file_scope),
                gold_task_id=task.id,
                requested_by=actor,
            ),
            actor=actor,
            batch_provenance=batch_provenance,
        )

    def create_work_order_mcp(self, body: WorkOrderIn, actor: str) -> dict:
        """The MCP surface's door onto intake (round 17). The act is
        the ordinary ``create_work_order`` — same validation, same
        records, same audit event with the same actor — plus one
        trail entry naming the channel and the tool, so a reader of
        the order's history can tell where the submission came
        from without the act itself being judged any differently.
        The door's ``actor`` is authoritative: it becomes the
        order's ``requested_by``, exactly as the API's create makes
        the body's requester the actor of record."""
        body = body.model_copy(update={"requested_by": actor})
        order = self.create_work_order(body, actor=actor)
        self._audit(
            "work_order",
            order["work_order_id"],
            "mcp_work_order_created",
            actor,
            channel="mcp",
            tool="create_work_order",
        )
        return order

    def draft_plan_mcp(self, work_order_id: str, actor: str) -> dict:
        """The MCP surface's door onto plan drafting (round 17).
        Drafting is a preparatory act — it proposes; it never
        decides — so a machine client may ask for a draft. The act
        is the ordinary ``draft_plan`` under the caller's name, plus
        the channel's trail entry. The plan the door returns still
        stands undecided: approval is a human act, and no door on
        this service leads to it from the MCP surface."""
        plan = self.draft_plan(work_order_id, actor=actor)
        self._audit(
            "work_order",
            work_order_id,
            "mcp_plan_drafted",
            actor,
            channel="mcp",
            tool="draft_plan",
            version=plan["version"],
        )
        return plan

    def _materialise_target(self, task) -> Path:
        """The task's repo on disk: the workshop, with the broken
        overlay applied. This directory is the 'developer's repo' the
        work order runs against; runs copy it, never touch it."""
        target = self.settings.targets_dir / task.id
        if target.exists():
            shutil.rmtree(target)
        workshop_src = self.settings.gold_dir.parent.parent / "workshop"
        shutil.copytree(workshop_src, target / "workshop", ignore=_copy_ignore)
        for rel, content in task.broken_files().items():
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        return target

    def get_work_order(self, work_order_id: str) -> dict | None:
        return self.store.get_work_order(work_order_id)

    def list_work_orders(self) -> list[dict]:
        return self.store.list_work_orders()

    def work_order_detail(self, work_order_id: str) -> dict | None:
        order = self.store.get_work_order(work_order_id)
        if order is None:
            return None
        runs = [self._attach_usage(r) for r in self.store.list_runs(work_order_id)]
        plans = [
            self._with_fingerprint_state(order, plan)
            for plan in self.store.list_plans(work_order_id)
        ]
        return {
            "work_order": order,
            "plan": plans[-1] if plans else None,
            "plans": plans,
            "runs": runs,
            "usage": self.store.list_usage(work_order_id=work_order_id),
            "audit": self.audit(work_order_id),
        }

    # -- plan gate ---------------------------------------------------------
    def _repo_files(self, order: dict) -> list[str]:
        """The target repo's files, repo-relative — the context plan
        validation resolves test names against."""
        target = Path(order["target_path"])
        if not target.is_dir():
            return []
        return sorted(
            p.relative_to(target).as_posix()
            for p in target.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )

    def _grading_names(self, order: dict) -> list[str]:
        """The task's grading set, by name: the order's named grading
        tests, plus a gold task's grading file at its staged path."""
        names = list(order.get("grading_tests") or [])
        gold = self.gold.get(order["gold_task_id"]) if order.get("gold_task_id") else None
        if gold is not None:
            names.append(gold.grading_relpath())
        return names

    def _validate_draft(self, draft, order: dict) -> None:
        """One validator for every plan draft, scripted or live —
        called with the full context: the approved scope, the repo's
        files, and the grading set test names must resolve against."""
        validate_plan_draft(
            draft,
            file_scope=list(order["file_scope"]),
            repo_files=self._repo_files(order),
            grading_tests=self._grading_names(order),
        )

    def draft_plan(self, work_order_id: str, actor: str = "console") -> dict:
        order = self._require_order(work_order_id)
        if order["status"] not in ("draft", "plan_rejected", "changes_requested"):
            raise GateError(
                "plan_not_draftable",
                f"a plan can only be drafted from "
                f"draft/plan_rejected/changes_requested, "
                f"not {order['status']}",
            )
        repo_files = self._repo_files(order)
        model = self._make_model()
        plan_input = dict(order)
        # Round 11: when a reviewer sent this order back, their note
        # rides into the draft's context under its own key — the
        # planner, scripted or live, is shown what the reviewer
        # asked to change, as part of the order it plans against.
        plan_input["_review_note"] = order.get("review_note")
        # Round 12: when the order was instantiated from a task
        # template, the substituted skeleton rides into the draft's
        # context under its own keys, labelled with the template it
        # came from. It is context, never a plan: the draft still
        # passes the shared validator, and the plan record carries
        # the label so a reader can see the suggestion was there.
        template_prov = order.get("template")
        if template_prov:
            plan_input["_template"] = {
                "name": template_prov["name"],
                "version": template_prov["version"],
            }
            plan_input["_template_steps"] = [
                dict(step) for step in template_prov.get("steps") or []
            ]
        gold = self.gold.get(order["gold_task_id"]) if order.get("gold_task_id") else None
        if gold is not None:
            plan_input["_plan_approach"] = gold.plan_approach
            plan_input["_plan_steps"] = [dict(step) for step in gold.plan_steps]
            plan_input["_grading_relpath"] = gold.grading_relpath()
        try:
            draft = model.draft_plan(work_order=plan_input, repo_files=repo_files)
        finally:
            self._persist_usage(model, work_order_id=work_order_id, run_id=None)
        # One validator for every model's draft, scripted or live.
        self._validate_draft(draft, order)
        previous = self.store.get_plan(work_order_id)
        version = previous["version"] + 1 if previous else 1
        plan = Plan(
            work_order_id=work_order_id,
            version=version,
            approach=draft.approach,
            steps=[PlanStep(**step.as_dict()) for step in draft.steps],
            files=draft.files,
            tests=draft.tests,
            model_profile=self.serving_profile_id(),
            template_context=(
                {
                    "name": template_prov["name"],
                    "version": template_prov["version"],
                }
                if template_prov
                else None
            ),
            created_at=utcnow(),
        )
        self.store.put_plan(plan.model_dump())
        self._set_status(order, "plan_ready")
        self._audit(
            "work_order", work_order_id, "plan_drafted", actor,
            version=version, files=plan.files, model_profile=plan.model_profile,
            template_context=plan.template_context,
        )
        log_event(
            "plan_drafted", work_order_id=work_order_id, version=version,
            actor=actor,
        )
        return plan.model_dump()

    def _scope_fingerprint(self, order: dict, plan: dict) -> tuple[str, dict]:
        """The fingerprint of a plan's approved scope in the target
        repo, and the per-file digests behind it — recomputed the
        same way at approval and at execution, so the two compare."""
        digests = file_digests(Path(order["target_path"]), list(plan["files"]))
        fingerprint = scope_fingerprint(
            task_text=order["task_text"], files=list(plan["files"]), digests=digests
        )
        return fingerprint, digests

    def _with_fingerprint_state(self, order: dict, plan: dict) -> dict:
        """A plan read carries its fingerprint state: ``unrecorded``
        (never approved), ``current`` (the repo still matches the
        world this version was approved against), or ``stale``."""
        stored = plan.get("scope_fingerprint")
        if not stored:
            return {**plan, "fingerprint_state": "unrecorded"}
        current, _digests = self._scope_fingerprint(order, plan)
        state = "current" if current == stored else "stale"
        return {**plan, "fingerprint_state": state}

    def decide_plan(
        self, work_order_id: str, *, approve: bool, actor: str, note: str | None = None
    ) -> dict:
        order = self._require_order(work_order_id)
        plan = self.store.get_plan(work_order_id)
        if plan is None or order["status"] != "plan_ready":
            raise GateError(
                "no_plan_awaiting_decision",
                f"work order {work_order_id} has no plan awaiting a decision "
                f"(status {order['status']})",
            )
        plan["status"] = "approved" if approve else "rejected"
        plan["decided_by"] = actor
        plan["decided_at"] = utcnow()
        plan["decision_note"] = note
        if approve:
            # Approval binds this version to the world as it stands:
            # the fingerprint execution will be checked against.
            fingerprint, digests = self._scope_fingerprint(order, plan)
            plan["scope_fingerprint"] = fingerprint
            plan["scope_digests"] = digests
        self.store.put_plan(plan)
        self._set_status(order, "approved" if approve else "plan_rejected")
        self._audit(
            "work_order", work_order_id,
            "plan_approved" if approve else "plan_rejected", actor,
            version=plan["version"], note=note,
            scope_fingerprint=plan.get("scope_fingerprint"),
        )
        log_event(
            "plan_decided", work_order_id=work_order_id,
            version=plan["version"],
            decision="approved" if approve else "rejected", actor=actor,
        )
        return plan

    def amend_plan(self, work_order_id: str, *, draft, actor: str) -> dict:
        """Amend the approved plan. The amendment is a new version,
        never an edit in place: it arrives as a full replacement
        draft, is diffed against the version it supersedes, and the
        diff is stored on the new version's record.

        Two rules bind it. The scope ceiling: the amendment's files
        must stay inside the *work order's* approved scope — widening
        past it is refused as ``scope_widening_refused`` (narrowing,
        or moving within the scope, is the amendment's business). And
        the approval binding: the new version starts undecided and
        the order returns to plan_ready — the approval recorded
        against the old version does not carry to this one.
        Amending is possible only before execution starts, while the
        order stands approved."""
        order = self._require_order(work_order_id)
        previous = self.store.get_plan(work_order_id)
        if (
            order["status"] != "approved"
            or previous is None
            or previous["status"] != "approved"
        ):
            raise GateError(
                "plan_not_amendable",
                f"only an approved plan can be amended, before execution "
                f"starts (work order {work_order_id} is {order['status']})",
            )
        outside = [f for f in draft.files if f not in set(order["file_scope"])]
        if outside:
            raise GateError(
                "scope_widening_refused",
                f"the amendment names files outside the work order's "
                f"approved scope {order['file_scope']}: {outside}; the work "
                f"order's scope is the ceiling an amendment cannot widen past",
            )
        self._validate_draft(draft, order)
        plan = Plan(
            work_order_id=work_order_id,
            version=previous["version"] + 1,
            approach=draft.approach,
            steps=[PlanStep(**step.as_dict()) for step in draft.steps],
            files=draft.files,
            tests=draft.tests,
            model_profile=previous["model_profile"],
            amended_by=actor,
            created_at=utcnow(),
        )
        payload = plan.model_dump()
        payload["diff_from_previous"] = diff_plans(previous, payload)
        self.store.put_plan(payload)
        self._set_status(order, "plan_ready")
        diff = payload["diff_from_previous"]
        self._audit(
            "work_order", work_order_id, "plan_amended", actor,
            from_version=previous["version"], to_version=plan.version,
            steps_added=len(diff["steps_added"]),
            steps_removed=len(diff["steps_removed"]),
            steps_changed=len(diff["steps_changed"]),
            files_added=diff["files_added"], files_removed=diff["files_removed"],
        )
        log_event(
            "plan_amended", work_order_id=work_order_id,
            from_version=previous["version"], to_version=plan.version,
            actor=actor,
        )
        return payload

    # -- runs ----------------------------------------------------------------
    def _refuse_if_stale(self, order: dict, plan: dict, actor: str) -> None:
        """Execution starts only in the world the plan was approved
        against. The fingerprint recorded at approval is recomputed
        over the target repo; drift in any in-scope file — an edit,
        a deletion — refuses the run as ``stale_plan``, names the
        drifted files on the audit trail, and returns the order to
        draft, from which a fresh plan can be drafted against the
        repo as it now stands. Files outside the approved scope are
        not in the fingerprint and can never stale a plan."""
        stored = plan.get("scope_fingerprint")
        if not stored:
            drift: list[str] = []
            message = (
                f"execution refused: plan v{plan['version']} carries no scope "
                f"fingerprint, so the world it was approved against cannot "
                f"be verified"
            )
        else:
            current, current_digests = self._scope_fingerprint(order, plan)
            if current == stored:
                return
            drift = drifted_files(plan.get("scope_digests") or {}, current_digests)
            message = (
                f"execution refused: the target repo moved inside the "
                f"approved scope since plan v{plan['version']} was approved "
                f"(drifted: {', '.join(drift) or 'scope contents'}); draft a "
                f"fresh plan against the repo as it now stands"
            )
        self._audit(
            "work_order", order["work_order_id"], "stale_plan_refused", actor,
            version=plan["version"], drifted_files=drift,
            expected_fingerprint=stored,
        )
        self._set_status(order, "draft")
        raise GateError("stale_plan", message)

    def start_run(
        self,
        work_order_id: str,
        actor: str = "console",
        plan_version: int | None = None,
    ) -> dict:
        order = self._require_order(work_order_id)
        plans = self.store.list_plans(work_order_id)
        plan = plans[-1] if plans else None
        if plan_version is not None:
            named = next(
                (p for p in plans if p["version"] == plan_version), None
            )
            if named is None:
                raise GateError(
                    "unknown_plan_version",
                    f"work order {work_order_id} has no plan version "
                    f"{plan_version}",
                )
            if plan is not None and plan_version != plan["version"]:
                raise GateError(
                    "plan_superseded",
                    f"execution refused: plan v{plan_version} was superseded "
                    f"by v{plan['version']}; execution runs the latest "
                    f"approved version, and an approval recorded against "
                    f"v{plan_version} does not authorise v{plan['version']}",
                )
        if order["status"] != "approved" or plan is None or plan["status"] != "approved":
            raise GateError(
                "plan_not_approved",
                f"execution refused: work order {work_order_id} has no approved "
                f"plan (status {order['status']})",
            )
        self._refuse_if_stale(order, plan, actor)
        return self._launch(order, plan, actor)

    def _launch(
        self,
        order: dict,
        plan: dict,
        actor: str,
        *,
        resume_run: dict | None = None,
    ) -> dict:
        """Drive one run — fresh, or resumed from its interruption.

        A fresh run copies the target twice (workspace + pristine)
        and starts with an empty transcript. A resumed run reuses
        its own workspace and pristine copy, rebuilds the transcript
        from the stored tool-call records, and hands those records
        to the model's resume hook, so completed steps are not
        re-executed and landed edits are not re-applied.
        """
        work_order_id = order["work_order_id"]
        scripts: dict[str, list[dict]] = {}
        gold = self.gold.get(order["gold_task_id"]) if order.get("gold_task_id") else None
        if gold is not None:
            scripts[gold.id] = gold.scripted_edits
        model = self._make_model(scripts=scripts)

        if resume_run is None:
            run_id = _new_id("run")
            run_dir = self.settings.runs_dir / run_id
            workspace = run_dir / "workspace"
            pristine = run_dir / "pristine"
            target = Path(order["target_path"])
            if not target.is_dir():
                raise GateError(
                    "target_missing", f"target repo {target} does not exist on disk"
                )
            shutil.copytree(target, workspace, ignore=_copy_ignore)
            shutil.copytree(target, pristine, ignore=_copy_ignore)
            run = Run(
                run_id=run_id,
                work_order_id=work_order_id,
                plan_version=plan["version"],
                model_profile=self.serving_profile_id(),
                deployment_profile=self.settings.deployment.name,
                workspace=str(workspace),
                budget=self._budget_snapshot(),
                created_at=utcnow(),
            )
            self.store.put_run(run.model_dump())
            transcript: list[dict] = []
            progress = _RunProgress()
            self._set_status(order, "running")
            # Round 13: this run replaces every earlier run as
            # the order's current attempt — challenges raised on
            # those runs are marked superseded, never deleted.
            self._supersede_challenges(work_order_id, keep_run_id=run_id)
            self._audit(
                "work_order", work_order_id, "run_started", actor,
                run_id=run_id, plan_version=plan["version"],
                model_profile=run.model_profile,
            )
            log_event("run_started", work_order_id=work_order_id, run_id=run_id)
        else:
            run = Run(**resume_run)
            run_id = run.run_id
            workspace = Path(run.workspace)
            pristine = workspace.parent / "pristine"
            if not workspace.is_dir() or not pristine.is_dir():
                raise GateError(
                    "run_workspace_missing",
                    f"run {run_id} cannot resume: its workspace "
                    f"{run.workspace} is no longer on disk; restart instead",
                )
            transcript = self._stored_transcript(work_order_id, run_id)
            checkpoints = self.store.list_checkpoints(run_id)
            last = checkpoints[-1] if checkpoints else None
            progress = _RunProgress(
                tool_calls=len(transcript),
                edited_files=list(last.get("edited_files") or []) if last else [],
                stepped={
                    c["step_index"] for c in checkpoints if c["kind"] == "step"
                },
                next_index=len(checkpoints),
            )
            run.status = "running"
            run.interruption_reason = None
            if not run.budget:
                # A run recorded before budgets existed resumes under
                # a snapshot taken now; a run that has one keeps it —
                # a resume never refreshes a budget.
                run.budget = self._budget_snapshot()
            self.store.put_run(run.model_dump())
            self._set_status(order, "running")
            self._audit(
                "work_order", work_order_id, "run_resumed", actor,
                run_id=run_id, plan_version=run.plan_version,
                from_checkpoint=run.last_checkpoint,
                prior_tool_calls=len(transcript),
            )
            log_event("run_resumed", work_order_id=work_order_id, run_id=run_id)

        tools = SandboxTools(
            workspace,
            scope=plan["files"],
            test_timeout=self.settings.policy.test_timeout_seconds,
            plan_steps=plan.get("steps"),
        )
        tools.edited_files = list(progress.edited_files)
        steps = run.steps_used
        max_steps = self.settings.policy.max_steps
        model_error: GateError | None = None
        crash: Exception | None = None
        budget_stop: _BudgetStop | None = None
        attempt_started = self._monotonic()
        try:
            model.start_run(work_order=order, plan=plan)
            if resume_run is not None and transcript:
                model.prepare_resume(transcript=transcript)
            while steps < max_steps:
                # The budget is checked before every model call: at
                # a ceiling, the call is not made at all.
                stop = self._budget_breach(
                    run, after_call=False, attempt_started=attempt_started
                )
                if stop is not None:
                    raise stop
                step: Step = model.next_step(transcript=transcript)
                self._persist_usage(model, work_order_id=work_order_id, run_id=run_id)
                # And after the call's usage lands: the call itself
                # may have crossed the token ceiling. The run stops
                # here, so the overshoot is that one in-flight call
                # and no more.
                stop = self._budget_breach(
                    run, after_call=True, attempt_started=attempt_started
                )
                if stop is not None:
                    raise stop
                steps += 1
                if step.tool == "finish":
                    transcript.append({"tool": "finish", "ok": True, "output": ""})
                    break
                result = dispatch(tools, step)
                entry = {
                    "tool": step.tool,
                    "args": _summarise_args(step),
                    **result.as_dict(),
                }
                transcript.append(entry)
                progress.tool_calls += 1
                self._audit(
                    "work_order", work_order_id, "tool_call", "agent",
                    run_id=run_id, tool=step.tool, ok=result.ok,
                    refused=result.refused, args=_summarise_args(step),
                    result=result.as_dict(),
                )
                if result.refused and step.tool == "edit_file":
                    self._audit(
                        "work_order", work_order_id, "edit_refused", "shell",
                        run_id=run_id, path=(step.args or {}).get("path"),
                        reason=result.output,
                    )
                if (
                    step.tool == "edit_file"
                    and result.ok
                    and not result.refused
                ):
                    self._after_landed_edit(plan, run, progress, result)
        except _BudgetStop as exc:
            budget_stop = exc
            self._persist_usage(model, work_order_id=work_order_id, run_id=run_id)
        except GateError as exc:
            # A model-side failure: how the run lands depends on the
            # failure's kind, decided below — transient failures
            # interrupt the run, terminal ones fail it. Either way
            # the reason is recorded; never a hang, never a crash
            # that leaves the order 'running'.
            model_error = exc
            self._persist_usage(model, work_order_id=work_order_id, run_id=run_id)
        except Exception as exc:  # a process-level death inside the loop
            crash = exc
            self._persist_usage(model, work_order_id=work_order_id, run_id=run_id)
        run.steps_used = steps
        run.elapsed_seconds = round(
            run.elapsed_seconds + (self._monotonic() - attempt_started), 3
        )

        if model_error is not None and model_error.code not in _TRANSIENT_MODEL_CODES:
            reason = f"model call failed: {model_error.code} — {model_error.message}"
            run.status = "failed"
            run.failure_reason = reason
            run.verification = {
                "passed": False,
                "reason": reason,
                "model_error": model_error.code,
            }
            run.finished_at = utcnow()
            run.cost = self._cost_for_run(run.model_dump())
            self.store.put_run(run.model_dump())
            self._set_status(order, "failed")
            self._audit(
                "work_order", work_order_id, "model_call_failed", "shell",
                run_id=run_id, code=model_error.code, reason=model_error.message,
            )
            self._audit(
                "work_order", work_order_id, "run_failed", "shell",
                run_id=run_id, steps_used=steps,
            )
            log_event(
                "run_finished", work_order_id=work_order_id, run_id=run_id,
                status="failed",
            )
            return self._attach_usage(run.model_dump())

        if budget_stop is not None:
            # The run crossed a budget ceiling. It lands INTERRUPTED
            # — the work is not wrong, the spend is done — with the
            # ceiling named on its record. A budget-stopped run
            # cannot resume (resume would overspend the same budget;
            # see resume_run) — restart, into a fresh budget, is its
            # way back.
            reason = (
                f"budget_exhausted: the run crossed its "
                f"{budget_stop.ceiling} budget (ceiling "
                f"{budget_stop.limit}, spent {budget_stop.used})"
            )
            run.status = "interrupted"
            run.budget_stop = budget_stop.ceiling
            run.interruption_reason = reason
            run.cost = self._cost_for_run(run.model_dump())
            self.store.put_run(run.model_dump())
            self._set_status(order, "interrupted")
            self._audit(
                "work_order", work_order_id, "budget_exhausted", "shell",
                run_id=run_id, ceiling=budget_stop.ceiling,
                limit=budget_stop.limit, used=budget_stop.used,
            )
            self._audit(
                "work_order", work_order_id, "run_interrupted", "shell",
                run_id=run_id, code="budget_exhausted", reason=reason,
                last_checkpoint=run.last_checkpoint, steps_used=steps,
            )
            log_event(
                "run_interrupted", work_order_id=work_order_id,
                run_id=run_id, reason="budget_exhausted",
            )
            return self._attach_usage(run.model_dump())

        if model_error is not None or crash is not None:
            # The run died mid-flight. It lands INTERRUPTED with the
            # last checkpoint it recorded named on its record — the
            # basis a later resume is checked against — and the
            # approval it ran under stands unconsumed.
            if model_error is not None:
                code = model_error.code
                reason = (
                    f"model call failed: {model_error.code} — "
                    f"{model_error.message}"
                )
            else:
                assert crash is not None
                code = "unexpected_error"
                reason = (
                    f"the run died mid-flight: "
                    f"{type(crash).__name__}: {crash}"
                )
            run.status = "interrupted"
            run.interruption_reason = reason
            run.cost = self._cost_for_run(run.model_dump())
            self.store.put_run(run.model_dump())
            self._set_status(order, "interrupted")
            if model_error is not None:
                self._audit(
                    "work_order", work_order_id, "model_call_failed", "shell",
                    run_id=run_id, code=code, reason=model_error.message,
                )
            self._audit(
                "work_order", work_order_id, "run_interrupted", "shell",
                run_id=run_id, code=code, reason=reason,
                last_checkpoint=run.last_checkpoint, steps_used=steps,
            )
            log_event(
                "run_interrupted", work_order_id=work_order_id, run_id=run_id,
                reason=code,
            )
            return self._attach_usage(run.model_dump())

        patch_text, changed = build_patch(pristine, workspace)
        run.patch = patch_text
        run.changed_files = changed
        verification = self._verify(order, plan, pristine, patch_text, changed)
        run.verification = verification
        run.finished_at = utcnow()
        if verification["passed"]:
            run.status = "succeeded"
            self._set_status(order, "succeeded")
        else:
            run.status = "failed"
            run.failure_reason = verification["reason"]
            self._set_status(order, "failed")
        run.cost = self._cost_for_run(run.model_dump())
        self.store.put_run(run.model_dump())
        self._audit(
            "work_order", work_order_id, "verification_completed", "shell",
            run_id=run_id, passed=verification["passed"],
            reason=verification["reason"], changed_files=changed,
            test_surface_files=verification.get("test_surface_files") or [],
            unplanned_files=verification.get("unplanned_files") or [],
            hidden_verdict=verification.get("hidden_verdict"),
        )
        self._audit(
            "work_order", work_order_id,
            "run_succeeded" if run.status == "succeeded" else "run_failed", "shell",
            run_id=run_id, steps_used=steps,
        )
        log_event(
            "run_finished", work_order_id=work_order_id, run_id=run_id,
            status=run.status,
        )
        if run.status == "succeeded":
            # Round 13: the challenge pass takes its independent
            # look at the verified patch now, from the stored
            # records — its challenges stand between this run and
            # the review gate's accept.
            self.challenge_pass(work_order_id, run_id=run_id)
        return self._attach_usage(run.model_dump())

    # -- checkpoints ---------------------------------------------------------
    def _after_landed_edit(
        self,
        plan: dict,
        run: Run,
        progress: _RunProgress,
        result,
    ) -> None:
        """A landed edit is a recovery boundary. Record the edit
        checkpoint, then a step checkpoint for any plan step this
        edit completes — a step is complete when every file it names
        has taken a landed edit."""
        path = (result.detail or {}).get("path")
        if path and path not in progress.edited_files:
            progress.edited_files.append(path)
        plan_steps = plan.get("steps") or []
        step_index = next(
            (
                index
                for index, step in enumerate(plan_steps)
                if path in (step.get("files") or [])
            ),
            0,
        )
        self._record_checkpoint(
            plan, run, progress, kind="edit", step_index=step_index
        )
        edited = set(progress.edited_files)
        for index, step in enumerate(plan_steps):
            files = step.get("files") or []
            if (
                files
                and all(name in edited for name in files)
                and index not in progress.stepped
            ):
                progress.stepped.add(index)
                self._record_checkpoint(
                    plan, run, progress, kind="step", step_index=index
                )

    def _record_checkpoint(
        self,
        plan: dict,
        run: Run,
        progress: _RunProgress,
        *,
        kind: str,
        step_index: int,
    ) -> dict:
        """One checkpoint record: the run, the plan step it reached,
        the workspace state digest (per-file digests of the plan's
        in-scope files), the tool calls so far, and the usage totals
        so far. The workspace itself is not copied — the digest is
        the record, and a resume checks the living workspace against
        the last checkpoint's digests."""
        usage_rows = self.store.list_usage(run_id=run.run_id)
        record = {
            "checkpoint_id": _new_id("cp"),
            "run_id": run.run_id,
            "work_order_id": run.work_order_id,
            "index": progress.next_index,
            "kind": kind,
            "step_index": step_index,
            "digests": file_digests(Path(run.workspace), list(plan["files"])),
            "edited_files": list(progress.edited_files),
            "tool_calls_so_far": progress.tool_calls,
            "usage_totals": {
                "calls": len(usage_rows),
                "prompt_tokens": sum(r["prompt_tokens"] for r in usage_rows),
                "completion_tokens": sum(
                    r["completion_tokens"] for r in usage_rows
                ),
                "latency_ms": sum(r["latency_ms"] for r in usage_rows),
            },
            "created_at": utcnow(),
        }
        progress.next_index += 1
        self.store.append_checkpoint(record)
        run.last_checkpoint = record["checkpoint_id"]
        self.store.put_run(run.model_dump())
        return record

    # -- failure recovery ------------------------------------------------------
    def _stored_transcript(self, work_order_id: str, run_id: str) -> list[dict]:
        """A run's transcript, rebuilt from its stored tool-call
        records — the same entries the live loop carried (tool, the
        summarised args, the tool's structured result), so a resumed
        attempt's context is exactly what the records tell."""
        transcript: list[dict] = []
        for record in self.store.list_audit("work_order", work_order_id):
            if record["event"] != "tool_call":
                continue
            detail = record["detail"]
            if detail.get("run_id") != run_id:
                continue
            transcript.append(
                {
                    "tool": detail.get("tool"),
                    "args": detail.get("args") or {},
                    **(detail.get("result") or {}),
                }
            )
        return transcript

    def _reconcile_orphaned_runs(self) -> None:
        """Runs execute synchronously inside one process, so a run
        still standing at 'running' when a service is constructed
        belonged to a process that died mid-run. Mark it INTERRUPTED
        with the reason ``process_lost`` — never leave a zombie row,
        never silently fail the run. Idempotent: a reconciled run is
        no longer 'running', so a second boot changes nothing, and a
        run a live process is driving is never visible here."""
        for stored in self.store.runs_with_status("running"):
            stored["status"] = "interrupted"
            stored["interruption_reason"] = "process_lost"
            self.store.put_run(stored)
            order = self.store.get_work_order(stored["work_order_id"])
            if order is not None and order["status"] == "running":
                order["status"] = "interrupted"
                order["updated_at"] = utcnow()
                self.store.put_work_order(order)
            self._audit(
                "work_order", stored["work_order_id"], "run_interrupted",
                "shell",
                run_id=stored["run_id"], code="process_lost",
                reason="the process driving this run died before it "
                "finished",
                last_checkpoint=stored.get("last_checkpoint"),
            )
            log_event(
                "run_interrupted", work_order_id=stored["work_order_id"],
                run_id=stored["run_id"], reason="process_lost",
            )

    def _require_run(self, order: dict, run_id: str | None) -> dict:
        if run_id is not None:
            run = self.store.get_run(run_id)
            if run is None or run["work_order_id"] != order["work_order_id"]:
                raise GateError(
                    "unknown_run",
                    f"work order {order['work_order_id']} has no run "
                    f"{run_id!r}",
                )
            return run
        run = self.store.latest_run(order["work_order_id"])
        if run is None:
            raise GateError(
                "unknown_run",
                f"work order {order['work_order_id']} has no runs",
            )
        return run

    def resume_run(
        self,
        work_order_id: str,
        run_id: str | None = None,
        *,
        actor: str = "console",
    ) -> dict:
        """Resume an interrupted run from its last checkpoint. The
        basis is checked in code first (see :meth:`_resume_basis`);
        only then does the run continue — in its own workspace, with
        its context rebuilt from stored records."""
        order = self._require_order(work_order_id)
        run = self._require_run(order, run_id)
        if run["status"] != "interrupted":
            raise GateError(
                "run_not_interrupted",
                f"only an interrupted run can resume; run {run['run_id']} "
                f"is {run['status']}",
            )
        if run.get("budget_stop"):
            # A budget-stopped run's budget is spent; resuming would
            # spend past the ceiling the run was stopped by. Refused
            # by the budget's own name — restart, which snapshots a
            # fresh budget, is the way back.
            self._audit(
                "work_order", work_order_id, "resume_refused", actor,
                run_id=run["run_id"], basis="budget_exhausted",
                reason=run.get("interruption_reason") or "",
            )
            raise GateError(
                "budget_exhausted",
                f"resume refused: run {run['run_id']} was stopped by "
                f"its {run['budget_stop']} budget, and that budget is "
                f"spent — a resume would overspend the ceiling it was "
                f"stopped by; restart the run to spend a fresh budget",
            )
        plan = self._resume_basis(order, run, actor)
        return self._launch(order, plan, actor, resume_run=run)

    def _resume_basis(self, order: dict, run: dict, actor: str) -> dict:
        """Decide whether a resume stands on the basis the run was
        interrupted on. Three checks, in order: the plan version the
        run started on is still the approved version; the target
        repo still fingerprints as the world that plan was approved
        against; and the run's workspace still digests to its last
        checkpoint (or, with no checkpoint yet, to the pristine
        copy). Any basis that moved refuses as
        ``resume_basis_changed``, naming the basis, and is recorded
        — restart is the offered path instead."""
        work_order_id = order["work_order_id"]
        plans = self.store.list_plans(work_order_id)
        plan = plans[-1] if plans else None
        basis: str | None = None
        why = ""
        if plan is None or plan["version"] != run["plan_version"]:
            basis = "plan_superseded"
            why = (
                f"plan v{run['plan_version']}, the version this run "
                f"started on, is no longer the latest version"
            )
        elif plan["status"] != "approved":
            basis = "plan_not_approved"
            why = f"plan v{plan['version']} no longer stands approved"
        else:
            stored = plan.get("scope_fingerprint")
            current, current_digests = self._scope_fingerprint(order, plan)
            if stored and current != stored:
                drift = drifted_files(
                    plan.get("scope_digests") or {}, current_digests
                )
                basis = "target_moved"
                why = (
                    "the target repo moved inside the approved scope "
                    f"since approval (drifted: {', '.join(drift)})"
                )
        if basis is None:
            assert plan is not None
            workspace = Path(run["workspace"]) if run.get("workspace") else None
            if workspace is None or not workspace.is_dir():
                basis = "workspace_missing"
                why = "the run's workspace is no longer on disk"
            else:
                checkpoints = self.store.list_checkpoints(run["run_id"])
                if checkpoints:
                    expected = checkpoints[-1].get("digests") or {}
                else:
                    expected = file_digests(
                        workspace.parent / "pristine", list(plan["files"])
                    )
                current_digests = file_digests(workspace, list(plan["files"]))
                if current_digests != expected:
                    changed = drifted_files(expected, current_digests)
                    basis = "workspace_changed"
                    why = (
                        "the run's workspace no longer matches its "
                        "last checkpoint "
                        f"(changed: {', '.join(changed)})"
                    )
        if basis is not None:
            self._audit(
                "work_order", work_order_id, "resume_refused", actor,
                run_id=run["run_id"], basis=basis, reason=why,
            )
            raise GateError(
                "resume_basis_changed",
                f"resume refused: {why}; the run cannot continue on "
                f"a basis that moved — restart it instead",
            )
        assert plan is not None
        return plan

    def restart_run(
        self,
        work_order_id: str,
        run_id: str | None = None,
        *,
        actor: str = "console",
    ) -> dict:
        """Start over: discard the run's workspace and run fresh
        under the same approved plan. The plan's approval was never
        consumed by a run that did not finish, so the order stands
        approved again and start_run's own checks — the approval,
        the version pin, staleness — all re-apply to the fresh run.
        What the discarded workspace held is summarised on the audit
        trail; an abandoned run's partial patch stays on its record,
        which is the durable copy."""
        order = self._require_order(work_order_id)
        run = self._require_run(order, run_id)
        if run["status"] in ("succeeded", "running"):
            raise GateError(
                "run_not_restartable",
                f"a {run['status']} run cannot be restarted; restart "
                f"is for a run that did not finish (interrupted, "
                f"failed, or abandoned)",
            )
        discarded: list[str] = []
        workspace = Path(run["workspace"]) if run.get("workspace") else None
        if workspace is not None and workspace.is_dir():
            pristine = workspace.parent / "pristine"
            if pristine.is_dir():
                _text, discarded = build_patch(pristine, workspace)
            shutil.rmtree(workspace.parent, ignore_errors=True)
        if run.get("workspace"):
            run["workspace"] = ""
            self.store.put_run(run)
        self._audit(
            "work_order", work_order_id, "run_restarted", actor,
            from_run_id=run["run_id"], plan_version=run["plan_version"],
            discarded_files=discarded,
        )
        self._set_status(order, "approved")
        return self.start_run(
            work_order_id, actor=actor, plan_version=run["plan_version"]
        )

    def abandon_run(
        self,
        work_order_id: str,
        run_id: str | None = None,
        *,
        actor: str,
        reason: str,
    ) -> dict:
        """Give a run up, as a recorded act. Valid from interrupted,
        failed, or (stopping it first) running; a SUCCEEDED run
        refuses by name — it belongs to the review gate. If edits
        had landed, the run's record names the partial patch: the
        diff as far as it got, the files it touches, and the
        checkpoint it reached. A partial patch is never submittable
        to review (see decide_review)."""
        order = self._require_order(work_order_id)
        run = Run(**self._require_run(order, run_id))
        if run.status == "succeeded":
            raise GateError(
                "run_not_abandonable",
                f"run {run.run_id} succeeded; it goes to the review "
                f"gate and cannot be abandoned",
            )
        if run.status == "abandoned":
            raise GateError(
                "run_not_abandonable",
                f"run {run.run_id} is already abandoned",
            )
        if run.status == "running":
            # A run standing at 'running' while an abandon lands is
            # stopped first — as an interruption, on the record —
            # and only then abandoned.
            run.status = "interrupted"
            run.interruption_reason = "stopped by the abandon that followed"
            self.store.put_run(run.model_dump())
            self._audit(
                "work_order", work_order_id, "run_interrupted", "shell",
                run_id=run.run_id, code="stopped_for_abandon",
                reason="the run was stopped so it could be abandoned",
                last_checkpoint=run.last_checkpoint,
            )
        workspace = Path(run.workspace) if run.workspace else None
        if workspace is not None and workspace.is_dir():
            pristine = workspace.parent / "pristine"
            if pristine.is_dir():
                partial_text, changed = build_patch(pristine, workspace)
                if changed:
                    run.partial_patch = partial_text
                    run.partial_files = changed
                    run.partial_checkpoint = run.last_checkpoint
        run.status = "abandoned"
        run.abandoned_by = actor
        run.abandon_reason = reason
        run.finished_at = run.finished_at or utcnow()
        run.cost = self._cost_for_run(run.model_dump())
        self.store.put_run(run.model_dump())
        latest = self.store.latest_run(work_order_id)
        if latest is not None and latest["run_id"] == run.run_id:
            self._set_status(order, "abandoned")
        self._audit(
            "work_order", work_order_id, "run_abandoned", actor,
            run_id=run.run_id, reason=reason,
            partial_files=run.partial_files,
            partial_checkpoint=run.partial_checkpoint,
        )
        log_event(
            "run_abandoned", work_order_id=work_order_id, run_id=run.run_id
        )
        return self._attach_usage(run.model_dump())

    def _verify(
        self, order: dict, plan: dict, pristine: Path, patch_text: str, changed: list[str]
    ) -> dict:
        """The verdict, computed by the shell on a fresh copy.

        Order of checks: the patch must exist and be substantive,
        touch only in-scope files, apply cleanly to a pristine copy,
        and pass the grading tests there — and, for a gold task,
        pass the hidden re-grade staged only then. The agent's
        in-run results play no part.

        Round 10 adds the depth checks, computed before any grading
        runs and stored on every verdict record (see
        :mod:`.verification`): the patch's shape facts, the files
        it touches that are test/grading surface
        (``test_surface_touched``), and the files no step of the
        approved plan named (``unplanned_files``). Policy: for a
        gold task — whose fix never lives in tests, and whose plan
        steps cover exactly the task's scope — a test-surface or
        unplanned patch fails verification by name. For an ad-hoc
        order both flags are recorded and named on the record (and
        the test-surface flag is surfaced in red at the review
        gate), but they do not by themselves fail the run: an
        operator may legitimately scope work that includes tests.
        That the checks sit at verification at all is defence in
        depth — on any plan the validator accepted, the plan's
        steps derive its scope and the tool port already refuses
        edits outside it, so both checks are unreachable through
        the ordinary run path and bite on any record where that
        stops being true.
        """
        gold = self.gold.get(order["gold_task_id"]) if order.get("gold_task_id") else None
        is_gold = gold is not None
        shape = patch_shape(pristine, patch_text, changed)
        surface = test_surface_files(changed)
        unplanned = unplanned_files(plan, changed)
        base: dict = {
            "test_surface_touched": bool(surface),
            "test_surface_files": surface,
            "planned_files": planned_files(plan),
            "unplanned_files": unplanned,
            "patch_shape": shape,
            "visible_verdict": "not_run",
            "hidden_verdict": "not_applicable" if not is_gold else "not_run",
            "hidden_grading": {
                "applicable": is_gold,
                "verdict": "not_applicable" if not is_gold else "not_run",
                "passed": None,
                "reason": (
                    "no hidden grading set exists for an ad-hoc work order"
                    if not is_gold
                    else "the hidden re-grade did not run"
                ),
                "grading_tests": [],
                "grading_evidence": None,
            },
        }

        def verdict(passed: bool, reason: str, **extra) -> dict:
            return {"passed": passed, "reason": reason, **base, **extra}

        scope = set(plan["files"])
        out_of_scope = [f for f in changed if f not in scope]
        if out_of_scope:
            return verdict(
                False,
                f"patch touches files outside the approved scope: "
                f"{out_of_scope}",
                out_of_scope=out_of_scope,
            )
        if is_gold and surface:
            return verdict(
                False,
                f"test_surface_touched: the patch touches test/grading "
                f"surface files {surface} — a gold task's fix never "
                f"lives in its tests, so a patch that edits the "
                f"surface it is graded by fails verification",
            )
        if is_gold and unplanned:
            return verdict(
                False,
                f"unplanned_files: the patch touches files no step of "
                f"the approved plan named: {unplanned} — the plan's "
                f"steps cover exactly this task's scope, so work "
                f"outside them is scope creep, not a fix",
            )
        if not changed:
            return verdict(False, "the run produced no patch")
        if shape["whitespace_only"]:
            return verdict(
                False,
                f"no_substantive_change: the patch changes only "
                f"whitespace in {changed} — a patch exists, but no "
                f"behaviour in it changed, so there is nothing to grade",
            )
        verify_dir = pristine.parent / "verify"
        if verify_dir.exists():
            shutil.rmtree(verify_dir)
        shutil.copytree(pristine, verify_dir, ignore=_copy_ignore)
        try:
            apply_patch(verify_dir, patch_text)
        except PatchError as exc:
            return verdict(False, f"patch does not apply cleanly: {exc}")
        grading_paths = self._stage_grading(order, verify_dir)
        if not grading_paths:
            return verdict(
                False,
                "no grading tests named for this work order",
            )
        neutralised = self._neutralise_startup_files(verify_dir)
        result = self._run_grading(verify_dir, grading_paths)
        evidence = (result.detail or {}).get("grading")
        base["visible_verdict"] = "pass" if result.ok else "fail"
        visible = {
            "grading_tests": grading_paths,
            "grading_output": result.output[-2000:],
            "grading_evidence": evidence,
            "neutralised_startup_files": neutralised,
        }
        if not result.ok:
            if result.code == "grading_not_fully_run":
                reason = (
                    "grading tests did not all run and pass on the patched "
                    f"pristine copy (evidence: {evidence})"
                )
            else:
                reason = "grading tests fail on the patched pristine copy"
            return verdict(False, reason, **visible)
        if not is_gold:
            return verdict(
                True,
                "grading tests pass on the patched pristine copy",
                **visible,
            )
        # Hidden re-grade (round 10): the visible set passed, so the
        # same patched pristine copy is graded again — only now is
        # the task's hidden grading file staged, into this copy,
        # never into any workspace and never alongside the visible
        # staging. A hidden failure flips the verdict to FAILED:
        # the visible tests passing is no longer the whole claim.
        assert gold is not None
        hidden_paths = self._stage_hidden_grading(order, verify_dir)
        if not hidden_paths:
            base["hidden_verdict"] = "fail"
            base["hidden_grading"] = {
                "applicable": True,
                "verdict": "fail",
                "passed": False,
                "reason": "the gold task names no hidden grading set",
                "grading_tests": [],
                "grading_evidence": None,
            }
            return verdict(
                False,
                "hidden_grading_failed: this gold task names no hidden "
                "grading set, so its visible pass cannot be re-graded",
                **visible,
            )
        hidden_neutralised = self._neutralise_startup_files(verify_dir)
        hidden_result = self._run_grading(verify_dir, hidden_paths)
        hidden_evidence = (hidden_result.detail or {}).get("grading")
        base["hidden_verdict"] = "pass" if hidden_result.ok else "fail"
        base["hidden_grading"] = {
            "applicable": True,
            "verdict": base["hidden_verdict"],
            "passed": bool(hidden_result.ok),
            "reason": (
                "hidden grading tests pass on the patched pristine copy"
                if hidden_result.ok
                else "hidden grading tests fail on the patched pristine copy"
            ),
            "grading_tests": hidden_paths,
            "grading_output": hidden_result.output[-2000:],
            "grading_evidence": hidden_evidence,
            "neutralised_startup_files": hidden_neutralised,
        }
        if hidden_result.ok:
            return verdict(
                True,
                "grading tests pass on the patched pristine copy",
                **visible,
            )
        return verdict(
            False,
            f"hidden_grading_failed: the patch passes the visible "
            f"grading set but fails the hidden re-grade "
            f"({hidden_paths}; evidence: {hidden_evidence})",
            **visible,
        )

    def _run_grading(self, verify_dir: Path, grading_paths: list[str]):
        """One isolated grading run (round 9's isolation) over the
        named tests in the verification copy, returning the tool
        result whose detail carries the JUnit evidence."""
        tools = SandboxTools(
            verify_dir, scope=[], test_timeout=self.settings.policy.test_timeout_seconds
        )
        return tools.run_tests(grading_paths, grading=True)

    @staticmethod
    def _neutralise_startup_files(verify_dir: Path) -> list[str]:
        """Set aside interpreter startup files in the verification
        copy. The grading child runs with the copy on PYTHONPATH, so
        a ``sitecustomize.py`` / ``usercustomize.py`` the target repo
        carries at its root would be imported by the interpreter
        before pytest starts and could patch the code under test —
        the code-execution cousin of a hostile conftest. The verify
        copy is the shell's throwaway, so the files are renamed
        aside there (the run's workspace and the source repo keep
        theirs), and the names are recorded on the verdict."""
        neutralised: list[str] = []
        for name in ("sitecustomize.py", "usercustomize.py"):
            candidate = verify_dir / name
            if candidate.is_file() and not candidate.is_symlink():
                candidate.rename(verify_dir / f"{name}.shell-disabled")
                neutralised.append(name)
        return neutralised

    def _stage_grading(self, order: dict, verify_dir: Path) -> list[str]:
        """Place the grading tests into the verification copy only.

        Gold tasks: the suite's grading file, staged under grading/.
        Ad-hoc orders: the repo-relative test paths they named (they
        are already in the tree; nothing is staged).
        """
        gold = self.gold.get(order["gold_task_id"]) if order.get("gold_task_id") else None
        if gold is not None:
            dest = verify_dir / gold.grading_relpath()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(gold.grading_source())
            return [gold.grading_relpath()]
        return list(order.get("grading_tests") or [])

    def _stage_hidden_grading(self, order: dict, verify_dir: Path) -> list[str]:
        """Place the hidden grading tests into the verification
        copy — only at re-grade time, after the visible set has
        passed on this same patched copy. The hidden file is never
        staged into a run workspace, never staged alongside the
        visible set, and never present in the target repo: it
        exists in the eval area and, transiently, here. Ad-hoc
        orders have no hidden set and return an empty list."""
        gold = self.gold.get(order["gold_task_id"]) if order.get("gold_task_id") else None
        if gold is None or not gold.hidden_grading_file:
            return []
        dest = verify_dir / gold.hidden_grading_relpath()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(gold.hidden_grading_source())
        return [gold.hidden_grading_relpath()]

    # -- review gate -----------------------------------------------------------
    def decide_review(
        self,
        work_order_id: str,
        *,
        accept: bool | None = None,
        actor: str,
        reason: str | None = None,
        decision: str | None = None,
    ) -> dict:
        """Decide a review, as a recorded act. Three decisions stand
        on a verified run: **accept** exports the patch and closes
        the order; **reject** closes it the other way, with the
        reason recorded; **request changes** (round 11) sends the
        order back — the reviewer's note is stored on the order and
        the order returns to ``changes_requested``, from which a new
        plan version is drafted with the note in its context.

        Accept and reject stand only on an order standing
        ``succeeded`` — the decision that has not been taken yet.
        Request changes stands on the run itself: it is available
        wherever a SUCCEEDED run stands unaccepted — on a
        ``succeeded`` order, and on a ``rejected`` one, where it is
        the recorded way a reviewer reverses a rejection into a
        send-back — and it refuses by name where no succeeded run
        exists to send back. A request without a note refuses: the
        note is the decision's content, and a silent send-back would
        leave the next plan drafting against nothing."""
        order = self._require_order(work_order_id)
        run = self.store.latest_run(work_order_id)
        if decision is None:
            decision = "accept" if accept else "reject"
        if decision not in ("accept", "reject", "request_changes"):
            raise GateError(
                "unknown_review_decision",
                f"a review decision is accept, reject, or "
                f"request_changes, not {decision!r}",
            )
        if run is not None and run["status"] == "abandoned":
            # A partial patch is evidence for the humans, never a
            # submittable patch: the review gate takes SUCCEEDED
            # runs only, and an abandoned run is refused by name.
            raise GateError(
                "partial_patch_not_reviewable",
                f"run {run['run_id']} was abandoned; its partial patch "
                f"cannot be reviewed or accepted — only a SUCCEEDED "
                f"run reaches the review gate",
            )
        if decision == "request_changes":
            if (
                run is None
                or run["status"] != "succeeded"
                or order["status"] not in ("succeeded", "rejected")
            ):
                raise GateError(
                    "no_succeeded_run_to_return",
                    f"work order {work_order_id} has no succeeded run "
                    f"to send back with changes requested (status "
                    f"{order['status']})",
                )
            note = (reason or "").strip()
            if not note:
                raise GateError(
                    "changes_note_required",
                    "requesting changes requires the reviewer's note: "
                    "the note is stored on the order and shown to the "
                    "next plan draft — a send-back without it decides "
                    "nothing the planner can act on",
                )
            self._apply_request_changes(order, run, actor=actor, note=note)
            return self.store.get_work_order(work_order_id)
        if order["status"] != "succeeded" or run is None:
            raise GateError(
                "no_verified_run_awaiting_review",
                f"work order {work_order_id} has no verified run awaiting "
                f"review (status {order['status']})",
            )
        if decision == "accept":
            # Round 13: the challenge pass stands between a
            # verified run and acceptance. An unanswered
            # must-answer challenge refuses the accept by name —
            # the reviewer answers it (rebut, downgrade, or
            # accept-and-revise) or the patch does not land.
            open_must = self._open_must_answer(work_order_id, run["run_id"])
            if open_must:
                rules = ", ".join(
                    sorted({c["rule"] for c in open_must})
                )
                raise GateError(
                    "unanswered_challenges",
                    f"review acceptance refused: run {run['run_id']} "
                    f"carries {len(open_must)} unanswered must-answer "
                    f"challenge(s) ({rules}) — answer each one "
                    f"(rebut it with its counter-evidence, downgrade "
                    f"it with a reason, or accept and revise) before "
                    f"the patch can be accepted",
                )
            artifact = self.settings.artifacts_dir / f"{work_order_id}.patch"
            artifact.write_text(run["patch"])
            self._set_status(order, "accepted")
            self._audit(
                "work_order", work_order_id, "review_accepted", actor,
                run_id=run["run_id"], artifact=str(artifact),
            )
            log_event("review_accepted", work_order_id=work_order_id)
        else:
            self._set_status(order, "rejected")
            self._audit(
                "work_order", work_order_id, "review_rejected", actor,
                run_id=run["run_id"], reason=reason,
            )
            log_event("review_rejected", work_order_id=work_order_id)
        return self.store.get_work_order(work_order_id)

    def _apply_request_changes(
        self,
        order: dict,
        run: dict,
        *,
        actor: str,
        note: str,
        challenge_id: str | None = None,
    ) -> None:
        """The send-back mechanics, shared by the review gate's
        request-changes decision and a challenge's
        accept-and-revise disposition: the note is stored on the
        order for the next plan draft, the order returns to
        ``changes_requested``, and the act is audited — with the
        challenge cited when a disposition drove it."""
        order["review_note"] = note
        self._set_status(order, "changes_requested")
        detail: dict = {}
        if challenge_id is not None:
            detail["challenge_id"] = challenge_id
        self._audit(
            "work_order", order["work_order_id"], "review_changes_requested",
            actor, run_id=run["run_id"], note=note, **detail,
        )
        log_event(
            "review_changes_requested", work_order_id=order["work_order_id"]
        )

    # -- the challenge pass (round 13) -----------------------------------------
    #
    # A second, independent look at a verified patch, computed by
    # challenges.compute_challenges from the run's stored records
    # alone — the patch, the approved plan, the task text, and the
    # run's retained pristine snapshot. The pass runs when a run
    # lands SUCCEEDED; its challenges stand between that run and
    # the review gate's accept. A challenge is answered by a
    # recorded disposition, never deleted; when a newer run
    # replaces the run it was raised on, it is marked superseded,
    # disposition and all.

    def challenge_pass(
        self, work_order_id: str, run_id: str | None = None
    ) -> list[dict]:
        """Run (or re-run) the challenge pass over a SUCCEEDED
        run's stored records, storing any challenges not already
        on record. Idempotent: candidates dedupe on the run and
        their evidence key, so a re-run over the same records
        stores nothing new — a fresh pass belongs to a fresh run
        (a new patch), never to a second look at the same one.
        Returns the run's challenges as they now stand."""
        order = self._require_order(work_order_id)
        if run_id is not None:
            run = self._require_run(order, run_id)
        else:
            run = self.store.latest_run(work_order_id)
            if run is None:
                raise GateError(
                    "unknown_run",
                    f"work order {work_order_id} has no run to challenge",
                )
        if run["status"] != "succeeded":
            raise GateError(
                "run_not_succeeded",
                f"the challenge pass judges verified patches: run "
                f"{run['run_id']} stands {run['status']}, not succeeded",
            )
        plan = (
            self.store.get_plan(work_order_id, run["plan_version"]) or {}
        )
        pristine = Path(run["workspace"]).parent / "pristine"
        prior_texts: dict[str, str | None] = {}
        for rel in run.get("changed_files") or []:
            base = pristine / rel
            prior_texts[rel] = (
                base.read_text() if base.is_file() else None
            )
        candidates = compute_challenges(
            task_text=order["task_text"],
            plan=plan,
            patch_text=run.get("patch") or "",
            changed_files=list(run.get("changed_files") or []),
            prior_texts=prior_texts,
        )
        existing = {
            c["evidence_key"]: c
            for c in self.store.list_challenges(run_id=run["run_id"])
        }
        raised = 0
        for candidate in candidates:
            if candidate["evidence_key"] in existing:
                continue
            record = {
                "challenge_id": _new_id("challenge"),
                "run_id": run["run_id"],
                "work_order_id": work_order_id,
                "rule": candidate["rule"],
                "severity": candidate["severity"],
                "current_severity": candidate["severity"],
                "status": "open",
                "evidence": candidate["evidence"],
                "evidence_key": candidate["evidence_key"],
                "disposition": None,
                "created_at": utcnow(),
            }
            self.store.put_challenge(record)
            existing[record["evidence_key"]] = record
            raised += 1
            self._audit(
                "work_order", work_order_id, "challenge_raised", "shell",
                challenge_id=record["challenge_id"],
                run_id=run["run_id"], rule=record["rule"],
                severity=record["severity"],
            )
        challenges = self.store.list_challenges(run_id=run["run_id"])
        self._audit(
            "work_order", work_order_id, "challenge_pass_completed",
            "shell", run_id=run["run_id"], raised=raised,
            open_must_answer=len(
                self._open_must_answer(work_order_id, run["run_id"])
            ),
            total=len(challenges),
        )
        log_event(
            "challenge_pass_completed",
            work_order_id=work_order_id, run_id=run["run_id"],
            raised=raised,
        )
        return challenges

    def list_challenges(
        self, work_order_id: str, run_id: str | None = None
    ) -> list[dict]:
        """An order's challenges — one run's, or every run's —
        in the order they were raised."""
        self._require_order(work_order_id)
        return self.store.list_challenges(
            work_order_id=work_order_id, run_id=run_id
        )

    def _open_must_answer(
        self, work_order_id: str, run_id: str
    ) -> list[dict]:
        """The challenges standing between a run and acceptance:
        open, and must-answer at their current severity."""
        return [
            c
            for c in self.store.list_challenges(
                work_order_id=work_order_id, run_id=run_id
            )
            if c["status"] == "open"
            and c["current_severity"] == "must_answer"
        ]

    def _supersede_challenges(
        self, work_order_id: str, *, keep_run_id: str
    ) -> None:
        """A newer run replaces the runs before it as the order's
        current attempt: their challenges are marked superseded —
        never deleted, dispositions intact — and stop standing
        between anything and acceptance."""
        for challenge in self.store.list_challenges(
            work_order_id=work_order_id
        ):
            if challenge["run_id"] == keep_run_id:
                continue
            if challenge["status"] == "superseded":
                continue
            challenge["status"] = "superseded"
            self.store.put_challenge(challenge)
            self._audit(
                "work_order", work_order_id, "challenge_superseded",
                "shell", challenge_id=challenge["challenge_id"],
                run_id=challenge["run_id"], superseded_by=keep_run_id,
            )

    def answer_challenge(
        self,
        work_order_id: str,
        challenge_id: str,
        *,
        kind: str,
        actor: str,
        text: str | None = None,
        cites: str | None = None,
        reason: str | None = None,
    ) -> dict:
        """Answer one challenge, as a recorded act. Three
        dispositions stand:

        - ``rebut`` — the challenge is wrong, and the rebuttal
          says why *and names the record it stands on* (the plan
          step, the verification record, the task text): a
          rebuttal without its counter-evidence cited is an
          opinion, and refuses by name.
        - ``downgrade`` — the challenge stands but does not
          block: a must-answer challenge becomes advisory, with
          the reason recorded. The original severity stays on
          the record; only the current severity moves.
        - ``accept_and_revise`` — the challenge is right: the
          order returns through the review gate's request-changes
          path, the challenge cited in the stored note, and a
          fresh plan drafts against it.

        An answered or superseded challenge takes no second
        answer. The disposition is stored on the challenge and
        audited; the challenge itself is never deleted."""
        order = self._require_order(work_order_id)
        challenge = self.store.get_challenge(challenge_id)
        if challenge is None or challenge["work_order_id"] != work_order_id:
            raise GateError(
                "unknown_challenge",
                f"work order {work_order_id} has no challenge "
                f"{challenge_id!r}",
            )
        if challenge["status"] == "superseded":
            raise GateError(
                "challenge_superseded",
                f"challenge {challenge_id} was raised on run "
                f"{challenge['run_id']}, which a newer run has "
                f"superseded; it takes no answer",
            )
        if challenge["status"] == "answered":
            raise GateError(
                "challenge_already_answered",
                f"challenge {challenge_id} already carries its "
                f"disposition ({(challenge.get('disposition') or {}).get('kind')}); "
                f"a challenge is answered once",
            )
        if kind not in ("accept_and_revise", "rebut", "downgrade"):
            raise GateError(
                "unknown_challenge_disposition",
                f"a challenge is answered by accept_and_revise, "
                f"rebut, or downgrade, not {kind!r}",
            )
        disposition: dict = {
            "kind": kind,
            "actor": actor,
            "at": utcnow(),
            "text": (text or "").strip() or None,
            "cites": (cites or "").strip() or None,
            "reason": (reason or "").strip() or None,
        }
        if kind == "rebut":
            if not disposition["text"] or not disposition["cites"]:
                raise GateError(
                    "rebuttal_needs_evidence",
                    "a rebuttal must state its case and name the "
                    "record it stands on as counter-evidence — a "
                    "rebuttal that cites nothing is an opinion, "
                    "and the challenge stands",
                )
        elif kind == "downgrade":
            if challenge["current_severity"] != "must_answer":
                raise GateError(
                    "challenge_not_downgradable",
                    f"challenge {challenge_id} stands "
                    f"{challenge['current_severity']}; only a "
                    f"must-answer challenge can be downgraded",
                )
            if not disposition["reason"]:
                raise GateError(
                    "downgrade_reason_required",
                    "downgrading a must-answer challenge requires "
                    "the reason: the severity was the rule's "
                    "judgement, and moving it is a person's — the "
                    "record must say why",
                )
        elif kind == "accept_and_revise":
            run = self.store.get_run(challenge["run_id"])
            latest = self.store.latest_run(work_order_id)
            if (
                run is None
                or latest is None
                or latest["run_id"] != challenge["run_id"]
                or run["status"] != "succeeded"
                or order["status"] not in ("succeeded", "rejected")
            ):
                raise GateError(
                    "no_succeeded_run_to_return",
                    f"work order {work_order_id} has no succeeded "
                    f"run standing to send back on challenge "
                    f"{challenge_id} (status {order['status']})",
                )
            evidence = challenge["evidence"]
            citation = (
                f"challenge {challenge_id} ({challenge['rule']}, "
                f"{evidence.get('file')}: "
                f"{evidence.get('symbol') or evidence.get('value')})"
            )
            note = (
                f"{disposition['text']} [{citation}]"
                if disposition["text"]
                else citation
            )
            disposition["note"] = note
            self._apply_request_changes(
                order, run, actor=actor, note=note,
                challenge_id=challenge_id,
            )
        if kind == "downgrade":
            challenge["current_severity"] = "advisory"
        challenge["status"] = "answered"
        challenge["disposition"] = disposition
        self.store.put_challenge(challenge)
        self._audit(
            "work_order", work_order_id, "challenge_answered", actor,
            challenge_id=challenge_id, run_id=challenge["run_id"],
            kind=kind,
        )
        log_event(
            "challenge_answered",
            work_order_id=work_order_id, challenge_id=challenge_id,
            kind=kind,
        )
        return self.store.get_challenge(challenge_id)

    # -- the review workbench (round 11) --------------------------------------
    #
    # Everything in this section is a fold over stored records,
    # computed on read: the queue, the packet, and reviewer history
    # write nothing and cache nothing. A decision is an audit record
    # (review_accepted / review_rejected / review_changes_requested,
    # with the run it decided on in its detail); the folds below are
    # the only readers that give those records their workbench shape.

    _REVIEW_EVENTS = {
        "review_accepted": "accepted",
        "review_rejected": "rejected",
        "review_changes_requested": "changes_requested",
    }

    def _review_decisions(self, work_order_id: str) -> list[dict]:
        """The order's review decisions, folded from its audit
        records in the order they were taken."""
        decisions = []
        for row in self.store.list_audit("work_order", work_order_id):
            kind = self._REVIEW_EVENTS.get(row["event"])
            if kind is None:
                continue
            detail = row.get("detail") or {}
            decisions.append(
                {
                    "kind": kind,
                    "actor": row["actor"],
                    "at": row["created_at"],
                    "run_id": detail.get("run_id"),
                    "reason": detail.get("reason"),
                    "note": detail.get("note"),
                }
            )
        return decisions

    @staticmethod
    def _run_failure_class(run: dict) -> tuple[str, str]:
        """A failed or interrupted run's class and its detail, read
        off the run record: the same facts the benchmark folds into
        its failure classes, named for the queue."""
        verification = run.get("verification") or {}
        reason = verification.get("reason") or ""
        if run["status"] == "interrupted":
            detail = run.get("interruption_reason") or ""
            if run.get("budget_stop"):
                return "budget_exhausted", detail
            return "run_interrupted", detail
        if verification.get("out_of_scope"):
            return "out_of_scope_edit", reason
        if "hidden_grading_failed" in reason or (
            verification.get("visible_verdict") == "pass"
            and verification.get("hidden_verdict") == "fail"
        ):
            return "hidden_grading_failed", reason
        if verification.get("test_surface_touched"):
            return "test_surface_touched", reason
        if verification.get("unplanned_files"):
            return "unplanned_files", reason
        if (verification.get("patch_shape") or {}).get("whitespace_only"):
            return "no_substantive_change", reason
        if verification:
            return "verification_failed", reason
        return "run_failed", run.get("failure_reason") or ""

    def review_queue(self, *, now: str | None = None) -> dict:
        """The reviewers' queue, derived on read from the stored
        records: every order standing where a person must act —
        a plan awaiting its decision, a verified run awaiting
        review, a failed run, an interrupted run awaiting its
        recovery decision. Each entry names the order, its state,
        the single next act that moves it, the acts open beside it,
        and its age — the time since the order entered its current
        state (its record's last update), which is the moment the
        act became available. Longest-waiting first. There is no
        queue table behind this and no bulk decision anywhere: a
        decision is one act, on one order, by one named actor."""
        moment = _parse_ts(now) if now else datetime.now(timezone.utc)
        entries: list[dict] = []
        for order in self.store.list_work_orders():
            status = order["status"]
            wid = order["work_order_id"]
            run = self.store.latest_run(wid)
            entry: dict | None = None
            if status == "plan_ready":
                plan = self.store.get_plan(wid)
                entry = {
                    "category": "plan_awaiting_approval",
                    "next_act": "decide_plan",
                    "available_acts": ["decide_plan"],
                    "plan_version": plan["version"] if plan else None,
                }
            elif status == "succeeded" and run is not None:
                verification = run.get("verification") or {}
                entry = {
                    "category": "awaiting_review",
                    "next_act": "review",
                    "available_acts": ["review"],
                    "run_id": run["run_id"],
                    "plan_version": run["plan_version"],
                    # Round 13: the count standing between this
                    # entry and an acceptance — the challenge
                    # pass's unanswered must-answer challenges.
                    "unanswered_must_answer": len(
                        self._open_must_answer(wid, run["run_id"])
                    ),
                    "changed_files": list(run.get("changed_files") or []),
                    "risk_flags": {
                        "test_surface_touched": bool(
                            verification.get("test_surface_touched")
                        ),
                        "unplanned_files": list(
                            verification.get("unplanned_files") or []
                        ),
                        "hidden_verdict": verification.get("hidden_verdict"),
                    },
                }
            elif status == "failed" and run is not None:
                failure_class, detail = self._run_failure_class(run)
                entry = {
                    "category": "run_failed",
                    "next_act": "restart_run",
                    "available_acts": ["restart_run", "abandon_run"],
                    "run_id": run["run_id"],
                    "failure_class": failure_class,
                    "failure_detail": detail,
                }
            elif status == "interrupted" and run is not None:
                failure_class, detail = self._run_failure_class(run)
                entry = {
                    "category": "run_interrupted",
                    "next_act": "resume_run",
                    "available_acts": [
                        "resume_run",
                        "restart_run",
                        "abandon_run",
                    ],
                    "run_id": run["run_id"],
                    "failure_class": failure_class,
                    "failure_detail": detail,
                    "last_checkpoint": run.get("last_checkpoint"),
                }
            if entry is None:
                continue
            updated = _parse_ts(order.get("updated_at"))
            entry.update(
                {
                    "work_order_id": wid,
                    "title": order["title"],
                    "status": status,
                    # Round 14: the batch this order arrived in,
                    # when it arrived in one — the reviewer sees
                    # which submission a waiting item belongs to.
                    "batch": order.get("batch"),
                    "since": order.get("updated_at"),
                    "age_seconds": (
                        max(0, int((moment - updated).total_seconds()))
                        if updated is not None
                        else None
                    ),
                }
            )
            entries.append(entry)
        entries.sort(key=lambda e: (e["since"] or "", e["work_order_id"]))
        return {"entries": entries, "count": len(entries)}

    def review_packet(self, work_order_id: str) -> dict | None:
        """Everything a reviewer decides from, assembled for one
        order: the task and its scope, and the template provenance
        when the order was instantiated from the library, and the
        batch provenance when it arrived as a batch item; the
        approved plan the latest
        run ran on, with its fingerprint state; the patch and its
        shape; the verification record, with the risk flags lifted
        out and named distinctly — test surface touched, files no
        plan step named, and the hidden verdict (``absent`` for an
        ad-hoc order, which has no hidden set: the flag says so
        rather than letting a missing verdict read as a pass);
        the run's usage and priced cost; the order's run history;
        and its prior review decisions. Since round 13 the packet
        also carries the challenge pass's records on the run it
        stands on — each challenge's rule, severity, evidence, and
        disposition state, with the counts folded beside them.
        Once a decision stands on
        an earlier run, the packet also names what changed since
        that packet: the plan version, the patch's files (added
        and removed), and the verdicts, then and now."""
        order = self.store.get_work_order(work_order_id)
        if order is None:
            return None
        runs = [self._attach_usage(r) for r in self.store.list_runs(work_order_id)]
        run = runs[-1] if runs else None
        decisions = self._review_decisions(work_order_id)
        plan = None
        if run is not None:
            stored = self.store.get_plan(work_order_id, run["plan_version"])
            if stored is not None:
                plan = self._with_fingerprint_state(order, stored)
        if plan is None:
            latest = self.store.get_plan(work_order_id)
            if latest is not None:
                plan = self._with_fingerprint_state(order, latest)
        verification = (run or {}).get("verification") or {}
        hidden = verification.get("hidden_verdict")
        # Round 13: the challenge pass's records on the run this
        # packet stands on — the challenges a reviewer must answer
        # before acceptance, with their dispositions — and the
        # counts folded beside them.
        packet_challenges = (
            self.store.list_challenges(
                work_order_id, run_id=run["run_id"]
            )
            if run is not None
            else []
        )
        packet: dict = {
            "work_order": order,
            "task": {
                "title": order["title"],
                "task_text": order["task_text"],
                "file_scope": list(order["file_scope"]),
                "gold_task_id": order.get("gold_task_id"),
                "grading_tests": list(order.get("grading_tests") or []),
            },
            # Round 12: the template provenance, when the order was
            # instantiated from the library — the reviewer sees the
            # shape this work came from, with the values used.
            "template": order.get("template"),
            # Round 14: the batch provenance, when the order was
            # created as an item of a batch — the reviewer sees
            # the submission this work arrived in.
            "batch": order.get("batch"),
            "plan": plan,
            "run": run,
            "patch": (run or {}).get("patch") or "",
            "patch_shape": verification.get("patch_shape"),
            "verification": verification,
            "risk_flags": {
                "test_surface_touched": bool(
                    verification.get("test_surface_touched")
                ),
                "test_surface_files": list(
                    verification.get("test_surface_files") or []
                ),
                "unplanned_files": list(
                    verification.get("unplanned_files") or []
                ),
                "visible_verdict": verification.get("visible_verdict"),
                "hidden_verdict": hidden,
                "hidden_verdict_absent": hidden in (None, "not_applicable"),
            },
            "usage": (run or {}).get("usage") or [],
            "usage_totals": (run or {}).get("usage_totals"),
            "cost": (run or {}).get("cost") or {},
            "run_history": [
                {
                    "run_id": r["run_id"],
                    "status": r["status"],
                    "plan_version": r["plan_version"],
                    "model_profile": r["model_profile"],
                    "changed_files": list(r.get("changed_files") or []),
                    "failure_reason": r.get("failure_reason"),
                    "interruption_reason": r.get("interruption_reason"),
                    "budget_stop": r.get("budget_stop"),
                    "created_at": r.get("created_at"),
                    "finished_at": r.get("finished_at"),
                }
                for r in runs
            ],
            "review_decisions": decisions,
            "challenges": packet_challenges,
            "challenge_summary": self._challenge_summary(
                packet_challenges
            ),
            "changes_since_previous": self._packet_changes(
                runs, run, decisions
            ),
        }
        return packet

    @staticmethod
    def _challenge_summary(challenges: list[dict]) -> dict:
        """A run's challenges counted by where they stand: open
        at each severity, answered, superseded. Folded on read
        from the challenge records, like every workbench number."""
        return {
            "total": len(challenges),
            "open_must_answer": sum(
                1
                for c in challenges
                if c["status"] == "open"
                and c["current_severity"] == "must_answer"
            ),
            "open_advisory": sum(
                1
                for c in challenges
                if c["status"] == "open"
                and c["current_severity"] == "advisory"
            ),
            "answered": sum(
                1 for c in challenges if c["status"] == "answered"
            ),
            "superseded": sum(
                1 for c in challenges if c["status"] == "superseded"
            ),
        }

    @staticmethod
    def _packet_changes(
        runs: list[dict], run: dict | None, decisions: list[dict]
    ) -> dict | None:
        """What changed between the packet a reviewer last decided
        on and this one — computed only when a decision stands on
        an earlier run than the latest. The comparison is between
        run records: the plan version each ran on, the files each
        patch changed, and each verification's verdicts."""
        if run is None or not decisions:
            return None
        previous_ids = [
            d["run_id"] for d in decisions if d["run_id"] != run["run_id"]
        ]
        if not previous_ids:
            return None
        previous = next(
            (r for r in runs if r["run_id"] == previous_ids[-1]), None
        )
        if previous is None:
            return None
        before_v = previous.get("verification") or {}
        now_v = run.get("verification") or {}
        before_files = set(previous.get("changed_files") or [])
        now_files = set(run.get("changed_files") or [])
        return {
            "previous_run_id": previous["run_id"],
            "plan_version": {
                "before": previous["plan_version"],
                "now": run["plan_version"],
            },
            "patch_files": {
                "added": sorted(now_files - before_files),
                "removed": sorted(before_files - now_files),
                "unchanged": sorted(before_files & now_files),
            },
            "verdicts": {
                "passed": {
                    "before": before_v.get("passed"),
                    "now": now_v.get("passed"),
                },
                "visible": {
                    "before": before_v.get("visible_verdict"),
                    "now": now_v.get("visible_verdict"),
                },
                "hidden": {
                    "before": before_v.get("hidden_verdict"),
                    "now": now_v.get("hidden_verdict"),
                },
            },
        }

    def reviewer_history(self) -> dict:
        """Per-reviewer history, folded from the decision records:
        for each actor, their decisions counted by kind, the work
        orders they decided (in decision order), and the median
        time-to-decision. The derivation of the timing: a decision's
        audit timestamp minus the landing of the run it decided —
        the run record's ``finished_at`` (falling back to its
        ``created_at``); a decision whose run cannot be read, or
        whose landing cannot be parsed, counts in the tallies but
        not in the timing. No tracking table stands behind this:
        the audit records are the history, folded on read."""
        rows = self.store.list_audit_events(list(self._REVIEW_EVENTS))
        by_actor: dict[str, dict] = {}
        for row in rows:
            kind = self._REVIEW_EVENTS[row["event"]]
            entry = by_actor.setdefault(
                row["actor"],
                {
                    "actor": row["actor"],
                    "decisions": 0,
                    "accepted": 0,
                    "rejected": 0,
                    "changes_requested": 0,
                    "work_orders": [],
                    "history": [],
                    "_durations": [],
                },
            )
            entry["decisions"] += 1
            entry[kind] += 1
            wid = row["entity_id"]
            if wid not in entry["work_orders"]:
                entry["work_orders"].append(wid)
            detail = row.get("detail") or {}
            run_id = detail.get("run_id")
            entry["history"].append(
                {
                    "work_order_id": wid,
                    "kind": kind,
                    "at": row["created_at"],
                    "run_id": run_id,
                }
            )
            decided_at = _parse_ts(row["created_at"])
            run = self.store.get_run(run_id) if run_id else None
            landed_at = (
                _parse_ts(run.get("finished_at") or run.get("created_at"))
                if run
                else None
            )
            if decided_at is not None and landed_at is not None:
                entry["_durations"].append(
                    (decided_at - landed_at).total_seconds()
                )
        reviewers = []
        for entry in by_actor.values():
            durations = entry.pop("_durations")
            entry["median_time_to_decision_seconds"] = (
                statistics.median(durations) if durations else None
            )
            entry["timed_decisions"] = len(durations)
            reviewers.append(entry)
        reviewers.sort(key=lambda e: e["actor"])
        return {
            "reviewers": reviewers,
            "derivation": (
                "folded on read from the review decision audit "
                "records; time-to-decision is the decision's "
                "timestamp minus the decided run's finished_at "
                "(created_at as fallback), per decision, and the "
                "median is taken over an actor's timed decisions"
            ),
        }

    # -- internals -------------------------------------------------------------
    def _require_order(self, work_order_id: str) -> dict:
        order = self.store.get_work_order(work_order_id)
        if order is None:
            raise GateError("unknown_work_order", f"no work order {work_order_id!r}")
        return order

    def _set_status(self, order: dict, status: str) -> None:
        order["status"] = status
        order["updated_at"] = utcnow()
        self.store.put_work_order(order)


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a stored ISO timestamp; an absent or unparseable one
    reads as None, never as an exception — the workbench folds
    tolerate records older than the fields they read."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _summarise_args(step: Step) -> dict:
    """Audit-safe step args: edit bodies are long; record their shape,
    not their full text (the patch itself is the durable record)."""
    args = dict(step.args or {})
    if step.tool == "edit_file":
        return {
            "path": args.get("path"),
            "old_chars": len(str(args.get("old", ""))),
            "new_chars": len(str(args.get("new", ""))),
        }
    return args
