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
    PlanDecisionIn,
    ReviewDecisionIn,
    RunAbandonIn,
    RunActionIn,
    RunStartIn,
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
        profile = settings.profile
        return {
            "status": "ready",
            "store": settings.store_kind,
            "model_profile": profile.profile_id,
            "model_label": profile.label,
            "baseten_base_url": profile.base_url,
            "baseten_model": profile.model,
        }

    @app.get("/")
    def console():
        return FileResponse(STATIC / "index.html")

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

    @app.post("/api/work-orders/{work_order_id}/review")
    def decide_review(work_order_id: str, body: ReviewDecisionIn):
        return service.decide_review(
            work_order_id, accept=body.accept, actor=body.actor, reason=body.reason
        )

    return app


app = create_app()
