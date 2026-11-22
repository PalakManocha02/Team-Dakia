from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
import hmac
import os
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from app.contracts import (Contract, SourcingRequest, WorkflowRun, WorkflowEvent, RunState as S,
                           Spec, Approval, LedgerEntry, BudgetReservation, Decision, Order, RFQDraft, now)
from app.domain.state_machines import transition
from app.persistence.database import Database
from app.persistence.repositories import get_run, save_run, list_runs, ledger
from app.workflow.orchestrator import approve, fingerprint


class BudgetUpdate(Contract):
    research_budget_minor: int | None = Field(default=None, ge=0)
    credit_budget: float | None = Field(default=None, ge=0, le=1000)


def _env_number(name, cast):
    value = os.getenv(name)
    return cast(value) if value not in (None, "") else None


class ClarificationAnswer(Contract):
    answer: str = Field(min_length=1, max_length=10000)
    specs: list[Spec] | None = None


def create_app(database: Database | None = None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.db = database or Database()
        yield

    app = FastAPI(title="Sourcing Desk", version="0.1.0", lifespan=lifespan,
                  description="Local procurement skeleton. Fixture data, simulated payments, RFQ drafts only.")

    # Hosted deployments: a shared passcode guards every /api route except health, plus spend caps.
    # All three are unset locally, so development and tests behave exactly as before.
    access_code = os.getenv("ACCESS_CODE") or None
    max_runs_per_day = _env_number("MAX_RUNS_PER_DAY", int)
    max_credit_budget = _env_number("MAX_CREDIT_BUDGET", float)

    @app.middleware("http")
    async def passcode(request: Request, call_next):
        path = request.url.path
        if access_code and path.startswith("/api") and path != "/api/health":
            supplied = request.headers.get("x-access-code", "")
            if not hmac.compare_digest(supplied.encode(), access_code.encode()):
                return JSONResponse(status_code=401, content={"error": {"code": "ACCESS_CODE_REQUIRED", "message": "Enter the team passcode to use this desk."}})
        return await call_next(request)

    def check_credit_cap(value):
        if max_credit_budget is not None and value is not None and value > max_credit_budget:
            raise ValueError(f"credit ceiling above {max_credit_budget:g} is not allowed on this hosted desk")

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "Run not found"}})

    @app.exception_handler(ValueError)
    async def conflict(_, exc):
        return JSONResponse(status_code=409, content={"error": {"code": "INVALID_STATE", "message": str(exc)}})

    @app.exception_handler(RequestValidationError)
    async def validation(_, exc):
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": "Invalid request", "fields": [".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors()]}})

    @app.get("/api/health")
    def health():
        return {"status": "ok", "payments": "mock",
                "gemini_configured": bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GEMINI_API_KEY")),
                "crustdata_configured": bool(os.getenv("CRUSTDATA_API_KEY")), "live_integrations": "Crustdata (company search, enrich, job counts, news) requires worker key; Gemini parsing/extraction opt-in via llm_enabled (Vertex AI service account or GEMINI_API_KEY)", "worker": "separate process required"}

    @app.post("/api/runs", response_model=WorkflowRun, status_code=201)
    def create(body: SourcingRequest, request: Request):
        check_credit_cap(body.credit_budget)
        if max_runs_per_day is not None:
            with request.app.state.db.transaction() as c:
                today = sum(1 for r in list_runs(c) if r.created_at > now() - timedelta(days=1))
            if today >= max_runs_per_day:
                return JSONResponse(status_code=429, content={"error": {"code": "DAILY_RUN_LIMIT", "message": f"This hosted desk allows {max_runs_per_day} new requests per 24 hours. Try again later."}})
        run = WorkflowRun(request=body)
        run.events.append(WorkflowEvent(state=S.CREATED, message=f"{body.mode} run created; awaiting worker."))
        with request.app.state.db.transaction() as c: save_run(c, run)
        return run

    @app.get("/api/runs", response_model=list[WorkflowRun])
    def index(request: Request):
        with request.app.state.db.transaction() as c: return list_runs(c)

    @app.get("/api/runs/{run_id}", response_model=WorkflowRun)
    def read(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id)

    @app.get("/api/runs/{run_id}/events", response_model=list[WorkflowEvent])
    def events(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id).events

    @app.get("/api/runs/{run_id}/plan", response_model=list[Decision])
    def plan(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id).decisions

    @app.get("/api/runs/{run_id}/orders", response_model=list[Order])
    def orders(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id).orders

    @app.get("/api/runs/{run_id}/rfqs", response_model=list[RFQDraft])
    def rfqs(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id).rfqs

    @app.get("/api/runs/{run_id}/ledger", response_model=list[LedgerEntry])
    def spend(run_id: str, request: Request):
        with request.app.state.db.transaction() as c:
            get_run(c, run_id)
            return ledger(c, run_id)

    @app.get("/api/runs/{run_id}/reservations", response_model=list[BudgetReservation])
    def reservations(run_id: str, request: Request):
        with request.app.state.db.transaction() as c:
            get_run(c, run_id)
            return [BudgetReservation(id=r.id, run_id=r.run_id, amount_minor=r.amount, status=r.status) for r in c.exec_driver_sql("SELECT * FROM reservations WHERE run_id=?", (run_id,))]

    @app.patch("/api/runs/{run_id}/budget", response_model=WorkflowRun)
    def budget(run_id: str, body: BudgetUpdate, request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c, run_id)
            if body.research_budget_minor is None and body.credit_budget is None:
                raise ValueError("send research_budget_minor, credit_budget or both")
            if body.research_budget_minor is not None:
                run.request.research_budget_minor = body.research_budget_minor
                run.events.append(WorkflowEvent(state=run.state, message=f"Research ceiling updated to {body.research_budget_minor} paise; committed spend remains visible."))
            if body.credit_budget is not None:
                check_credit_cap(body.credit_budget)
                run.request.credit_budget = body.credit_budget
                run.events.append(WorkflowEvent(state=run.state, message=f"Crustdata credit ceiling set to {body.credit_budget:g}; credits already spent stay on the ledger and no new call may exceed it."))
            save_run(c, run)
        return run

    @app.post("/api/runs/{run_id}/clarifications", response_model=WorkflowRun)
    def clarification(run_id: str, body: ClarificationAnswer, request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c, run_id)
            if run.state != S.WAITING_FOR_CLARIFICATION: raise ValueError("not awaiting clarification")
            run.clarification.answer = body.answer
            run.request.text += "\nClarification: " + body.answer
            if body.specs is not None:
                updated = run.request.model_dump()
                updated["specs"] = [s.model_dump() for s in body.specs]
                run.request = SourcingRequest.model_validate(updated)
            transition(run, S.PARSING, "Clarification saved; worker will parse again.")
            save_run(c, run)
        return run

    @app.post("/api/runs/{run_id}/approvals", response_model=WorkflowRun)
    def approval(run_id: str, body: Approval, request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c, run_id)
            approve(run, body)
            save_run(c, run)
        return run

    @app.post("/api/runs/{run_id}/resume", response_model=WorkflowRun)
    def resume(run_id: str, request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c, run_id)
            if run.state == S.FAILED:
                raise ValueError("Inspect run.error and configure the missing integration before creating a new run. Failed paid calls are not silently retried.")
            if run.state == S.WAITING_FOR_APPROVAL and (run.plan_version != fingerprint(run) or any(q.expires_at <= now() for q in run.quotes)):
                transition(run, S.DECIDING, "Replanning changed/expired quote; approval invalidated.")
            elif run.state == S.WAITING_FOR_CLARIFICATION:
                raise ValueError("answer the clarification before resuming")
            save_run(c, run)
        return run

    @app.get("/api/access")
    def access():
        return {"ok": True}

    # Serve the built dashboard from the same origin when FRONTEND_DIST points at it.
    dist = os.getenv("FRONTEND_DIST")
    if dist and Path(dist, "index.html").is_file():
        root = Path(dist).resolve()
        if (root / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path == "api" or path.startswith("api/"):
                return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "Unknown API route"}})
            target = (root / path).resolve()
            if path and target.is_file() and target.is_relative_to(root):
                return FileResponse(target)
            return FileResponse(root / "index.html")

    return app


app = create_app()
