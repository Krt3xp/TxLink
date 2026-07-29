from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from taxlink_nfse.collector import Collector
from taxlink_nfse.domain import FetchResult
from taxlink_nfse.domain import DanfseResult
from taxlink_nfse.storage import SqliteRepository
from tests.test_storage import sample_document, write_test_config


class FakeAdnClient:
    def __init__(self):
        self.requested: list[int] = []

    def fetch_batch(self, unit, nsu: int) -> FetchResult:
        self.requested.append(nsu)
        if len(self.requested) == 1:
            return FetchResult(nsu, 200, (sample_document(nsu),))
        return FetchResult(nsu, 404, ())

    def fetch_danfse(self, unit, access_key: str) -> DanfseResult:
        return DanfseResult("BAIXADO_OFICIAL", 200, b"%PDF-test")


class MissingOfficialPdfClient(FakeAdnClient):
    def fetch_danfse(self, unit, access_key: str) -> DanfseResult:
        return DanfseResult("NAO_DISPONIVEL_404", 404)


class FakeDanfseGenerator:
    def __init__(self):
        self.requests: list[tuple[bytes, str]] = []

    def generate(self, xml_bytes: bytes, access_key: str = "") -> bytes:
        self.requests.append((xml_bytes, access_key))
        return b"%PDF-generated-from-xml"


class CollectorTests(unittest.TestCase):
    def test_cycle_persists_document_and_stops_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = write_test_config(Path(temp_dir))
            repository = SqliteRepository(config.collector.database_path)
            client = FakeAdnClient()
            collector = Collector(config, repository=repository, client=client)

            summary = collector.run_cycle(force=True)

            self.assertEqual(summary.errors, 0)
            self.assertEqual(summary.documents_stored, 1)
            self.assertEqual(summary.danfse_pdfs_stored, 1)
            self.assertEqual(client.requested, [0, 1])
            self.assertEqual(repository.cursor("u1")["next_nsu"], 1)
            self.assertEqual(repository.status()[0]["invoices"], 1)

    def test_generates_pdf_from_xml_when_official_pdf_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = write_test_config(Path(temp_dir))
            repository = SqliteRepository(config.collector.database_path)
            client = MissingOfficialPdfClient()
            generator = FakeDanfseGenerator()
            collector = Collector(
                config,
                repository=repository,
                client=client,
                danfse_generator=generator,
            )

            summary = collector.run_cycle(force=True)

            self.assertEqual(summary.errors, 0)
            self.assertEqual(summary.danfse_pdfs_stored, 1)
            self.assertEqual(len(generator.requests), 1)
            xml_bytes, access_key = generator.requests[0]
            self.assertTrue(xml_bytes.startswith(b"<?xml"))
            self.assertEqual(
                access_key,
                "330455705029600000368000000000000000000000001",
            )
            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                row = connection.execute(
                    "SELECT danfse_pdf_status, danfse_pdf FROM invoice"
                ).fetchone()
            self.assertEqual(row, ("GERADO_DO_XML", b"%PDF-generated-from-xml"))


if __name__ == "__main__":
    unittest.main()
