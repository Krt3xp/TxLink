from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taxlink_nfse.collector import Collector
from taxlink_nfse.domain import FetchResult
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
            self.assertEqual(client.requested, [0, 1])
            self.assertEqual(repository.cursor("u1")["next_nsu"], 1)
            self.assertEqual(repository.status()[0]["invoices"], 1)


if __name__ == "__main__":
    unittest.main()
