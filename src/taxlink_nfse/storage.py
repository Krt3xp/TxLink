from __future__ import annotations

import json
import gzip
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from taxlink_nfse.config import CertificateConfig, UnitConfig
from taxlink_nfse.certificates import inspect_certificate
from taxlink_nfse.domain import DecodedDfe


SCHEMA_VERSION = 4


class RepositoryError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


class SqliteRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def initialize(self, units: Iterable[UnitConfig]) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            self._create_schema(connection)
        for unit in units:
            self.register_unit(unit)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fiscal_unit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                system_unit_id INTEGER,
                tax_id TEXT NOT NULL,
                name TEXT NOT NULL,
                environment TEXT NOT NULL,
                certificate_provider TEXT NOT NULL,
                certificate_reference TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tax_id, environment)
            );

            CREATE TABLE IF NOT EXISTS digital_certificate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id INTEGER NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                certificate_path TEXT,
                private_key_path TEXT,
                password_env TEXT,
                thumbprint TEXT,
                store_location TEXT,
                certificate_tax_id TEXT,
                valid_from TEXT,
                valid_until TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(unit_id) REFERENCES fiscal_unit(id)
            );

            CREATE TABLE IF NOT EXISTS distribution_cursor (
                unit_id INTEGER PRIMARY KEY,
                next_nsu INTEGER NOT NULL DEFAULT 0,
                last_processed_nsu INTEGER,
                last_http_status INTEGER,
                consecutive_errors INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_success_at TEXT,
                history_target_nsu INTEGER,
                history_backfilled_at TEXT,
                next_poll_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(unit_id) REFERENCES fiscal_unit(id)
            );

            CREATE TABLE IF NOT EXISTS collection_job (
                id TEXT PRIMARY KEY,
                trigger_source TEXT NOT NULL,
                requested_unit_code TEXT,
                requested_by TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                units_processed INTEGER NOT NULL DEFAULT 0,
                requested_batches INTEGER NOT NULL DEFAULT 0,
                received_documents INTEGER NOT NULL DEFAULT 0,
                stored_documents INTEGER NOT NULL DEFAULT 0,
                ignored_documents INTEGER NOT NULL DEFAULT 0,
                danfse_pdfs_stored INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_collection_job_status
                ON collection_job(status, created_at);

            CREATE TABLE IF NOT EXISTS collection_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                unit_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                result TEXT NOT NULL,
                requested_batches INTEGER NOT NULL DEFAULT 0,
                received_documents INTEGER NOT NULL DEFAULT 0,
                stored_documents INTEGER NOT NULL DEFAULT 0,
                ignored_documents INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                FOREIGN KEY(job_id) REFERENCES collection_job(id),
                FOREIGN KEY(unit_id) REFERENCES fiscal_unit(id)
            );

            CREATE TABLE IF NOT EXISTS collection_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                unit_id INTEGER NOT NULL,
                nsu INTEGER,
                access_key TEXT,
                event_type TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES collection_run(id),
                FOREIGN KEY(unit_id) REFERENCES fiscal_unit(id)
            );

            CREATE INDEX IF NOT EXISTS idx_collection_event_run
                ON collection_event(run_id, id);

            CREATE TABLE IF NOT EXISTS dfe_artifact (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id INTEGER NOT NULL,
                nsu INTEGER NOT NULL,
                access_key TEXT,
                schema_name TEXT,
                document_type TEXT NOT NULL,
                generated_at TEXT,
                xml_gzip BLOB NOT NULL,
                xml_content BLOB,
                xml_sha256 TEXT NOT NULL,
                received_at TEXT NOT NULL,
                FOREIGN KEY(unit_id) REFERENCES fiscal_unit(id),
                UNIQUE(unit_id, nsu)
            );

            CREATE INDEX IF NOT EXISTS idx_dfe_artifact_access_key
                ON dfe_artifact(unit_id, access_key);

            CREATE TABLE IF NOT EXISTS invoice (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id INTEGER NOT NULL,
                source_artifact_id INTEGER NOT NULL,
                access_key TEXT NOT NULL,
                document_number TEXT,
                series TEXT,
                issued_at TEXT,
                competence_date TEXT,
                provider_tax_id TEXT,
                provider_name TEXT,
                taker_tax_id TEXT,
                taker_name TEXT,
                service_code TEXT,
                service_description TEXT,
                service_amount_cents INTEGER,
                net_amount_cents INTEGER,
                fiscal_status TEXT NOT NULL,
                contract_id INTEGER,
                contract_number TEXT,
                danfse_pdf BLOB,
                danfse_pdf_sha256 TEXT,
                danfse_pdf_status TEXT NOT NULL DEFAULT 'PENDENTE',
                danfse_pdf_received_at TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(unit_id) REFERENCES fiscal_unit(id),
                FOREIGN KEY(source_artifact_id) REFERENCES dfe_artifact(id),
                UNIQUE(unit_id, access_key)
            );

            CREATE INDEX IF NOT EXISTS idx_invoice_provider
                ON invoice(unit_id, provider_tax_id);
            CREATE INDEX IF NOT EXISTS idx_invoice_taker
                ON invoice(unit_id, taker_tax_id);
            CREATE INDEX IF NOT EXISTS idx_invoice_issued_at
                ON invoice(unit_id, issued_at);

            CREATE TABLE IF NOT EXISTS invoice_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                item_number INTEGER NOT NULL,
                code TEXT,
                description TEXT,
                quantity TEXT,
                total_amount_cents INTEGER,
                FOREIGN KEY(invoice_id) REFERENCES invoice(id) ON DELETE CASCADE,
                UNIQUE(invoice_id, item_number)
            );

            CREATE TABLE IF NOT EXISTS fiscal_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id INTEGER NOT NULL,
                source_artifact_id INTEGER NOT NULL,
                invoice_access_key TEXT,
                event_key TEXT,
                event_type TEXT,
                event_sequence INTEGER,
                occurred_at TEXT,
                protocol TEXT,
                status TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(unit_id) REFERENCES fiscal_unit(id),
                FOREIGN KEY(source_artifact_id) REFERENCES dfe_artifact(id),
                UNIQUE(unit_id, event_key)
            );

            CREATE TABLE IF NOT EXISTS integration_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aggregate_type TEXT NOT NULL,
                aggregate_id INTEGER NOT NULL,
                operation TEXT NOT NULL,
                aggregate_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(aggregate_type, aggregate_id, operation, aggregate_version)
            );

            CREATE TABLE IF NOT EXISTS sync_run (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                trigger_source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                local_path TEXT,
                remote_path TEXT,
                size_bytes INTEGER,
                sha256 TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                next_attempt_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_sync_run_status
                ON sync_run(status, created_at);

            """
        )
        self._migrate_schema(connection)
        connection.executescript(
            """
            DROP VIEW IF EXISTS vw_invoice_outbox;
            DROP VIEW IF EXISTS vw_notas_fiscais;
            DROP VIEW IF EXISTS vw_certificados_digitais;

            CREATE VIEW vw_invoice_outbox AS
            SELECT
                o.id AS outbox_id,
                o.operation,
                o.aggregate_version,
                o.created_at AS outbox_created_at,
                u.code AS unit_code,
                u.system_unit_id,
                u.tax_id AS unit_tax_id,
                i.id AS invoice_id,
                i.access_key,
                i.document_number,
                i.series,
                i.issued_at,
                i.competence_date,
                i.provider_tax_id,
                i.provider_name,
                i.taker_tax_id,
                i.taker_name,
                i.service_code,
                i.service_description,
                i.service_amount_cents,
                i.net_amount_cents,
                i.fiscal_status,
                i.contract_id,
                i.contract_number,
                i.danfse_pdf_status,
                i.danfse_pdf_sha256,
                i.danfse_pdf_received_at,
                a.id AS artifact_id,
                a.nsu,
                a.document_type,
                a.schema_name,
                a.xml_sha256
            FROM integration_outbox o
            JOIN invoice i
              ON o.aggregate_type = 'INVOICE' AND i.id = o.aggregate_id
            JOIN fiscal_unit u ON u.id = i.unit_id
            JOIN dfe_artifact a ON a.id = i.source_artifact_id;

            CREATE VIEW vw_notas_fiscais AS
            SELECT
                i.contract_number AS "Contrato",
                i.id AS "ID",
                i.contract_id AS "Contrato ID",
                u.tax_id AS "Unidade CNPJ",
                i.provider_tax_id AS "Fornecedor CNPJ",
                i.issued_at AS "Data de Emissao",
                CASE
                    WHEN i.service_amount_cents IS NULL THEN NULL
                    ELSE i.service_amount_cents / 100.0
                END AS "Valor",
                i.competence_date AS "Competencia",
                a.xml_content AS "XML",
                i.danfse_pdf AS "DANFe PDF",
                i.danfse_pdf_status AS "Status DANFe PDF",
                i.access_key AS "Chave de Acesso",
                a.nsu AS "NSU"
            FROM invoice i
            JOIN fiscal_unit u ON u.id = i.unit_id
            JOIN dfe_artifact a ON a.id = i.source_artifact_id;

            CREATE VIEW vw_certificados_digitais AS
            SELECT
                c.id AS "ID",
                u.code AS "Unidade",
                u.name AS "Nome da Unidade",
                u.tax_id AS "CNPJ da Unidade",
                upper(c.provider) AS "Formato",
                c.certificate_path AS "Arquivo no Servidor",
                c.private_key_path AS "Chave PEM no Servidor",
                c.password_env AS "Variavel da Senha",
                c.thumbprint AS "Thumbprint",
                c.certificate_tax_id AS "CNPJ do Certificado",
                c.valid_from AS "Valido Desde",
                c.valid_until AS "Valido Ate",
                CASE
                    WHEN c.enabled = 0 THEN 'INATIVO'
                    WHEN c.valid_until IS NULL THEN 'VALIDADE NAO EXTRAIDA'
                    WHEN datetime(c.valid_until) < datetime('now') THEN 'VENCIDO'
                    ELSE 'VALIDO'
                END AS "Situacao"
            FROM digital_certificate c
            JOIN fiscal_unit u ON u.id = c.unit_id;
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, iso_utc()),
        )
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if int(version) != SCHEMA_VERSION:
            raise RepositoryError(
                f"Versao do banco {version} incompativel com a aplicacao {SCHEMA_VERSION}."
            )
        connection.commit()

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        self._add_column(connection, "dfe_artifact", "xml_content", "BLOB")
        self._add_column(connection, "invoice", "contract_id", "INTEGER")
        self._add_column(connection, "invoice", "contract_number", "TEXT")
        self._add_column(connection, "invoice", "danfse_pdf", "BLOB")
        self._add_column(connection, "invoice", "danfse_pdf_sha256", "TEXT")
        self._add_column(
            connection,
            "invoice",
            "danfse_pdf_status",
            "TEXT NOT NULL DEFAULT 'PENDENTE'",
        )
        self._add_column(connection, "invoice", "danfse_pdf_received_at", "TEXT")
        self._add_column(connection, "distribution_cursor", "history_target_nsu", "INTEGER")
        self._add_column(connection, "distribution_cursor", "history_backfilled_at", "TEXT")
        self._add_column(connection, "collection_run", "job_id", "TEXT")
        self._add_column(
            connection, "collection_run", "ignored_documents", "INTEGER NOT NULL DEFAULT 0"
        )

        rows = connection.execute(
            "SELECT id, xml_gzip FROM dfe_artifact WHERE xml_content IS NULL"
        ).fetchall()
        for row in rows:
            try:
                xml_content = gzip.decompress(bytes(row["xml_gzip"]))
            except (OSError, EOFError) as exc:
                raise RepositoryError(
                    f"Nao foi possivel descompactar o XML do artefato {row['id']}."
                ) from exc
            connection.execute(
                "UPDATE dfe_artifact SET xml_content = ? WHERE id = ?",
                (xml_content, int(row["id"])),
            )

    @staticmethod
    def _add_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def register_unit(self, unit: UnitConfig) -> int:
        now = iso_utc()
        certificate_metadata = inspect_certificate(unit.certificate)
        if unit.certificate.provider == "windows":
            certificate_reference = unit.certificate.thumbprint
        elif unit.certificate.provider == "pfx":
            certificate_reference = str(unit.certificate.pfx_path or "")
        else:
            certificate_reference = str(unit.certificate.pem_cert_path or "")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO fiscal_unit (
                    code, system_unit_id, tax_id, name, environment,
                    certificate_provider, certificate_reference, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    system_unit_id = excluded.system_unit_id,
                    tax_id = excluded.tax_id,
                    name = excluded.name,
                    environment = excluded.environment,
                    certificate_provider = excluded.certificate_provider,
                    certificate_reference = excluded.certificate_reference,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    unit.code,
                    unit.system_unit_id,
                    unit.tax_id,
                    unit.name,
                    unit.environment,
                    unit.certificate.provider,
                    certificate_reference,
                    int(unit.enabled),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM fiscal_unit WHERE code = ?", (unit.code,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"Nao foi possivel cadastrar a unidade {unit.code}.")
            unit_id = int(row["id"])
            connection.execute(
                """
                INSERT OR IGNORE INTO distribution_cursor (
                    unit_id, next_nsu, next_poll_at, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (unit_id, unit.initial_nsu, now, now),
            )
            certificate = unit.certificate
            certificate_path = (
                str(certificate.pfx_path or "")
                if certificate.provider == "pfx"
                else str(certificate.pem_cert_path or "")
            )
            private_key_path = (
                str(certificate.pem_key_path or "")
                if certificate.provider == "pem"
                else ""
            )
            connection.execute(
                """
                INSERT INTO digital_certificate (
                    unit_id, provider, certificate_path, private_key_path,
                    password_env, thumbprint, store_location, certificate_tax_id,
                    valid_from, valid_until, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(unit_id) DO UPDATE SET
                    provider = excluded.provider,
                    certificate_path = excluded.certificate_path,
                    private_key_path = excluded.private_key_path,
                    password_env = excluded.password_env,
                    thumbprint = COALESCE(excluded.thumbprint, digital_certificate.thumbprint),
                    store_location = excluded.store_location,
                    certificate_tax_id = excluded.certificate_tax_id,
                    valid_from = COALESCE(excluded.valid_from, digital_certificate.valid_from),
                    valid_until = COALESCE(excluded.valid_until, digital_certificate.valid_until),
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    unit_id,
                    certificate.provider,
                    certificate_path or None,
                    private_key_path or None,
                    certificate.password_env or None,
                    (
                        certificate_metadata.thumbprint
                        if certificate_metadata is not None
                        else certificate.thumbprint or None
                    ),
                    (
                        certificate.store_location
                        if certificate.provider == "windows"
                        else None
                    ),
                    certificate.certificate_tax_id or None,
                    (
                        certificate_metadata.valid_from
                        if certificate_metadata is not None
                        else None
                    ),
                    (
                        certificate_metadata.valid_until
                        if certificate_metadata is not None
                        else None
                    ),
                    now,
                    now,
                ),
            )
            return unit_id

    def certificate_for_unit(self, unit_code: str) -> CertificateConfig:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT c.*
                FROM digital_certificate c
                JOIN fiscal_unit u ON u.id = c.unit_id
                WHERE u.code = ? AND c.enabled = 1
                """,
                (unit_code,),
            ).fetchone()
        if row is None:
            raise RepositoryError(f"Certificado ativo nao encontrado para {unit_code}.")
        provider = str(row["provider"])
        certificate_path = str(row["certificate_path"] or "")
        certificate = CertificateConfig(
            provider=provider,
            thumbprint=str(row["thumbprint"] or ""),
            store_location=str(row["store_location"] or "Auto"),
            pfx_path=Path(certificate_path) if provider == "pfx" and certificate_path else None,
            pem_cert_path=(
                Path(certificate_path) if provider == "pem" and certificate_path else None
            ),
            pem_key_path=(
                Path(str(row["private_key_path"]))
                if provider == "pem" and row["private_key_path"]
                else None
            ),
            password_env=str(row["password_env"] or ""),
            certificate_tax_id=str(row["certificate_tax_id"] or ""),
        )
        certificate.validate()
        return certificate

    def certificates(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT u.code AS unit_code, c.provider, c.certificate_path,
                       c.private_key_path, c.password_env, c.thumbprint,
                       c.store_location, c.certificate_tax_id, c.valid_from,
                       c.valid_until, c.enabled
                FROM digital_certificate c
                JOIN fiscal_unit u ON u.id = c.unit_id
                ORDER BY u.code
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def unit_id(self, code: str, connection: sqlite3.Connection | None = None) -> int:
        owns_connection = connection is None
        active_connection = connection or self._connect()
        try:
            row = active_connection.execute(
                "SELECT id FROM fiscal_unit WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"Unidade nao cadastrada: {code}")
            return int(row["id"])
        finally:
            if owns_connection:
                active_connection.close()

    def cursor(self, unit_code: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT c.*
                FROM distribution_cursor c
                JOIN fiscal_unit u ON u.id = c.unit_id
                WHERE u.code = ?
                """,
                (unit_code,),
            ).fetchone()
            if row is None:
                raise RepositoryError(f"Cursor nao encontrado para {unit_code}.")
            return dict(row)

    def rewind_cursor(self, unit_code: str, nsu: int) -> None:
        if nsu < 0:
            raise ValueError("O NSU inicial nao pode ser negativo.")
        now = iso_utc()
        with self.transaction(immediate=True) as connection:
            unit_id = self.unit_id(unit_code, connection)
            connection.execute(
                """
                UPDATE distribution_cursor
                SET next_nsu = ?, next_poll_at = ?, consecutive_errors = 0,
                    last_error = NULL, updated_at = ?
                WHERE unit_id = ?
                """,
                (nsu, now, now, unit_id),
            )

    def prepare_backfill(self, unit_code: str, from_nsu: int) -> int:
        if from_nsu < 0:
            raise ValueError("O NSU inicial nao pode ser negativo.")
        now = iso_utc()
        with self.transaction(immediate=True) as connection:
            unit_id = self.unit_id(unit_code, connection)
            row = connection.execute(
                """
                SELECT next_nsu, history_target_nsu
                FROM distribution_cursor WHERE unit_id = ?
                """,
                (unit_id,),
            ).fetchone()
            if row is None:
                raise RepositoryError(f"Cursor nao encontrado para {unit_code}.")
            target_nsu = max(
                int(row["next_nsu"]), int(row["history_target_nsu"] or 0)
            )
            if from_nsu < target_nsu:
                connection.execute(
                    """
                    UPDATE distribution_cursor
                    SET next_nsu = ?, history_target_nsu = ?,
                        history_backfilled_at = NULL, next_poll_at = ?,
                        consecutive_errors = 0, last_error = NULL, updated_at = ?
                    WHERE unit_id = ?
                    """,
                    (from_nsu, target_nsu, now, now, unit_id),
                )
            return target_nsu

    def complete_backfill(self, unit_code: str) -> None:
        now = iso_utc()
        with self.transaction(immediate=True) as connection:
            unit_id = self.unit_id(unit_code, connection)
            connection.execute(
                """
                UPDATE distribution_cursor
                SET history_target_nsu = NULL, history_backfilled_at = ?, updated_at = ?
                WHERE unit_id = ?
                """,
                (now, now, unit_id),
            )

    def is_due(self, unit_code: str, now: datetime | None = None) -> bool:
        next_poll = str(self.cursor(unit_code)["next_poll_at"])
        return datetime.fromisoformat(next_poll) <= (now or utc_now())

    def create_collection_job(
        self,
        trigger_source: str,
        unit_code: str | None = None,
        requested_by: str = "",
    ) -> str:
        job_id = str(uuid.uuid4())
        now = iso_utc()
        with self.transaction(immediate=True) as connection:
            if unit_code:
                self.unit_id(unit_code, connection)
            connection.execute(
                """
                INSERT INTO collection_job (
                    id, trigger_source, requested_unit_code, requested_by,
                    status, created_at
                ) VALUES (?, ?, ?, ?, 'QUEUED', ?)
                """,
                (job_id, trigger_source.upper(), unit_code or None, requested_by or None, now),
            )
        return job_id

    def recover_interrupted_work(self) -> None:
        """Devolve trabalhos interrompidos a fila apos reinicio do servico."""
        now = iso_utc()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE collection_run
                SET result = 'ERROR', finished_at = ?,
                    error_message = COALESCE(error_message, 'Servico reiniciado durante a coleta.')
                WHERE result = 'RUNNING'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE collection_job
                SET status = 'QUEUED', started_at = NULL, finished_at = NULL,
                    error_message = 'Execucao retomada apos reinicio do servico.'
                WHERE status = 'RUNNING'
                """
            )
            connection.execute(
                """
                UPDATE sync_run
                SET status = 'QUEUED', started_at = NULL, finished_at = NULL,
                    error_message = 'Sincronizacao retomada apos reinicio do servico.'
                WHERE status = 'RUNNING'
                """
            )

    def claim_collection_job(self, job_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE collection_job
                SET status = 'RUNNING', started_at = ?, error_message = NULL
                WHERE id = ? AND status = 'QUEUED'
                """,
                (iso_utc(), job_id),
            )
            return cursor.rowcount == 1

    def next_queued_collection_job(self) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM collection_job
                WHERE status = 'QUEUED'
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
            return str(row["id"]) if row else None

    def collection_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM collection_job WHERE id = ?", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    def recent_collection_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM collection_job ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def collection_runs_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT r.*, u.code AS unit_code
                FROM collection_run r
                JOIN fiscal_unit u ON u.id = r.unit_id
                WHERE r.job_id = ?
                ORDER BY r.id
                """,
                (job_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def finish_collection_job(self, job_id: str, summary: dict[str, int]) -> None:
        errors = int(summary.get("errors", 0))
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE collection_job
                SET status = ?, finished_at = ?, units_processed = ?,
                    requested_batches = ?, received_documents = ?,
                    stored_documents = ?, ignored_documents = ?,
                    danfse_pdfs_stored = ?, error_count = ?
                WHERE id = ?
                """,
                (
                    "ERROR" if errors else "SUCCESS",
                    iso_utc(),
                    int(summary.get("units_processed", 0)),
                    int(summary.get("batches_requested", 0)),
                    int(summary.get("documents_received", 0)),
                    int(summary.get("documents_stored", 0)),
                    int(summary.get("documents_ignored", 0)),
                    int(summary.get("danfse_pdfs_stored", 0)),
                    errors,
                    job_id,
                ),
            )

    def fail_collection_job(self, job_id: str, error_message: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE collection_job
                SET status = 'ERROR', finished_at = ?, error_count = error_count + 1,
                    error_message = ?
                WHERE id = ?
                """,
                (iso_utc(), error_message[:2000], job_id),
            )

    def start_run(self, unit_code: str, job_id: str | None = None) -> int:
        with self.transaction(immediate=True) as connection:
            unit_id = self.unit_id(unit_code, connection)
            cursor = connection.execute(
                """
                INSERT INTO collection_run(job_id, unit_id, started_at, result)
                VALUES (?, ?, ?, 'RUNNING')
                """,
                (job_id, unit_id, iso_utc()),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        result: str,
        requested_batches: int,
        received_documents: int,
        stored_documents: int,
        error_message: str = "",
        ignored_documents: int | None = None,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE collection_run
                SET finished_at = ?, result = ?, requested_batches = ?,
                    received_documents = ?, stored_documents = ?,
                    ignored_documents = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    iso_utc(),
                    result,
                    requested_batches,
                    received_documents,
                    stored_documents,
                    (
                        max(0, received_documents - stored_documents)
                        if ignored_documents is None
                        else max(0, ignored_documents)
                    ),
                    error_message[:2000] or None,
                    run_id,
                ),
            )

    def persist_batch(
        self,
        unit_code: str,
        documents: Iterable[DecodedDfe],
        next_nsu: int,
        http_status: int,
        run_id: int | None = None,
    ) -> int:
        document_list = tuple(documents)
        now = iso_utc()
        stored = 0
        with self.transaction(immediate=True) as connection:
            unit_id = self.unit_id(unit_code, connection)
            for document in document_list:
                artifact_id, inserted = self._insert_artifact(connection, unit_id, document, now)
                if not inserted:
                    connection.execute(
                        """
                        INSERT INTO collection_event (
                            run_id, unit_id, nsu, access_key, event_type, message, created_at
                        ) VALUES (?, ?, ?, ?, 'DOCUMENTO_DUPLICADO', ?, ?)
                        """,
                        (
                            run_id,
                            unit_id,
                            document.nsu,
                            document.access_key or None,
                            "Documento ja existente; nenhuma nova gravacao realizada.",
                            now,
                        ),
                    )
                    continue
                stored += 1
                if document.invoice is not None and document.invoice.access_key:
                    self._upsert_invoice(connection, unit_id, artifact_id, document, now)
                else:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO integration_outbox (
                            aggregate_type, aggregate_id, operation, aggregate_version, created_at
                        ) VALUES ('DFE_ARTIFACT', ?, 'UPSERT', 1, ?)
                        """,
                        (artifact_id, now),
                    )

            last_processed = max((document.nsu for document in document_list), default=None)
            connection.execute(
                """
                UPDATE distribution_cursor
                SET next_nsu = ?, last_processed_nsu = COALESCE(?, last_processed_nsu),
                    last_http_status = ?, consecutive_errors = 0, last_error = NULL,
                    last_success_at = ?, next_poll_at = ?, updated_at = ?
                WHERE unit_id = ?
                """,
                (
                    next_nsu,
                    last_processed,
                    http_status,
                    now,
                    now,
                    now,
                    unit_id,
                ),
            )
        return stored

    def _insert_artifact(
        self,
        connection: sqlite3.Connection,
        unit_id: int,
        document: DecodedDfe,
        now: str,
    ) -> tuple[int, bool]:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO dfe_artifact (
                unit_id, nsu, access_key, schema_name, document_type,
                generated_at, xml_gzip, xml_content, xml_sha256, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unit_id,
                document.nsu,
                document.access_key or None,
                document.schema_name or None,
                document.document_type,
                document.generated_at or None,
                document.compressed_xml,
                document.xml_bytes,
                document.xml_sha256,
                now,
            ),
        )
        inserted = cursor.rowcount == 1
        row = connection.execute(
            "SELECT id, xml_sha256 FROM dfe_artifact WHERE unit_id = ? AND nsu = ?",
            (unit_id, document.nsu),
        ).fetchone()
        if row is None:
            raise RepositoryError(f"Falha ao persistir o NSU {document.nsu}.")
        if not inserted and str(row["xml_sha256"]) != document.xml_sha256:
            raise RepositoryError(
                f"O NSU {document.nsu} ja existe com conteudo XML diferente. Cursor preservado."
            )
        return int(row["id"]), inserted

    def _upsert_invoice(
        self,
        connection: sqlite3.Connection,
        unit_id: int,
        artifact_id: int,
        document: DecodedDfe,
        now: str,
    ) -> None:
        invoice = document.invoice
        assert invoice is not None
        existing = connection.execute(
            "SELECT id, version FROM invoice WHERE unit_id = ? AND access_key = ?",
            (unit_id, invoice.access_key),
        ).fetchone()
        if existing is None:
            version = 1
            cursor = connection.execute(
                """
                INSERT INTO invoice (
                    unit_id, source_artifact_id, access_key, document_number, series,
                    issued_at, competence_date, provider_tax_id, provider_name,
                    taker_tax_id, taker_name, service_code, service_description,
                    service_amount_cents, net_amount_cents, fiscal_status,
                    version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    unit_id,
                    artifact_id,
                    invoice.access_key,
                    invoice.document_number or None,
                    invoice.series or None,
                    invoice.issued_at or None,
                    invoice.competence_date or None,
                    invoice.provider_tax_id or None,
                    invoice.provider_name or None,
                    invoice.taker_tax_id or None,
                    invoice.taker_name or None,
                    invoice.service_code or None,
                    invoice.service_description or None,
                    invoice.service_amount_cents,
                    invoice.net_amount_cents,
                    invoice.status,
                    version,
                    now,
                    now,
                ),
            )
            invoice_id = int(cursor.lastrowid)
        else:
            invoice_id = int(existing["id"])
            version = int(existing["version"]) + 1
            connection.execute(
                """
                UPDATE invoice
                SET source_artifact_id = ?, document_number = ?, series = ?, issued_at = ?,
                    competence_date = ?, provider_tax_id = ?, provider_name = ?,
                    taker_tax_id = ?, taker_name = ?, service_code = ?,
                    service_description = ?, service_amount_cents = ?, net_amount_cents = ?,
                    fiscal_status = ?, version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    artifact_id,
                    invoice.document_number or None,
                    invoice.series or None,
                    invoice.issued_at or None,
                    invoice.competence_date or None,
                    invoice.provider_tax_id or None,
                    invoice.provider_name or None,
                    invoice.taker_tax_id or None,
                    invoice.taker_name or None,
                    invoice.service_code or None,
                    invoice.service_description or None,
                    invoice.service_amount_cents,
                    invoice.net_amount_cents,
                    invoice.status,
                    version,
                    now,
                    invoice_id,
                ),
            )

        connection.execute("DELETE FROM invoice_item WHERE invoice_id = ?", (invoice_id,))
        connection.executemany(
            """
            INSERT INTO invoice_item (
                invoice_id, item_number, code, description, quantity, total_amount_cents
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    invoice_id,
                    item.item_number,
                    item.code or None,
                    item.description or None,
                    item.quantity or None,
                    item.total_amount_cents,
                )
                for item in invoice.items
            ),
        )
        connection.execute(
            """
            INSERT INTO integration_outbox (
                aggregate_type, aggregate_id, operation, aggregate_version, created_at
            ) VALUES ('INVOICE', ?, 'UPSERT', ?, ?)
            """,
            (invoice_id, version, now),
        )

    def pending_danfse(self, unit_code: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.id AS invoice_id, i.access_key, i.danfse_pdf_status
                FROM invoice i
                JOIN fiscal_unit u ON u.id = i.unit_id
                WHERE u.code = ? AND i.danfse_pdf IS NULL
                ORDER BY i.id
                LIMIT ?
                """,
                (unit_code, max(1, int(limit))),
            ).fetchall()
            return [dict(row) for row in rows]

    def save_danfse(
        self,
        invoice_id: int,
        status: str,
        pdf_bytes: bytes | None,
    ) -> bool:
        now = iso_utc()
        pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest() if pdf_bytes else None
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT danfse_pdf_sha256, danfse_pdf_status, version
                FROM invoice WHERE id = ?
                """,
                (invoice_id,),
            ).fetchone()
            if existing is None:
                raise RepositoryError(f"NFS-e nao encontrada para salvar DANFSe: {invoice_id}")
            if (
                existing["danfse_pdf_sha256"] == pdf_sha256
                and str(existing["danfse_pdf_status"]) == status
            ):
                return False

            version = int(existing["version"]) + 1
            connection.execute(
                """
                UPDATE invoice
                SET danfse_pdf = COALESCE(?, danfse_pdf),
                    danfse_pdf_sha256 = COALESCE(?, danfse_pdf_sha256),
                    danfse_pdf_status = ?,
                    danfse_pdf_received_at = CASE WHEN ? IS NULL THEN danfse_pdf_received_at ELSE ? END,
                    version = ?, updated_at = ?
                WHERE id = ?
                """,
                (pdf_bytes, pdf_sha256, status, pdf_bytes, now, version, now, invoice_id),
            )
            connection.execute(
                """
                INSERT INTO integration_outbox (
                    aggregate_type, aggregate_id, operation, aggregate_version, created_at
                ) VALUES ('INVOICE', ?, 'UPSERT', ?, ?)
                """,
                (invoice_id, version, now),
            )
            return True

    def link_contract(
        self, invoice_id: int, contract_id: int, contract_number: str
    ) -> bool:
        now = iso_utc()
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT contract_id, contract_number, version FROM invoice WHERE id = ?",
                (invoice_id,),
            ).fetchone()
            if existing is None:
                raise RepositoryError(f"NFS-e nao encontrada para vincular contrato: {invoice_id}")
            if (
                existing["contract_id"] == contract_id
                and str(existing["contract_number"] or "") == contract_number
            ):
                return False
            version = int(existing["version"]) + 1
            connection.execute(
                """
                UPDATE invoice
                SET contract_id = ?, contract_number = ?, version = ?, updated_at = ?
                WHERE id = ?
                """,
                (contract_id, contract_number or None, version, now, invoice_id),
            )
            connection.execute(
                """
                INSERT INTO integration_outbox (
                    aggregate_type, aggregate_id, operation, aggregate_version, created_at
                ) VALUES ('INVOICE', ?, 'UPSERT', ?, ?)
                """,
                (invoice_id, version, now),
            )
            return True

    def mark_idle(self, unit_code: str, http_status: int, delay_seconds: int) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            unit_id = self.unit_id(unit_code, connection)
            connection.execute(
                """
                UPDATE distribution_cursor
                SET last_http_status = ?, consecutive_errors = 0, last_error = NULL,
                    last_success_at = ?, next_poll_at = ?, updated_at = ?
                WHERE unit_id = ?
                """,
                (
                    http_status,
                    iso_utc(now),
                    iso_utc(now + timedelta(seconds=delay_seconds)),
                    iso_utc(now),
                    unit_id,
                ),
            )

    def mark_error(
        self,
        unit_code: str,
        message: str,
        base_delay_seconds: int,
        max_delay_seconds: int,
    ) -> int:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            unit_id = self.unit_id(unit_code, connection)
            row = connection.execute(
                "SELECT consecutive_errors FROM distribution_cursor WHERE unit_id = ?",
                (unit_id,),
            ).fetchone()
            errors = int(row["consecutive_errors"] if row else 0) + 1
            delay = min(max_delay_seconds, base_delay_seconds * (2 ** min(errors - 1, 8)))
            connection.execute(
                """
                UPDATE distribution_cursor
                SET consecutive_errors = ?, last_error = ?, next_poll_at = ?, updated_at = ?
                WHERE unit_id = ?
                """,
                (
                    errors,
                    message[:2000],
                    iso_utc(now + timedelta(seconds=delay)),
                    iso_utc(now),
                    unit_id,
                ),
            )
            return delay

    def create_sync_run(self, trigger_source: str) -> str:
        sync_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO sync_run(id, status, trigger_source, created_at)
                VALUES (?, 'QUEUED', ?, ?)
                """,
                (sync_id, trigger_source.upper(), iso_utc()),
            )
        return sync_id

    def claim_sync_run(self, sync_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE sync_run
                SET status = 'RUNNING', started_at = ?, attempts = attempts + 1,
                    error_message = NULL
                WHERE id = ? AND status = 'QUEUED'
                """,
                (iso_utc(), sync_id),
            )
            return cursor.rowcount == 1

    def next_queued_sync_run(self) -> str | None:
        now = iso_utc()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE sync_run
                SET status = 'QUEUED', finished_at = NULL
                WHERE status = 'ERROR' AND next_attempt_at IS NOT NULL
                  AND next_attempt_at <= ?
                """,
                (now,),
            )
            row = connection.execute(
                """
                SELECT id FROM sync_run
                WHERE status = 'QUEUED'
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
            return str(row["id"]) if row else None

    def finish_sync_run(
        self,
        sync_id: str,
        local_path: str,
        remote_path: str,
        size_bytes: int,
        sha256: str,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE sync_run
                SET status = 'SUCCESS', finished_at = ?, local_path = ?, remote_path = ?,
                    size_bytes = ?, sha256 = ?, next_attempt_at = NULL
                WHERE id = ?
                """,
                (iso_utc(), local_path, remote_path, size_bytes, sha256, sync_id),
            )

    def fail_sync_run(
        self, sync_id: str, error_message: str, retry_delay_seconds: int = 300
    ) -> None:
        next_attempt = iso_utc(utc_now() + timedelta(seconds=retry_delay_seconds))
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE sync_run
                SET status = 'ERROR', finished_at = ?, error_message = ?, next_attempt_at = ?
                WHERE id = ?
                """,
                (iso_utc(), error_message[:2000], next_attempt, sync_id),
            )

    def sync_run(self, sync_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sync_run WHERE id = ?", (sync_id,)
            ).fetchone()
            return dict(row) if row else None

    def recent_sync_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sync_run ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def status(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    u.code, u.system_unit_id, u.tax_id, u.name, u.environment, u.enabled,
                    c.next_nsu, c.last_processed_nsu, c.last_http_status,
                    c.consecutive_errors, c.last_error, c.last_success_at,
                    c.history_target_nsu, c.history_backfilled_at, c.next_poll_at,
                    (SELECT COUNT(*) FROM dfe_artifact a WHERE a.unit_id = u.id) AS artifacts,
                    (SELECT COUNT(*) FROM invoice i WHERE i.unit_id = u.id) AS invoices,
                    (SELECT COUNT(*) FROM invoice i WHERE i.unit_id = u.id
                        AND i.danfse_pdf IS NOT NULL) AS danfse_pdfs,
                    (SELECT COUNT(*) FROM invoice i WHERE i.unit_id = u.id
                        AND i.contract_id IS NOT NULL) AS linked_contracts,
                    (SELECT COUNT(*) FROM integration_outbox o
                        JOIN invoice oi ON o.aggregate_type = 'INVOICE'
                            AND oi.id = o.aggregate_id AND oi.unit_id = u.id) AS outbox_entries
                FROM fiscal_unit u
                JOIN distribution_cursor c ON c.unit_id = u.id
                ORDER BY u.code
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def invoice_summaries(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    i.id, u.code AS unit_code, u.tax_id AS unit_tax_id,
                    i.contract_id, i.contract_number, a.nsu, i.access_key,
                    i.document_number, i.issued_at, i.competence_date,
                    i.provider_tax_id, i.provider_name, i.service_amount_cents,
                    i.fiscal_status, length(a.xml_content) AS xml_bytes,
                    length(i.danfse_pdf) AS pdf_bytes, i.danfse_pdf_status
                FROM invoice i
                JOIN fiscal_unit u ON u.id = i.unit_id
                JOIN dfe_artifact a ON a.id = i.source_artifact_id
                ORDER BY COALESCE(i.issued_at, '') DESC, a.nsu DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    r.id, u.code AS unit_code, r.started_at, r.finished_at,
                    r.result, r.requested_batches, r.received_documents,
                    r.stored_documents, r.error_message
                FROM collection_run r
                JOIN fiscal_unit u ON u.id = r.unit_id
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(row) for row in rows]


    def purge_outbox(self, retention_days: int) -> int:
        """Remove registros do integration_outbox mais antigos que retention_days."""
        cutoff = iso_utc(utc_now() - timedelta(days=retention_days))
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM integration_outbox WHERE created_at < ?",
                (cutoff,),
            )
            return cursor.rowcount

    def dump_status_json(self) -> str:
        return json.dumps(self.status(), ensure_ascii=False, indent=2)
