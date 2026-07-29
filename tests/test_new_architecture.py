from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from taxlink_nfse.api import create_app
from taxlink_nfse.storage import SCHEMA_VERSION, SqliteRepository
from taxlink_nfse.sync import MirrorSyncService
from tests.test_storage import sample_document, write_test_config


class NewArchitectureTests(unittest.TestCase):
    def test_extracts_pfx_validity_and_does_not_use_windows_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pfx_path = root / "certificate.pfx"
            password = "test-password"
            valid_from = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
            valid_until = valid_from + timedelta(days=365)
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = issuer = x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "TaxLink Test")]
            )
            certificate = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(private_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(valid_from)
                .not_valid_after(valid_until)
                .sign(private_key, hashes.SHA256())
            )
            pfx_path.write_bytes(
                pkcs12.serialize_key_and_certificates(
                    b"taxlink-test",
                    private_key,
                    certificate,
                    None,
                    serialization.BestAvailableEncryption(password.encode()),
                )
            )
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
[collector]
database_path = "data/test.sqlite3"
log_path = "logs/test.log"

[[units]]
code = "u1"
tax_id = "05029600000368"
environment = "production"

[units.certificate]
provider = "pfx"
pfx_path = "{pfx_path.as_posix()}"
password_env = "TEST_PFX_PASSWORD"
certificate_tax_id = "05029600000104"
""".strip(),
                encoding="utf-8",
            )
            from taxlink_nfse.config import AppConfig

            config = AppConfig.load(config_path)
            repository = SqliteRepository(config.collector.database_path)
            with patch.dict("os.environ", {"TEST_PFX_PASSWORD": password}):
                repository.initialize(config.units)
            row = repository.certificates()[0]

            self.assertEqual(row["provider"], "pfx")
            self.assertIsNone(row["store_location"])
            self.assertEqual(row["valid_from"], valid_from.isoformat(timespec="seconds"))
            self.assertEqual(row["valid_until"], valid_until.isoformat(timespec="seconds"))
            self.assertEqual(
                row["thumbprint"], certificate.fingerprint(hashes.SHA1()).hex().upper()
            )
            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                presentation = connection.execute(
                    'SELECT "Formato", "Arquivo no Servidor", "Situacao" '
                    "FROM vw_certificados_digitais"
                ).fetchone()
            self.assertEqual(presentation, ("PFX", str(pfx_path), "VALIDO"))

    def test_registers_certificate_and_persistent_collection_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = write_test_config(Path(temp_dir))
            repository = SqliteRepository(config.collector.database_path)
            repository.initialize(config.units)

            certificate = repository.certificate_for_unit("u1")
            self.assertEqual(certificate.provider, "windows")
            self.assertEqual(certificate.thumbprint, "AABBCCDD")

            job_id = repository.create_collection_job("API", "u1", "test")
            self.assertTrue(repository.claim_collection_job(job_id))
            run_id = repository.start_run("u1", job_id)
            repository.persist_batch("u1", [sample_document(10)], 11, 200, run_id)
            repository.persist_batch("u1", [sample_document(10)], 11, 200, run_id)
            repository.finish_run(run_id, "SUCCESS", 2, 2, 1)
            repository.finish_collection_job(
                job_id,
                {
                    "units_processed": 1,
                    "batches_requested": 2,
                    "documents_received": 2,
                    "documents_stored": 1,
                    "documents_ignored": 1,
                    "errors": 0,
                },
            )

            job = repository.collection_job(job_id)
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "SUCCESS")
            self.assertEqual(job["ignored_documents"], 1)
            with closing(sqlite3.connect(config.collector.database_path)) as connection:
                duplicate_count = connection.execute(
                    "SELECT COUNT(*) FROM collection_event "
                    "WHERE event_type = 'DOCUMENTO_DUPLICADO'"
                ).fetchone()[0]
                schema_version = connection.execute(
                    "SELECT MAX(version) FROM schema_version"
                ).fetchone()[0]
                contract = connection.execute(
                    "SELECT contract_id, contract_number FROM invoice"
                ).fetchone()
            self.assertEqual(duplicate_count, 1)
            self.assertEqual(schema_version, SCHEMA_VERSION)
            self.assertEqual(contract, (None, None))

    def test_creates_consistent_sqlite_snapshot_with_native_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = write_test_config(Path(temp_dir))
            repository = SqliteRepository(config.collector.database_path)
            repository.initialize(config.units)
            repository.persist_batch("u1", [sample_document(3)], 4, 200)
            service = MirrorSyncService(
                config.collector.database_path, config.sync, repository
            )

            mirror_path, size_bytes, digest = service.create_consistent_snapshot()

            self.assertTrue(mirror_path.is_file())
            self.assertEqual(size_bytes, mirror_path.stat().st_size)
            self.assertEqual(len(digest), 64)
            with closing(sqlite3.connect(mirror_path)) as mirror:
                self.assertEqual(mirror.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(mirror.execute("SELECT COUNT(*) FROM invoice").fetchone()[0], 1)

    def test_api_exposes_health_collection_and_sync_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = write_test_config(Path(temp_dir))
            app = create_app(config)
            paths = {route.path for route in app.routes}
            self.assertIn("/api/v1/health", paths)
            self.assertIn("/api/v1/coleta/executar", paths)
            self.assertIn("/api/v1/coleta/status/{execution_id}", paths)
            self.assertIn("/api/v1/sincronizacao/executar", paths)
            self.assertIn("/api/v1/sincronizacao/status/{sync_id}", paths)
            self.assertEqual(app.openapi()["info"]["version"], "0.2.0")


if __name__ == "__main__":
    unittest.main()
