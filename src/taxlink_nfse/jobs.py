from __future__ import annotations

import logging
import threading

from taxlink_nfse.collector import Collector
from taxlink_nfse.config import AppConfig
from taxlink_nfse.storage import SqliteRepository


class CollectionJobService:
    """Fila persistente com um unico escritor de coleta por processo."""

    def __init__(self, config: AppConfig, repository: SqliteRepository):
        self.config = config
        self.repository = repository
        self._execution_lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

    def enqueue(
        self,
        trigger_source: str,
        unit_code: str | None = None,
        requested_by: str = "",
    ) -> str:
        self.repository.initialize(self.config.units)
        return self.repository.create_collection_job(
            trigger_source=trigger_source,
            unit_code=unit_code,
            requested_by=requested_by,
        )

    def execute(self, job_id: str) -> bool:
        if not self._execution_lock.acquire(blocking=False):
            self.logger.info("Execucao %s permaneceu na fila; coletor ocupado", job_id)
            return False
        try:
            job = self.repository.collection_job(job_id)
            if job is None:
                raise ValueError(f"Execucao de coleta nao encontrada: {job_id}")
            if not self.repository.claim_collection_job(job_id):
                return False
            collector = Collector(self.config, repository=self.repository)
            summary = collector.run_cycle(
                force=True,
                job_id=job_id,
                unit_code=str(job["requested_unit_code"] or "") or None,
            )
            self.repository.finish_collection_job(job_id, summary.as_dict())
            return True
        except Exception as exc:
            self.logger.exception("Falha na execucao de coleta %s", job_id)
            self.repository.fail_collection_job(job_id, str(exc))
            return False
        finally:
            self._execution_lock.release()

    def execute_next(self) -> str | None:
        job_id = self.repository.next_queued_collection_job()
        if job_id is None:
            return None
        self.execute(job_id)
        return job_id
