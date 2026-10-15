"""HTTP 입출력을 담당한다. 근거 해석·우선순위·액션 선정은 ProductAgent가 맡는다."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence import EvidenceRepository
from .store import ActionStore, DuplicateAction, InvalidStoreInput, StoreNotFound


ROOT = Path(__file__).resolve().parents[2]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProposalRequest(RequestModel):
    topic_id: str = Field(min_length=1, max_length=100)
    snapshot_id: str = Field(default="SNAP-2", min_length=1, max_length=100)


class SaveActionRequest(RequestModel):
    proposal_id: str = Field(min_length=1, max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    notes: str = Field(default="", max_length=10000)


class UpdateActionRequest(RequestModel):
    status: Literal["draft", "in_progress", "done", "dismissed"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    notes: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Provide at least one non-null status, title or notes field.")
        return self


def create_app(service=None, *, repository=None, action_store=None, provider_factory=None,
               state_dir=None, customer_id=None, frontend_dir=None):
    """오프라인 테스트에서는 list_topics/get_topic/run을 구현한 서비스를 주입한다.

    실제 제안 요청마다 공급자와 ProductAgent를 새로 만들어 동시 요청의 영수증이 섞이지 않게 한다.
    앱 시작·조회·액션 관리·명시적 대체 응답에는 모델 키가 필요 없다.
    """
    load_dotenv(ROOT / ".env", override=False)
    state_dir = Path(state_dir or os.environ.get("VIVI_STATE_DIR", ROOT / ".vivi/product"))
    customer_id = customer_id or os.environ.get("VIVI_CUSTOMER_ID", "demo")
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("VIVI_CUSTOMER_ID must be nonempty.")
    store = action_store or ActionStore(state_dir / "actions.sqlite")
    repo = repository
    if service is None and repo is None:
        repo = EvidenceRepository(Path(os.environ.get("VIVI_DATASET_DIR", ROOT / "VIVI_MVP_Sample_Dataset")))
    frontend = Path(frontend_dir or os.environ.get("VIVI_FRONTEND_DIR", ROOT / "frontend/dist")).resolve()
    app = FastAPI(title="VIVI Action Agent", version="0.1.0")
    app.state.action_store, app.state.customer_id = store, customer_id

    def product(*, generating=False):
        if service is not None:
            return service
        from .product import ProductAgent
        provider = None
        if generating:
            if provider_factory is not None:
                provider = provider_factory()
            else:
                from .provider import create_provider, provider_api_key_name
                key_name = provider_api_key_name()
                if os.environ.get(key_name):
                    from .graph import digest
                    provider = create_provider(artifact_dir=state_dir / "customers" / digest(customer_id) / "traces")
        return ProductAgent(repo, provider, state_dir, store)

    @app.middleware("http")
    async def server_tenant_only(request: Request, call_next):
        if (set(request.query_params) & {"customer_id", "tenant_id", "customer", "tenant"}
                or "x-customer-id" in request.headers or "x-tenant-id" in request.headers):
            return JSONResponse(status_code=422, content={
                "detail": "Customer scope is configured by the server and cannot be supplied by the client."
            })
        return await call_next(request)

    @app.exception_handler(StoreNotFound)
    async def not_found(_request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(DuplicateAction)
    async def duplicate(_request, exc):
        return JSONResponse(status_code=409, content={
            "detail": {"message": str(exc), "existing_action_id": exc.action_id}
        })

    @app.exception_handler(InvalidStoreInput)
    async def invalid(_request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(sqlite3.Error)
    async def storage_unavailable(_request, _exc):
        return JSONResponse(status_code=503, content={"detail": "Action storage is temporarily unavailable."})

    @app.get("/api/topics")
    def topics():
        try:
            return product().list_topics()
        except Exception:
            raise HTTPException(503, "Topic data is temporarily unavailable.") from None

    @app.get("/api/topics/{topic_id}")
    def topic(topic_id: str, snapshot_id: str = "SNAP-2"):
        try:
            return product().get_topic(topic_id, snapshot_id)
        except (KeyError, ValueError):
            raise HTTPException(404, "Topic or snapshot not found.") from None
        except Exception:
            raise HTTPException(503, "Topic data is temporarily unavailable.") from None

    @app.post("/api/proposals")
    def propose(body: ProposalRequest):
        try:
            agent = product(generating=True)
            agent.get_topic(body.topic_id, body.snapshot_id)
        except (KeyError, ValueError):
            raise HTTPException(404, "Topic or snapshot not found.") from None
        except Exception:
            raise HTTPException(503, "Proposal service is temporarily unavailable.") from None
        try:
            result = agent.run(customer_id, body.topic_id, body.snapshot_id)
        except Exception:
            raise HTTPException(503, "Proposal generation is temporarily unavailable; retry later.") from None
        proposal_id = None
        if result.get("result_type") in {"propose", "cache_hit"}:
            try:
                proposal_id = store.save_proposal(customer_id, result)["id"]
            except InvalidStoreInput:
                raise HTTPException(503, "The agent returned an invalid proposal; nothing was saved.") from None
        return {**result, "proposal_id": proposal_id}

    @app.get("/api/actions")
    def actions(topic_id: str | None = None):
        return {"items": store.list_actions(customer_id, topic_id)}

    @app.post("/api/actions", status_code=201)
    def save_action(body: SaveActionRequest):
        return store.save_action(customer_id, body.proposal_id, body.title, body.notes)

    @app.patch("/api/actions/{action_id}")
    def update_action(action_id: str, body: UpdateActionRequest):
        return store.update_action(customer_id, action_id, **body.model_dump(exclude_unset=True))

    @app.get("/{asset_path:path}", include_in_schema=False)
    def frontend_file(asset_path: str):
        if asset_path == "api" or asset_path.startswith("api/"):
            raise HTTPException(404, "API route not found.")
        candidate = (frontend / asset_path).resolve()
        if not candidate.is_relative_to(frontend):
            raise HTTPException(404, "File not found.")
        if candidate.is_file():
            return FileResponse(candidate)
        if "." in Path(asset_path).name or not (frontend / "index.html").is_file():
            raise HTTPException(404, "File not found.")
        return FileResponse(frontend / "index.html")

    return app


app = create_app()
