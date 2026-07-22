from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from taxlink_nfse.config import AppConfig
from taxlink_nfse.jobs import CollectionJobService
from taxlink_nfse.scheduler import TaxLinkScheduler
from taxlink_nfse.storage import SqliteRepository
from taxlink_nfse.sync import MirrorSyncService


class CollectionRequest(BaseModel):
    unit_code: str | None = Field(default=None, max_length=100)


def create_app(config: AppConfig) -> Any:
    repository = SqliteRepository(config.collector.database_path)
    repository.initialize(config.units)
    jobs = CollectionJobService(config, repository)
    sync = MirrorSyncService(config.collector.database_path, config.sync, repository)
    scheduler = TaxLinkScheduler(config, repository, jobs, sync)
    security = HTTPBearer(auto_error=False)

    def authorize(
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> str:
        expected = os.environ.get(config.api.bearer_token_env, "")
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Variavel {config.api.bearer_token_env} nao configurada.",
            )
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not secrets.compare_digest(credentials.credentials, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token Bearer invalido.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return "php-contracts"

    @asynccontextmanager
    async def lifespan(_: Any):
        repository.recover_interrupted_work()
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()

    app = FastAPI(
        title="TaxLink Collector API",
        version="0.2.0",
        lifespan=lifespan,
    )

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/coleta/executar", status_code=status.HTTP_202_ACCEPTED)
    def execute_collection(
        background_tasks: BackgroundTasks,
        request: CollectionRequest | None = None,
        requested_by: str = Depends(authorize),
    ) -> dict[str, str]:
        try:
            job_id = jobs.enqueue(
                "API", request.unit_code if request is not None else None, requested_by
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        background_tasks.add_task(scheduler.execute_collection_and_sync, job_id)
        return {"execution_id": job_id, "status": "QUEUED"}

    @app.get("/api/v1/coleta/status/{execution_id}")
    def collection_status(
        execution_id: str, _: str = Depends(authorize)
    ) -> dict[str, Any]:
        job = repository.collection_job(execution_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Execucao nao encontrada.")
        return {**job, "runs": repository.collection_runs_for_job(execution_id)}

    @app.get("/api/v1/coleta/status")
    def recent_collections(_: str = Depends(authorize)) -> list[dict[str, Any]]:
        return repository.recent_collection_jobs()

    @app.post("/api/v1/sincronizacao/executar", status_code=status.HTTP_202_ACCEPTED)
    def execute_sync(
        background_tasks: BackgroundTasks, _: str = Depends(authorize)
    ) -> dict[str, str]:
        try:
            sync_id = sync.enqueue("API")
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background_tasks.add_task(sync.execute, sync_id)
        return {"sync_id": sync_id, "status": "QUEUED"}

    @app.get("/api/v1/sincronizacao/status/{sync_id}")
    def sync_status(sync_id: str, _: str = Depends(authorize)) -> dict[str, Any]:
        result = repository.sync_run(sync_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Sincronizacao nao encontrada.")
        return result

    return app
