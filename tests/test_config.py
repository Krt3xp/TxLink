from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taxlink_nfse.config import AppConfig


class ConfigTests(unittest.TestCase):
    def test_loads_relative_paths_and_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text(
                """
[collector]
database_path = "data/test.sqlite3"
log_path = "logs/test.log"

[adn]
batch_mode = true

[[units]]
code = "u1"
system_unit_id = 7
tax_id = "05.029.600/0003-68"
name = "Unidade Teste"
environment = "homologacao"
initial_nsu = 12

[units.certificate]
provider = "windows"
thumbprint = "AA BB CC DD"
store_location = "LocalMachine"
certificate_tax_id = "05.029.600/0001-04"
""".strip(),
                encoding="utf-8",
            )

            config = AppConfig.load(config_path)

            self.assertEqual(config.collector.database_path, root / "data" / "test.sqlite3")
            self.assertEqual(config.units[0].tax_id, "05029600000368")
            self.assertEqual(config.units[0].environment, "restricted")
            self.assertEqual(config.units[0].certificate.thumbprint, "AABBCCDD")
            self.assertEqual(config.units[0].initial_nsu, 12)

    def test_loads_linux_pem_api_scheduler_and_sftp_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text(
                """
[collector]
database_path = "data/master.db"
log_path = "logs/collector.log"

[api]
host = "0.0.0.0"
port = 8080
bearer_token_env = "TAXLINK_API_TOKEN"

[scheduler]
enabled = true
daily_times = ["01:30", "13:45"]
timezone = "America/Sao_Paulo"

[sync]
enabled = true
local_mirror_path = "data/mirror.db"
host = "windows.local"
username = "taxlink"
password_env = "TAXLINK_SFTP_PASSWORD"
known_hosts_path = "ssh/known_hosts"
remote_directory = "/C:/TaxLink/data"

[[units]]
code = "u1"
tax_id = "05029600000368"
environment = "production"

[units.certificate]
provider = "pem"
pem_cert_path = "certs/certificate.pem"
pem_key_path = "certs/private-key.pem"
certificate_tax_id = "05029600000104"
""".strip(),
                encoding="utf-8",
            )

            config = AppConfig.load(config_path)

            self.assertTrue(config.scheduler.enabled)
            self.assertEqual(config.scheduler.daily_times, ("01:30", "13:45"))
            self.assertTrue(config.sync.enabled)
            self.assertEqual(config.sync.host, "windows.local")
            self.assertEqual(config.units[0].certificate.provider, "pem")
            self.assertEqual(
                config.units[0].certificate.pem_cert_path,
                root / "certs" / "certificate.pem",
            )


if __name__ == "__main__":
    unittest.main()
