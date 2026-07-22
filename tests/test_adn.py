from __future__ import annotations

import base64
import gzip
import tempfile
import unittest
from pathlib import Path

from taxlink_nfse.adn import AdnClient
from taxlink_nfse.transport import HttpResponse
from tests.test_parser import NFSE_XML
from tests.test_storage import write_test_config


class FakeTransport:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.urls: list[str] = []

    def get(self, url, certificate, timeout_seconds):
        self.urls.append(url)
        return self.response


class AdnClientTests(unittest.TestCase):
    def test_fetches_batch_with_unit_tax_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = write_test_config(Path(temp_dir))
            body = (
                "{\"LoteDFe\":[{\"NSU\":7,\"ChaveAcesso\":\"KEY7\","
                "\"TipoDocumento\":\"NFSE\",\"ArquivoXml\":\""
                + base64.b64encode(gzip.compress(NFSE_XML)).decode("ascii")
                + "\"}]}"
            ).encode("utf-8")
            transport = FakeTransport(HttpResponse(200, body, {}))
            client = AdnClient(
                config.adn,
                config.collector,
                transport=transport,
                sleeper=lambda _: None,
            )

            result = client.fetch_batch(config.units[0], 7)

            self.assertEqual(result.http_status, 200)
            self.assertEqual(result.documents[0].nsu, 7)
            self.assertIn("/DFe/7?", transport.urls[0])
            self.assertIn("cnpjConsulta=05029600000368", transport.urls[0])
            self.assertIn("lote=true", transport.urls[0])

    def test_fetches_official_danfse_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = write_test_config(Path(temp_dir))
            transport = FakeTransport(HttpResponse(200, b"%PDF-test", {}))
            client = AdnClient(config.adn, config.collector, transport=transport)

            result = client.fetch_danfse(config.units[0], "KEY7")

            self.assertEqual(result.status, "BAIXADO_OFICIAL")
            self.assertEqual(result.pdf_bytes, b"%PDF-test")
            self.assertEqual(
                transport.urls[0],
                "https://adn.producaorestrita.nfse.gov.br/danfse/KEY7",
            )


if __name__ == "__main__":
    unittest.main()
