from __future__ import annotations

import json
import gzip
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from taxlink_nfse.config import UnitConfig
from taxlink_nfse.domain import DecodedDfe


SCHEMA_VERSION = 3


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

            CREATE TABLE IF NOT EXISTS collection_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                result TEXT NOT NULL,
                requested_batches INTEGER NOT NULL DEFAULT 0,
                received_documents INTEGER NOT NULL DEFAULT 0,
                stored_documents INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                FOREIGN KEY(unit_id) REFERENCES fiscal_unit(id)
            );

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

            """
        )
        self._migrate_schema(connection)
        connection.executescript(
            """
            DROP VIEW IF EXISTS vw_invoice_outbox;
            DROP VIEW IF EXISTS vw_notas_fiscais;

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
        certificate_reference = (
            unit.certificate.thumbprint
            if unit.certificate.provider == "windows"
            else str(unit.certificate.pfx_path or "")
        )
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
            return unit_id

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

    def start_run(self, unit_code: str) -> int:
        with self.transaction(immediate=True) as connection:
            unit_id = self.unit_id(unit_code, connection)
            cursor = connection.execute(
                """
                INSERT INTO collection_run(unit_id, started_at, result)
                VALUES (?, ?, 'RUNNING')
                """,
                (unit_id, iso_utc()),
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
    ) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE collection_run
                SET finished_at = ?, result = ?, requested_batches = ?,
                    received_documents = ?, stored_documents = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    iso_utc(),
                    result,
                    requested_batches,
                    received_documents,
                    stored_documents,
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
    ) -> int:
        document_list = tuple(documents)
        now = iso_utc()
        stored = 0
        with self.transaction(immediate=True) as connection:
            unit_id = self.unit_id(unit_code, connection)
            for document in document_list:
                artifact_id, inserted = self._insert_artifact(connection, unit_id, document, now)
                if not inserted:
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
                    (SELECT COUNT(*) FROM integration_outbox o) AS outbox_entries
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

    def dump_status_json(self) -> str:
        return json.dumps(self.status(), ensure_ascii=False, indent=2)
