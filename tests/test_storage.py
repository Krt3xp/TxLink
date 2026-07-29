from __future__ import annotations

import base64
import gzip
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from taxlink_nfse.config import AppConfig
from taxlink_nfse.parser import DfeDecoder
from taxlink_nfse.storage import RepositoryError, SqliteRepository
from tests.test_parser import NFSE_XML


def write_test_config(root: Path, enabled: bool = True) -> AppConfig:
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[collector]
database_path = "data/test.sqlite3"
log_path = "logs/test.log"
cycle_interval_seconds = 1
idle_poll_seconds = 60
error_backoff_seconds = 1
max_error_backoff_seconds = 4
max_batches_per_cycle = 3
request_timeout_seconds = 5
request_attempts = 1

[adn]
batch_mode = true

[[units]]
code = "u1"
system_unit_id = 7
tax_id = "05029600000368"
name = "Unidade Teste"
environment = "restricted"
initial_nsu = 0
enabled = {str(enabled).lower()}

[units.certificate]
provider = "windows"
thumbprint = "AABBCCDD"
store_location = "LocalMachine"
certificate_tax_id = "05029600000104"
""".strip(),
        encoding="utf-8",
    )
    return AppConfig.load(config_path)


def sample_document(nsu: int = 0):
    return DfeDecoder().decode(
        {
            "NSU": nsu,
            "ChaveAcesso": "330455705029600000368000000000000000000000001",
            "TipoDocumento": "NFSE",
            "ArquivoXml": base64.b64encode(gzip.compress(NFSE_XML)).decode("ascii"),
        }
    )


def sample_event(xml_bytes: bytes, nsu: int):
    return DfeDecoder().decode(
        {
            "NSU": nsu,
            "ChaveAcesso": "330455705029600000368000000000000000000000001",
            "TipoDocumento": "EVENTO",
            "ArquivoXml": base64.b64encode(gzip.compress(xml_bytes)).decode("ascii"),
        }
    )


class StorageTests(unittest.TestCase):
    def test_batch_is_idempotent_and_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_test_config(root)
            repository = SqliteRepository(config.collector.database_path)
            repository.initialize(config.units)
            document = sample_document(10)

            stored = repository.persist_batch("u1", [document], 11, 200)
            stored_again = repository.persist_batch("u1", [document], 11, 200)

            self.assertEqual(stored, 1)
            self.assertEqual(stored_again, 0)
            self.assertEqual(repository.cursor("u1")["next_nsu"], 11)
            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM invoice").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM integration_outbox").fetchone()[0], 1
                )
                view_row = connection.execute(
                    "SELECT system_unit_id, access_key, nsu FROM vw_invoice_outbox"
                ).fetchone()
                self.assertEqual(view_row[0], 7)
                self.assertEqual(view_row[1], document.access_key)
                self.assertEqual(view_row[2], 10)
                blob = connection.execute("SELECT xml_gzip FROM dfe_artifact").fetchone()[0]
                self.assertEqual(gzip.decompress(blob), NFSE_XML)
                consolidated = connection.execute(
                    'SELECT "ID", "Unidade CNPJ", "Fornecedor CNPJ", "XML" '
                    'FROM vw_notas_fiscais'
                ).fetchone()
                self.assertEqual(consolidated[0], 1)
                self.assertEqual(consolidated[1], "05029600000368")
                self.assertEqual(consolidated[2], "28524508000108")
                self.assertEqual(consolidated[3], NFSE_XML)

            repository.link_contract(1, 99, "CTS-99")
            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                row = connection.execute(
                    'SELECT "Contrato", "Contrato ID", "XML" '
                    'FROM vw_notas_fiscais'
                ).fetchone()
                self.assertEqual(row, ("CTS-99", 99, NFSE_XML))

    def test_initialize_removes_legacy_pdf_columns_without_losing_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_test_config(root)
            repository = SqliteRepository(config.collector.database_path)
            repository.initialize(config.units)
            repository.persist_batch("u1", [sample_document(10)], 11, 200)
            job_id = repository.create_collection_job("TEST", "u1")

            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                connection.execute("ALTER TABLE invoice ADD COLUMN danfse_pdf BLOB")
                connection.execute(
                    "ALTER TABLE invoice ADD COLUMN danfse_pdf_sha256 TEXT"
                )
                connection.execute(
                    "ALTER TABLE invoice ADD COLUMN danfse_pdf_status TEXT"
                )
                connection.execute(
                    "ALTER TABLE invoice ADD COLUMN danfse_pdf_received_at TEXT"
                )
                connection.execute(
                    "ALTER TABLE collection_job ADD COLUMN danfse_pdfs_stored INTEGER"
                )
                connection.execute(
                    "UPDATE invoice SET danfse_pdf = ?, danfse_pdf_status = ?",
                    (b"%PDF-legacy", "BAIXADO_OFICIAL"),
                )
                connection.execute(
                    "UPDATE collection_job SET danfse_pdfs_stored = 1 WHERE id = ?",
                    (job_id,),
                )
                connection.commit()

            repository.initialize(config.units)

            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                invoice_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(invoice)")
                }
                job_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(collection_job)")
                }
                invoice = connection.execute(
                    "SELECT id, access_key, contract_id FROM invoice"
                ).fetchone()
                job = connection.execute(
                    "SELECT id, status FROM collection_job WHERE id = ?",
                    (job_id,),
                ).fetchone()

            self.assertFalse(any("pdf" in column.lower() for column in invoice_columns))
            self.assertNotIn("danfse_pdfs_stored", job_columns)
            self.assertEqual(
                invoice,
                (1, "330455705029600000368000000000000000000000001", None),
            )
            self.assertEqual(job, (job_id, "QUEUED"))

    def test_same_nsu_with_different_xml_does_not_advance_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_test_config(root)
            repository = SqliteRepository(config.collector.database_path)
            repository.initialize(config.units)
            document = sample_document(5)
            repository.persist_batch("u1", [document], 6, 200)
            changed = replace(document, xml_sha256="0" * 64)

            with self.assertRaises(RepositoryError):
                repository.persist_batch("u1", [changed], 99, 200)

            self.assertEqual(repository.cursor("u1")["next_nsu"], 6)

    def test_national_cancellation_event_updates_invoice_and_outbox(self) -> None:
        cancellation_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<evento xmlns="http://www.sped.fazenda.gov.br/nfse">
  <infEvento>
    <nSeqEvento>1</nSeqEvento>
    <pedRegEvento>
      <infPedReg>
        <dhEvento>2026-07-28T09:59:24-03:00</dhEvento>
        <chNFSe>330455705029600000368000000000000000000000001</chNFSe>
        <e101101>
          <xDesc>Cancelamento de NFS-e</xDesc>
          <xMotivo>Erro na emissao</xMotivo>
        </e101101>
      </infPedReg>
    </pedRegEvento>
  </infEvento>
</evento>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_test_config(root)
            repository = SqliteRepository(config.collector.database_path)
            repository.initialize(config.units)
            repository.persist_batch("u1", [sample_document(10)], 11, 200)
            repository.persist_batch(
                "u1",
                [sample_event(cancellation_xml, 11)],
                12,
                200,
            )

            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                status = connection.execute("SELECT fiscal_status FROM invoice").fetchone()[0]
                outbox_count = connection.execute(
                    "SELECT COUNT(*) FROM integration_outbox"
                ).fetchone()[0]
                event_type = connection.execute(
                    "SELECT event_type FROM fiscal_event"
                ).fetchone()[0]
                event_code = connection.execute(
                    "SELECT event_code FROM fiscal_event"
                ).fetchone()[0]
                outbox_event_code = connection.execute(
                    "SELECT event_code FROM vw_invoice_outbox ORDER BY outbox_id DESC LIMIT 1"
                ).fetchone()[0]

            self.assertEqual(status, "CANCELADA")
            self.assertEqual(event_type, "CANCELAMENTO")
            self.assertEqual(event_code, "101101")
            self.assertEqual(outbox_event_code, "101101")
            self.assertEqual(outbox_count, 2)

    def test_initialize_reconciles_previously_generic_fiscal_event(self) -> None:
        cancellation_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<evento xmlns="http://www.sped.fazenda.gov.br/nfse">
  <infEvento>
    <nSeqEvento>1</nSeqEvento>
    <pedRegEvento>
      <infPedReg>
        <dhEvento>2026-07-28T09:59:24-03:00</dhEvento>
        <chNFSe>330455705029600000368000000000000000000000001</chNFSe>
        <e101101><xDesc>Cancelamento de NFS-e</xDesc></e101101>
      </infPedReg>
    </pedRegEvento>
  </infEvento>
</evento>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_test_config(root)
            repository = SqliteRepository(config.collector.database_path)
            repository.initialize(config.units)
            repository.persist_batch("u1", [sample_document(10)], 11, 200)

            parsed_event = sample_event(cancellation_xml, 11)
            assert parsed_event.fiscal_event is not None
            generic_event = replace(
                parsed_event,
                fiscal_event=replace(parsed_event.fiscal_event, event_type="EVENTO"),
            )
            repository.persist_batch("u1", [generic_event], 12, 200)

            repository.initialize(config.units)

            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                status = connection.execute("SELECT fiscal_status FROM invoice").fetchone()[0]
                event_type = connection.execute(
                    "SELECT event_type FROM fiscal_event"
                ).fetchone()[0]
                event_code = connection.execute(
                    "SELECT event_code FROM fiscal_event"
                ).fetchone()[0]
            self.assertEqual(status, "CANCELADA")
            self.assertEqual(event_type, "CANCELAMENTO")
            self.assertEqual(event_code, "101101")

    def test_rewind_cursor_prepares_historical_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_test_config(root)
            repository = SqliteRepository(config.collector.database_path)
            repository.initialize(config.units)
            repository.persist_batch("u1", [sample_document(10)], 11, 200)

            repository.rewind_cursor("u1", 1)

            cursor = repository.cursor("u1")
            self.assertEqual(cursor["next_nsu"], 1)
            self.assertIsNone(cursor["last_error"])

    def test_backfill_target_survives_an_interrupted_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_test_config(root)
            repository = SqliteRepository(config.collector.database_path)
            repository.initialize(config.units)
            repository.persist_batch("u1", [sample_document(10)], 11, 200)

            first_target = repository.prepare_backfill("u1", 1)
            repository.rewind_cursor("u1", 5)
            retry_target = repository.prepare_backfill("u1", 1)

            self.assertEqual(first_target, 11)
            self.assertEqual(retry_target, 11)
            self.assertEqual(repository.cursor("u1")["history_target_nsu"], 11)

            repository.rewind_cursor("u1", 11)
            repository.complete_backfill("u1")
            cursor = repository.cursor("u1")
            self.assertIsNone(cursor["history_target_nsu"])
            self.assertIsNotNone(cursor["history_backfilled_at"])


if __name__ == "__main__":
    unittest.main()
