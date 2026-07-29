from __future__ import annotations

import unittest

from taxlink_nfse.danfse import DanfsePdfGenerator, DanfseXmlParser
from tests.test_parser import NFSE_XML


class DanfseXmlParserTests(unittest.TestCase):
    def test_extracts_and_formats_fields_from_national_nfse(self) -> None:
        fields = DanfseXmlParser().parse(NFSE_XML)

        self.assertEqual(
            fields["chave_acesso"],
            "330455705029600000368000000000000000000000001",
        )
        self.assertEqual(fields["numero_nfs"], "12345")
        self.assertEqual(fields["numero_dps"], "-")
        self.assertEqual(fields["serie_dps"], "90001")
        self.assertEqual(fields["competencia"], "01/07/2026")
        self.assertEqual(fields["emitente_documento"], "28.524.508/0001-08")
        self.assertEqual(fields["tomador_documento"], "05.029.600/0003-68")
        self.assertEqual(fields["valor_servico"], "R$ 1.500,45")
        self.assertEqual(fields["valor_liquido_nfs"], "R$ 1.400,45")

    def test_generates_single_a4_pdf_with_template_sections(self) -> None:
        pdf_bytes = DanfsePdfGenerator().generate(NFSE_XML)

        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertTrue(pdf_bytes.rstrip().endswith(b"%%EOF"))
        self.assertGreater(len(pdf_bytes), 10_000)

    def test_long_unbroken_description_does_not_escape_the_page(self) -> None:
        long_xml = NFSE_XML.replace(
            b"Servico mensal",
            b"X" * 4_000,
        )

        pdf_bytes = DanfsePdfGenerator().generate(long_xml)

        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertTrue(pdf_bytes.rstrip().endswith(b"%%EOF"))


if __name__ == "__main__":
    unittest.main()
