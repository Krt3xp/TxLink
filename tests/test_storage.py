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

            pending = repository.pending_danfse("u1")
            self.assertEqual(len(pending), 1)
            repository.save_danfse(1, "BAIXADO_OFICIAL", b"%PDF-test")
            repository.link_contract(1, 99, "CTS-99")
            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                row = connection.execute(
                    'SELECT "Contrato", "Contrato ID", "DANFe PDF" '
                    'FROM vw_notas_fiscais'
                ).fetchone()
                self.assertEqual(row, ("CTS-99", 99, b"%PDF-test"))

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
