from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from taxlink_nfse.config import AppConfig
from taxlink_nfse.jobs import CollectionJobService
from taxlink_nfse.storage import SqliteRepository
from taxlink_nfse.sync import MirrorSyncService


class TaxLinkScheduler:
    def __init__(
        self,
        config: AppConfig,
        repository: SqliteRepository,
        jobs: CollectionJobService,
        sync: MirrorSyncService,
    ):
        self.config = config
        self.repository = repository
        self.jobs = jobs
        self.sync = sync
        self._scheduler = None
        self.logger = logging.getLogger(__name__)

    def start(self) -> None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError as exc:
            raise RuntimeError("Instale apscheduler para iniciar o servico da API.") from exc

        timezone = ZoneInfo(self.config.scheduler.timezone)
        scheduler = BackgroundScheduler(timezone=timezone, daemon=True)
        scheduler.add_job(
            self.process_queue,
            "interval",
            seconds=self.config.scheduler.job_poll_seconds,
            id="taxlink-job-worker",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        if self.config.scheduler.enabled:
            for index, daily_time in enumerate(self.config.scheduler.daily_times):
                hour, minute = (int(part) for part in daily_time.split(":"))
                scheduler.add_job(
                    self.enqueue_scheduled_collection,
                    CronTrigger(hour=hour, minute=minute, timezone=timezone),
                    id=f"taxlink-daily-{index}",
                    max_instances=1,
                    coalesce=True,
                    replace_existing=True,
                )
        scheduler.start()
        self._scheduler = scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def enqueue_scheduled_collection(self) -> str:
        job_id = self.jobs.enqueue("SCHEDULER")
        self.logger.info("Coleta agendada enfileirada: %s", job_id)
        return job_id

    def process_queue(self) -> None:
        job_id = self.repository.next_queued_collection_job()
        if job_id is not None:
            self.execute_collection_and_sync(job_id)
        elif self.config.sync.enabled:
            self.sync.execute_next()
        self._purge_outbox()

    def _purge_outbox(self) -> None:
        try:
            deleted = self.repository.purge_outbox(
                self.config.collector.outbox_retention_days
            )
            if deleted:
                self.logger.info(
                    "Outbox: %d registro(s) com mais de %d dia(s) removido(s)",
                    deleted,
                    self.config.collector.outbox_retention_days,
                )
        except Exception:
            self.logger.exception("Falha ao limpar o integration_outbox")

    def execute_collection_and_sync(self, job_id: str) -> None:
        executed = self.jobs.execute(job_id)
        job = self.repository.collection_job(job_id)
        if not executed or job is None or job["status"] != "SUCCESS":
            return
        if self.config.sync.enabled:
            sync_id = self.sync.enqueue("COLLECTION")
            self.sync.execute(sync_id)
