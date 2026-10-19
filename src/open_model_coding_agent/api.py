"""FastAPI surface: health, readiness, the console, and the shell's API.

The API is thin — every decision lives in the service. Governance
refusals (GateError) surface as 409 with the refusal's code and
message in the server's own words.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .config import load_settings
from .errors import GateError
from .logging_setup import request_id_var
from .model_port import PlanDraft, PlanStepDraft
from .models import (
    AmendPlanIn,
    BenchmarkIn,
    ChallengeAnswerIn,
    ComparisonIn,
    EvaluateIn,
    PlanDecisionIn,
    PromoteIn,
    ReviewDecisionIn,
    RollbackIn,
    RunAbandonIn,
    RunActionIn,
    RunStartIn,
    TemplateIn,
    TemplateInstantiateIn,
    TemplatePublishIn,
    WorkOrderIn,
)
from .service import CodingService

STATIC = Path(__file__).resolve().parent / "static"


def create_app(settings=None) -> FastAPI:
    settings = settings or load_settings()
    service = CodingService(settings)
    app = FastAPI(title="Open-Model Coding Agent — Trida AI Blueprint")
    app.state.service = service
    app.state.settings = settings

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        token = request_id_var.set(request.headers.get("x-request-id") or uuid.uuid4().hex[:12])
        try:
            return await call_next(request)
        finally:
            request_id_var.reset(token)

    @app.exception_handler(GateError)
    async def gate_error_handler(_request: Request, exc: GateError):
        return JSONResponse(
            status_code=409, content={"detail": exc.message, "code": exc.code}
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/readiness")
    def readiness():
        profile = service.serving_profile()
        return {
            "status": "ready",
            "store": settings.store_kind,
            "model_profile": profile.profile_id,
            "model_label": profile.label,
            "default_model_profile": service.default_profile_id(),
            "baseten_base_url": profile.base_url,
            "baseten_model": profile.model,
        }

    @app.get("/")
    def console():
        return FileResponse(STATIC / "index.html")

    # -- costs -------------------------------------------------------------
    @app.get("/api/costs")
    def costs():
        return service.cost_report()

    # -- the review workbench (round 11) -----------------------------------
    @app.get("/api/review-queue")
    def review_queue():
        return service.review_queue()

    @app.get("/api/reviewers")
    def reviewers():
        return service.reviewer_history()

    # -- benchmarks and the promotion gate ----------------------------------
    @app.get("/api/benchmarks")
    def benchmarks():
        return service.list_benchmarks()

    @app.post("/api/benchmarks", status_code=201)
    def run_benchmark(body: BenchmarkIn):
        return service.run_benchmark(
            body.profile_id, set_name=body.set_name, actor=body.actor
        )

    @app.get("/api/promotions")
    def promotions():
        return service.promotion_state()

    @app.post("/api/promotions/evaluate", status_code=201)
    def evaluate(body: EvaluateIn):
        return service.evaluate_profile(
            body.profile_id, actor=body.actor, times=body.times
        )

    @app.post("/api/promotions/promote")
    def promote(body: PromoteIn):
        return service.promote(
            body.profile_id, actor=body.actor, rationale=body.rationale
        )

    @app.post("/api/promotions/rollback")
    def rollback(body: RollbackIn):
        return service.rollback(actor=body.actor, rationale=body.rationale)

    # -- shortlist comparisons (round 8) ------------------------------------
    @app.post("/api/comparisons", status_code=201)
    def run_comparison(body: ComparisonIn):
        return service.run_comparison(set_name=body.set_name, actor=body.actor)

    @app.get("/api/comparisons")
    def comparisons():
        return service.list_comparisons()

    @app.get("/api/comparisons/{comparison_id}")
    def comparison(comparison_id: str):
        report = service.get_comparison(comparison_id)
        if report is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"no comparison {comparison_id!r}",
                    "code": "unknown_comparison",
                },
            )
        return report

    # -- the task library (round 12) ---------------------------------------
    @app.post("/api/templates", status_code=201)
    def create_template(body: TemplateIn):
        return service.create_template(body)

    @app.get("/api/templates")
    def templates():
        return service.list_templates()

    @app.get("/api/templates/{name}")
    def template(name: str):
        record = service.get_template(name)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"no template {name!r}",
                    "code": "unknown_template",
                },
            )
        return record

    @app.get("/api/templates/{name}/versions/{version}")
    def template_version(name: str, version: int):
        record = service.get_template(name, version)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"no template {name!r} version {version}",
                    "code": "unknown_template",
                },
            )
        return record

    @app.post("/api/templates/{name}/versions/{version}/publish")
    def publish_template(name: str, version: int, body: TemplatePublishIn):
        return service.publish_template(name, version, actor=body.actor)

    @app.post("/api/templates/{name}/instantiate", status_code=201)
    def instantiate_template(name: str, body: TemplateInstantiateIn):
        return service.instantiate_template(
            name,
            version=body.version,
            parameters=body.parameters,
            actor=body.actor,
            title=body.title,
            target_path=body.target_path,
            grading_tests=body.grading_tests,
            gold_task_id=body.gold_task_id,
            file_scope=body.file_scope,
        )

    @app.get("/api/templates/{name}/audit")
    def template_audit(name: str):
        return service.template_audit(name)

    # -- gold tasks ------------------------------------------------------
    @app.get("/api/gold-tasks")
    def gold_tasks():
        return [
            {
                "id": t.id,
                "title": t.title,
                "task_text": t.task_text,
                "file_scope": t.file_scope,
            }
            for t in service.gold.tasks
        ]

    # -- work orders -------------------------------------------------------
    @app.post("/api/work-orders", status_code=201)
    def create_work_order(body: WorkOrderIn):
        return service.create_work_order(body, actor=body.requested_by)

    @app.post("/api/work-orders/from-gold/{gold_task_id}", status_code=201)
    def create_from_gold(gold_task_id: str):
        return service.create_gold_work_order(gold_task_id)

    @app.get("/api/work-orders")
    def list_work_orders():
        return service.list_work_orders()

    @app.get("/api/work-orders/{work_order_id}")
    def work_order_detail(work_order_id: str):
        detail = service.work_order_detail(work_order_id)
        if detail is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"no work order {work_order_id!r}", "code": "unknown_work_order"},
            )
        return detail

    @app.post("/api/work-orders/{work_order_id}/plan")
    def draft_plan(work_order_id: str):
        return service.draft_plan(work_order_id)

    @app.post("/api/work-orders/{work_order_id}/plan/decide")
    def decide_plan(work_order_id: str, body: PlanDecisionIn):
        return service.decide_plan(
            work_order_id, approve=body.approve, actor=body.actor, note=body.note
        )

    @app.post("/api/work-orders/{work_order_id}/plan/amend")
    def amend_plan(work_order_id: str, body: AmendPlanIn):
        draft = PlanDraft(
            approach=body.approach,
            steps=[
                PlanStepDraft(
                    intent=step.intent, files=list(step.files), tests=list(step.tests)
                )
                for step in body.steps
            ],
        )
        return service.amend_plan(work_order_id, draft=draft, actor=body.actor)

    @app.post("/api/work-orders/{work_order_id}/runs", status_code=201)
    def start_run(work_order_id: str, body: RunStartIn | None = None):
        body = body or RunStartIn()
        return service.start_run(
            work_order_id, actor=body.actor, plan_version=body.plan_version
        )

    @app.post("/api/work-orders/{work_order_id}/runs/{run_id}/resume")
    def resume_run(work_order_id: str, run_id: str, body: RunActionIn):
        return service.resume_run(work_order_id, run_id, actor=body.actor)

    @app.post("/api/work-orders/{work_order_id}/runs/{run_id}/restart")
    def restart_run(work_order_id: str, run_id: str, body: RunActionIn):
        return service.restart_run(work_order_id, run_id, actor=body.actor)

    @app.post("/api/work-orders/{work_order_id}/runs/{run_id}/abandon")
    def abandon_run(work_order_id: str, run_id: str, body: RunAbandonIn):
        return service.abandon_run(
            work_order_id, run_id, actor=body.actor, reason=body.reason
        )

    @app.get("/api/work-orders/{work_order_id}/review-packet")
    def review_packet(work_order_id: str):
        packet = service.review_packet(work_order_id)
        if packet is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"no work order {work_order_id!r}", "code": "unknown_work_order"},
            )
        return packet

    @app.post("/api/work-orders/{work_order_id}/review")
    def decide_review(work_order_id: str, body: ReviewDecisionIn):
        return service.decide_review(
            work_order_id,
            accept=body.accept,
            actor=body.actor,
            reason=body.reason,
            decision=body.decision,
        )

    # -- the challenge pass (round 13) --------------------------------------
    @app.get("/api/work-orders/{work_order_id}/challenges")
    def list_challenges(work_order_id: str, run_id: str | None = None):
        if service.store.get_work_order(work_order_id) is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"no work order {work_order_id!r}", "code": "unknown_work_order"},
            )
        return service.list_challenges(work_order_id, run_id=run_id)

    @app.post(
        "/api/work-orders/{work_order_id}/challenges/{challenge_id}/answer"
    )
    def answer_challenge(
        work_order_id: str, challenge_id: str, body: ChallengeAnswerIn
    ):
        return service.answer_challenge(
            work_order_id,
            challenge_id,
            kind=body.kind,
            actor=body.actor,
            text=body.text,
            cites=body.cites,
            reason=body.reason,
        )

    return app


app = create_app()
