from __future__ import annotations

import base64
import gzip
import unittest

from taxlink_nfse.parser import DfeDecoder


NFSE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<NFSe xmlns="http://www.sped.fazenda.gov.br/nfse">
  <infNFSe Id="NFSe330455705029600000368000000000000000000000001">
    <nNFSe>12345</nNFSe>
    <DPS>
      <infDPS>
        <serie>90001</serie>
        <dhEmi>2026-07-20T10:30:00-03:00</dhEmi>
        <dCompet>2026-07-01</dCompet>
        <prest><CNPJ>28524508000108</CNPJ><xNome>Fornecedor Teste</xNome></prest>
        <toma><CNPJ>05029600000368</CNPJ><xNome>Unidade Teste</xNome></toma>
        <serv><cServ><cTribNac>010101</cTribNac><xDescServ>Servico mensal</xDescServ></cServ></serv>
        <valores><vServPrest><vServ>1500.45</vServ></vServPrest><vLiq>1400.45</vLiq></valores>
      </infDPS>
    </DPS>
  </infNFSe>
</NFSe>
"""


class ParserTests(unittest.TestCase):
    def test_decodes_gzip_and_parses_national_nfse(self) -> None:
        envelope = {
            "NSU": 42,
            "ChaveAcesso": "330455705029600000368000000000000000000000001",
            "TipoDocumento": "NFSE",
            "ArquivoXml": base64.b64encode(gzip.compress(NFSE_XML)).decode("ascii"),
        }

        document = DfeDecoder().decode(envelope)

        self.assertEqual(document.nsu, 42)
        self.assertEqual(document.xml_bytes, NFSE_XML)
        self.assertIsNotNone(document.invoice)
        invoice = document.invoice
        assert invoice is not None
        self.assertEqual(invoice.document_number, "12345")
        self.assertEqual(invoice.series, "90001")
        self.assertEqual(invoice.provider_tax_id, "28524508000108")
        self.assertEqual(invoice.taker_tax_id, "05029600000368")
        self.assertEqual(invoice.competence_date, "2026-07-01")
        self.assertEqual(invoice.service_amount_cents, 150045)
        self.assertEqual(invoice.net_amount_cents, 140045)
        self.assertEqual(invoice.items[0].code, "010101")


if __name__ == "__main__":
    unittest.main()
