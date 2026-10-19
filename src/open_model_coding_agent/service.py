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
  approved.
- A run works on an isolated copy of the target repo. The model's
  steps pass through the tool port, where scope and command allowlists
  are enforced in code. Every tool call is an audit record.
- The run's verdict is computed by the shell, never reported by the
  agent: the patch is applied to a fresh copy of the target and the
  grading tests run there. SUCCEEDED means exactly that the patch
  applies cleanly, touches only in-scope files, and the grading tests
  pass on the fresh copy.
- A human reviews a SUCCEEDED run: accept exports the .patch file,
  reject records the reason. Nothing is ever applied to the source
  repo by the agent's hand.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from .config import Settings
from .errors import GateError
from .gold import GoldSuite
from .logging_setup import log_event
from .model_port import Step, make_model, validate_plan_draft
from .models import Plan, Run, WorkOrder, WorkOrderIn
from .patch import PatchError, apply_patch, build_patch
from .store import SqlStore, utcnow
from .tools import SandboxTools, dispatch

_IGNORE = shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "*.pyc")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class CodingService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = SqlStore(settings)
        self.gold = GoldSuite(settings.gold_dir)
        for folder in (settings.targets_dir, settings.runs_dir, settings.artifacts_dir):
            folder.mkdir(parents=True, exist_ok=True)

    # -- audit ---------------------------------------------------------
    def _audit(self, entity: str, entity_id: str, event: str, actor: str, **detail):
        self.store.append_audit(entity, entity_id, event, actor, detail)

    # -- model usage -----------------------------------------------------
    def _persist_usage(self, model, *, work_order_id: str, run_id: str | None) -> None:
        """Drain the model's accounting into stored usage records,
        stamped with the call's context and the serving profile."""
        for record in model.drain_usage():
            self.store.append_usage(
                {
                    **record,
                    "run_id": run_id,
                    "work_order_id": work_order_id,
                    "profile_id": self.settings.model_profile,
                }
            )

    def _attach_usage(self, run: dict) -> dict:
        rows = self.store.list_usage(run_id=run["run_id"])
        run["usage"] = rows
        run["usage_totals"] = {
            "calls": len(rows),
            "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
            "completion_tokens": sum(r["completion_tokens"] for r in rows),
            "latency_ms": sum(r["latency_ms"] for r in rows),
        }
        return run

    def _make_model(self, *, scripts=None):
        return make_model(
            self.settings.model_profile,
            scripts=scripts,
            profile=self.settings.profile,
            transport=self.settings.model_transport,
        )

    def audit(self, work_order_id: str) -> list[dict]:
        return self.store.list_audit("work_order", work_order_id)

    # -- work orders ------------------------------------------------------
    def create_work_order(self, body: WorkOrderIn, actor: str = "console") -> dict:
        order = WorkOrder(
            work_order_id=_new_id("wo"),
            title=body.title,
            task_text=body.task_text,
            file_scope=list(body.file_scope),
            gold_task_id=body.gold_task_id,
            grading_tests=list(body.grading_tests),
            requested_by=body.requested_by,
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
        )
        log_event("work_order_created", work_order_id=order.work_order_id)
        return order.model_dump()

    def create_gold_work_order(self, gold_task_id: str, actor: str = "console") -> dict:
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
        )

    def _materialise_target(self, task) -> Path:
        """The task's repo on disk: the workshop, with the broken
        overlay applied. This directory is the 'developer's repo' the
        work order runs against; runs copy it, never touch it."""
        target = self.settings.targets_dir / task.id
        if target.exists():
            shutil.rmtree(target)
        workshop_src = self.settings.gold_dir.parent.parent / "workshop"
        shutil.copytree(workshop_src, target / "workshop", ignore=_IGNORE)
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
        return {
            "work_order": order,
            "plan": self.store.get_plan(work_order_id),
            "runs": runs,
            "usage": self.store.list_usage(work_order_id=work_order_id),
            "audit": self.audit(work_order_id),
        }

    # -- plan gate ---------------------------------------------------------
    def draft_plan(self, work_order_id: str, actor: str = "console") -> dict:
        order = self._require_order(work_order_id)
        if order["status"] not in ("draft", "plan_rejected"):
            raise GateError(
                "plan_not_draftable",
                f"a plan can only be drafted from draft/plan_rejected, "
                f"not {order['status']}",
            )
        target = Path(order["target_path"])
        repo_files = (
            sorted(
                p.relative_to(target).as_posix()
                for p in target.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            )
            if target.is_dir()
            else []
        )
        model = self._make_model()
        plan_input = dict(order)
        gold = self.gold.get(order["gold_task_id"]) if order.get("gold_task_id") else None
        if gold is not None:
            plan_input["_plan_approach"] = gold.plan_approach
        try:
            draft = model.draft_plan(work_order=plan_input, repo_files=repo_files)
        finally:
            self._persist_usage(model, work_order_id=work_order_id, run_id=None)
        # One validator for every model's draft, scripted or live.
        validate_plan_draft(draft, file_scope=list(order["file_scope"]))
        plan = Plan(
            work_order_id=work_order_id,
            version=1,
            approach=draft.approach,
            files=draft.files,
            tests=draft.tests,
            model_profile=self.settings.model_profile,
            created_at=utcnow(),
        )
        self.store.put_plan(plan.model_dump())
        self._set_status(order, "plan_ready")
        self._audit(
            "work_order", work_order_id, "plan_drafted", actor,
            version=1, files=plan.files, model_profile=plan.model_profile,
        )
        return plan.model_dump()

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
        self.store.put_plan(plan)
        self._set_status(order, "approved" if approve else "plan_rejected")
        self._audit(
            "work_order", work_order_id,
            "plan_approved" if approve else "plan_rejected", actor,
            version=plan["version"], note=note,
        )
        return plan

    # -- runs ----------------------------------------------------------------
    def start_run(self, work_order_id: str, actor: str = "console") -> dict:
        order = self._require_order(work_order_id)
        plan = self.store.get_plan(work_order_id)
        if order["status"] != "approved" or plan is None or plan["status"] != "approved":
            raise GateError(
                "plan_not_approved",
                f"execution refused: work order {work_order_id} has no approved "
                f"plan (status {order['status']})",
            )
        scripts: dict[str, list[dict]] = {}
        gold = self.gold.get(order["gold_task_id"]) if order.get("gold_task_id") else None
        if gold is not None:
            scripts[gold.id] = gold.scripted_edits
        model = self._make_model(scripts=scripts)

        run_id = _new_id("run")
        run_dir = self.settings.runs_dir / run_id
        workspace = run_dir / "workspace"
        pristine = run_dir / "pristine"
        target = Path(order["target_path"])
        if not target.is_dir():
            raise GateError(
                "target_missing", f"target repo {target} does not exist on disk"
            )
        shutil.copytree(target, workspace, ignore=_IGNORE)
        shutil.copytree(target, pristine, ignore=_IGNORE)

        run = Run(
            run_id=run_id,
            work_order_id=work_order_id,
            plan_version=plan["version"],
            model_profile=self.settings.model_profile,
            workspace=str(workspace),
            created_at=utcnow(),
        )
        self.store.put_run(run.model_dump())
        self._set_status(order, "running")
        self._audit(
            "work_order", work_order_id, "run_started", actor,
            run_id=run_id, plan_version=plan["version"],
            model_profile=run.model_profile,
        )
        log_event("run_started", work_order_id=work_order_id, run_id=run_id)

        tools = SandboxTools(
            workspace,
            scope=plan["files"],
            test_timeout=self.settings.policy.test_timeout_seconds,
        )
        transcript: list[dict] = []
        steps = 0
        max_steps = self.settings.policy.max_steps
        model_error: GateError | None = None
        try:
            model.start_run(work_order=order, plan=plan)
            while steps < max_steps:
                step: Step = model.next_step(transcript=transcript)
                self._persist_usage(model, work_order_id=work_order_id, run_id=run_id)
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
                self._audit(
                    "work_order", work_order_id, "tool_call", "agent",
                    run_id=run_id, tool=step.tool, ok=result.ok,
                    refused=result.refused, args=_summarise_args(step),
                )
                if result.refused and step.tool == "edit_file":
                    self._audit(
                        "work_order", work_order_id, "edit_refused", "shell",
                        run_id=run_id, path=(step.args or {}).get("path"),
                        reason=result.output,
                    )
        except GateError as exc:
            # A model-side failure (auth, rate limit, a malformed
            # response) ends the run FAILED with the reason recorded —
            # never a hang, never a silent retry, never a crash that
            # leaves the order 'running'.
            model_error = exc
            self._persist_usage(model, work_order_id=work_order_id, run_id=run_id)
        run.steps_used = steps

        if model_error is not None:
            reason = f"model call failed: {model_error.code} — {model_error.message}"
            run.status = "failed"
            run.failure_reason = reason
            run.verification = {
                "passed": False,
                "reason": reason,
                "model_error": model_error.code,
            }
            run.finished_at = utcnow()
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
        self.store.put_run(run.model_dump())
        self._audit(
            "work_order", work_order_id, "verification_completed", "shell",
            run_id=run_id, passed=verification["passed"],
            reason=verification["reason"], changed_files=changed,
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
        return self._attach_usage(run.model_dump())

    def _verify(
        self, order: dict, plan: dict, pristine: Path, patch_text: str, changed: list[str]
    ) -> dict:
        """The verdict, computed by the shell on a fresh copy.

        Order of checks: the patch must exist, touch only in-scope
        files, apply cleanly to a pristine copy, and pass the grading
        tests there. The agent's in-run results play no part.
        """
        scope = set(plan["files"])
        out_of_scope = [f for f in changed if f not in scope]
        if out_of_scope:
            return {
                "passed": False,
                "reason": f"patch touches files outside the approved scope: "
                f"{out_of_scope}",
                "out_of_scope": out_of_scope,
            }
        if not changed:
            return {"passed": False, "reason": "the run produced no patch"}
        verify_dir = pristine.parent / "verify"
        if verify_dir.exists():
            shutil.rmtree(verify_dir)
        shutil.copytree(pristine, verify_dir, ignore=_IGNORE)
        try:
            apply_patch(verify_dir, patch_text)
        except PatchError as exc:
            return {"passed": False, "reason": f"patch does not apply cleanly: {exc}"}
        grading_paths = self._stage_grading(order, verify_dir)
        if not grading_paths:
            return {
                "passed": False,
                "reason": "no grading tests named for this work order",
            }
        tools = SandboxTools(
            verify_dir, scope=[], test_timeout=self.settings.policy.test_timeout_seconds
        )
        result = tools.run_tests(grading_paths)
        return {
            "passed": result.ok,
            "reason": "grading tests pass on the patched pristine copy"
            if result.ok
            else "grading tests fail on the patched pristine copy",
            "grading_tests": grading_paths,
            "grading_output": result.output[-2000:],
        }

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

    # -- review gate -----------------------------------------------------------
    def decide_review(
        self, work_order_id: str, *, accept: bool, actor: str, reason: str | None = None
    ) -> dict:
        order = self._require_order(work_order_id)
        run = self.store.latest_run(work_order_id)
        if order["status"] != "succeeded" or run is None:
            raise GateError(
                "no_verified_run_awaiting_review",
                f"work order {work_order_id} has no verified run awaiting "
                f"review (status {order['status']})",
            )
        if accept:
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
