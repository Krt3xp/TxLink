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


if __name__ == "__main__":
    unittest.main()

