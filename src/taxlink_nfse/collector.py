from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from taxlink_nfse.adn import AdnClient
from taxlink_nfse.config import AppConfig, UnitConfig
from taxlink_nfse.storage import SqliteRepository


@dataclass(slots=True)
class CycleSummary:
    units_processed: int = 0
    batches_requested: int = 0
    documents_received: int = 0
    documents_stored: int = 0
    danfse_pdfs_stored: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "units_processed": self.units_processed,
            "batches_requested": self.batches_requested,
            "documents_received": self.documents_received,
            "documents_stored": self.documents_stored,
            "danfse_pdfs_stored": self.danfse_pdfs_stored,
            "errors": self.errors,
        }


class Collector:
    def __init__(
        self,
        config: AppConfig,
        repository: SqliteRepository | None = None,
        client: AdnClient | None = None,
    ):
        self.config = config
        self.repository = repository or SqliteRepository(config.collector.database_path)
        self.client = client or AdnClient(config.adn, config.collector)
        self.logger = logging.getLogger(__name__)

    def initialize(self) -> None:
        self.repository.initialize(self.config.units)

    def run_cycle(self, force: bool = False) -> CycleSummary:
        self.initialize()
        summary = CycleSummary()
        for unit in self.config.units:
            if not unit.enabled:
                continue
            if not force and not self.repository.is_due(unit.code):
                continue
            summary.units_processed += 1
            self._collect_unit(unit, summary)
        return summary

    def _collect_unit(self, unit: UnitConfig, summary: CycleSummary) -> None:
        run_id = self.repository.start_run(unit.code)
        requested_batches = 0
        received_documents = 0
        stored_documents = 0
        result = "SUCCESS"
        error_message = ""
        try:
            for _ in range(self.config.collector.max_batches_per_cycle):
                next_nsu = int(self.repository.cursor(unit.code)["next_nsu"])
                self.logger.info("Consultando unidade=%s nsu=%s", unit.code, next_nsu)
                fetch_result = self.client.fetch_batch(unit, next_nsu)
                requested_batches += 1
                summary.batches_requested += 1

                if not fetch_result.documents:
                    self.repository.mark_idle(
                        unit.code,
                        fetch_result.http_status,
                        self.config.collector.idle_poll_seconds,
                    )
                    result = "IDLE"
                    break

                minimum_nsu = min(document.nsu for document in fetch_result.documents)
                maximum_nsu = max(document.nsu for document in fetch_result.documents)
                if minimum_nsu < next_nsu:
                    raise RuntimeError(
                        f"ADN retornou NSU {minimum_nsu} anterior ao solicitado {next_nsu}."
                    )

                batch_stored = self.repository.persist_batch(
                    unit.code,
                    fetch_result.documents,
                    maximum_nsu + 1,
                    fetch_result.http_status,
                )
                received_documents += len(fetch_result.documents)
                stored_documents += batch_stored
                summary.documents_received += len(fetch_result.documents)
                summary.documents_stored += batch_stored
            else:
                result = "PARTIAL"

            if self.config.adn.download_danfse_pdf:
                summary.danfse_pdfs_stored += self._download_pending_danfse(unit)
        except Exception as exc:
            summary.errors += 1
            result = "ERROR"
            error_message = str(exc)
            delay = self.repository.mark_error(
                unit.code,
                error_message,
                self.config.collector.error_backoff_seconds,
                self.config.collector.max_error_backoff_seconds,
            )
            self.logger.exception(
                "Falha ao coletar unidade=%s; nova tentativa em %ss", unit.code, delay
            )
        finally:
            self.repository.finish_run(
                run_id,
                result,
                requested_batches,
                received_documents,
                stored_documents,
                error_message,
            )

    def _download_pending_danfse(self, unit: UnitConfig) -> int:
        stored = 0
        for pending in self.repository.pending_danfse(unit.code):
            invoice_id = int(pending["invoice_id"])
            access_key = str(pending["access_key"])
            try:
                result = self.client.fetch_danfse(unit, access_key)
                self.repository.save_danfse(invoice_id, result.status, result.pdf_bytes)
                if result.pdf_bytes:
                    stored += 1
                    self.logger.info(
                        "DANFSe armazenado unidade=%s chave=%s", unit.code, access_key
                    )
                else:
                    self.logger.warning(
                        "DANFSe indisponivel unidade=%s chave=%s status=%s",
                        unit.code,
                        access_key,
                        result.status,
                    )
            except Exception as exc:
                self.logger.warning(
                    "Falha acessoria ao baixar DANFSe unidade=%s chave=%s: %s",
                    unit.code,
                    access_key,
                    exc,
                )
        return stored

    def run_forever(self) -> None:
        self.logger.info("Coletor NFS-e iniciado em modo continuo")
        while True:
            summary = self.run_cycle()
            self.logger.info("Ciclo finalizado: %s", summary.as_dict())
            time.sleep(self.config.collector.cycle_interval_seconds)
