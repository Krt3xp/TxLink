from __future__ import annotations

import html
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from importlib import resources
from typing import Iterable, Mapping, Sequence

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"

_GREEN = HexColor("#147A4C")
_NAVY = HexColor("#223A78")
_TEXT = HexColor("#1F2933")
_LABEL = HexColor("#55606A")
_BORDER = HexColor("#C7D0D6")
_INNER_BORDER = HexColor("#E7ECEF")
_HIGHLIGHT = HexColor("#F5F7F8")
_WHITE = HexColor("#FFFFFF")


@dataclass(frozen=True, slots=True)
class _Cell:
    width_mm: float
    label: str
    value: str
    bold_value: bool = False
    fill: object | None = None
    top_aligned: bool = False


class DanfseXmlParser:
    """Extrai os campos do leiaute nacional necessários ao DANFSe local."""

    def parse(
        self, xml_bytes: bytes, external_access_key: str = ""
    ) -> dict[str, str]:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise ValueError(f"XML da NFS-e inválido: {exc}") from exc

        info = _first_descendant(root, "infNFSe")
        if info is None:
            raise ValueError("XML não contém o elemento infNFSe.")

        dps = _first_descendant(info, "infDPS")
        emit = _direct_child(info, "emit")
        prest = _first_descendant(dps, "prest")
        toma = _first_descendant(dps, "toma")
        intermediary = _first_descendant(dps, "interm")
        service = _first_descendant(dps, "serv")

        nfse_values = _direct_child(info, "valores")
        dps_values = _direct_child(dps, "valores")
        service_values = _first_descendant(dps_values, "vServPrest")
        tax = _first_descendant(dps_values, "trib")
        municipal_tax = _first_descendant(tax, "tribMun")
        federal_tax = _first_descendant(tax, "tribFed")
        pis_cofins = _first_descendant(federal_tax, "piscofins")
        approximate_tax = _first_descendant(tax, "totTrib")

        access_key = _normalize_access_key(
            external_access_key or str(info.attrib.get("Id") or info.attrib.get("id") or "")
        )
        issued_nfse = _text(info, "dhProc")
        issued_dps = _text(dps, "dhEmi")
        competence = _text(dps, "dCompet")

        issuer_address_source = _address_source(emit)
        if issuer_address_source is None:
            issuer_address_source = _address_source(prest)
        taker_address_source = _address_source(toma)
        issuer_location = _text(info, "xLocEmi")
        service_location = _text(info, "xLocPrestacao")

        service_amount = _first_value(
            _text(service_values, "vServ"),
            _text(dps_values, "vServ"),
            _text(info, "vServ"),
        )
        net_amount = _first_value(
            _text(nfse_values, "vLiq"),
            _text(dps_values, "vLiq"),
            _text(info, "vLiq"),
        )
        unconditional_discount = _first_value(
            _text(dps_values, "vDescIncond"),
            _text(info, "vDescIncond"),
        )
        conditional_discount = _first_value(
            _text(dps_values, "vDescCond"),
            _text(info, "vDescCond"),
        )
        deductions = _first_value(
            _text(municipal_tax, "vDedRed"),
            _text(dps_values, "vDedRed"),
            _text(info, "vDedRed"),
        )
        tax_base = _first_value(
            _text(municipal_tax, "vBC"),
            _text(dps_values, "vBC"),
            _text(info, "vBC"),
        )
        issqn_amount = _first_value(
            _text(municipal_tax, "vISSQN"),
            _text(dps_values, "vISSQN"),
            _text(info, "vISSQN"),
        )

        irrf = _first_value(_text(federal_tax, "vRetIRRF"), _text(info, "vRetIRRF"))
        social_security = _first_value(
            _text(federal_tax, "vRetCP"), _text(info, "vRetCP")
        )
        csll = _first_value(
            _text(federal_tax, "vRetCSLL"), _text(info, "vRetCSLL")
        )
        pis = _first_value(_text(pis_cofins, "vPis"), _text(info, "vPis"))
        cofins = _first_value(_text(pis_cofins, "vCofins"), _text(info, "vCofins"))
        federal_retention_total = _sum_decimal_texts(
            (irrf, social_security, csll, pis, cofins)
        )

        national_code = _text(service, "cTribNac")
        national_description = _first_value(
            _text(info, "xTribNac"), _text(service, "xTribNac")
        )
        municipal_code = _text(service, "cTribMun")
        municipal_description = _first_value(
            _text(info, "xTribMun"), _text(service, "xTribMun")
        )
        nbs_code = _text(service, "cNBS")
        nbs_description = _first_value(_text(info, "xNBS"), _text(service, "xNBS"))

        retention_code = _text(municipal_tax, "tpRetISSQN")
        suspension_value = _first_value(
            _text(municipal_tax, "exigSusp"),
            _text(municipal_tax, "indExigISSQN"),
        )

        issuer_name = _pick_text((emit, prest), ("xNome", "razaoSocial"))
        issuer_document = _pick_text((emit, prest), ("CNPJ", "CPF", "NIF"))
        taker_name = _pick_text((toma,), ("xNome", "razaoSocial"))
        taker_document = _pick_text((toma,), ("CNPJ", "CPF", "NIF"))

        fields = {
            "numero_nfs": _dash(_first_value(_text(info, "nNFSe"), _text(info, "nDFSe"))),
            "chave_acesso": _dash(access_key),
            "competencia": _format_date(competence),
            "data_hora_emissao_nfs": _format_datetime(issued_nfse or issued_dps),
            "situacao_nfs": _status(_text(info, "cStat")),
            "numero_dps": _dash(_text(dps, "nDPS")),
            "serie_dps": _dash(_text(dps, "serie")),
            "data_hora_emissao_dps": _format_datetime(issued_dps),
            "ambiente": _environment(_text(dps, "tpAmb")),
            "emitente_nome": _dash(issuer_name),
            "emitente_documento": _format_tax_id(issuer_document),
            "emitente_inscricao_municipal": _dash(
                _pick_text((emit, prest), ("IM",))
            ),
            "emitente_endereco": _format_address(issuer_address_source),
            "emitente_email": _dash(_pick_text((emit, prest), ("email",))),
            "emitente_telefone": _format_phone(
                _pick_text((emit, prest), ("fone", "telefone"))
            ),
            "emitente_municipio": _format_municipality(
                issuer_address_source, issuer_location
            ),
            "emitente_cep": _format_cep(_text(issuer_address_source, "CEP")),
            "simples_nacional": _simple_national(_text(prest, "opSimpNac")),
            "regime_apuracao_sn": _dash(
                _regime_sn(_text(prest, "regApTribSN"))
            ),
            "tomador_nome": _dash(taker_name),
            "tomador_documento": _format_tax_id(taker_document),
            "tomador_inscricao_municipal": _dash(_text(toma, "IM")),
            "tomador_endereco": _format_address(taker_address_source),
            "tomador_email": _dash(_text(toma, "email")),
            "tomador_telefone": _format_phone(
                _first_value(_text(toma, "fone"), _text(toma, "telefone"))
            ),
            "tomador_municipio": _format_municipality(
                taker_address_source, service_location
            ),
            "tomador_cep": _format_cep(_text(taker_address_source, "CEP")),
            "intermediario_servico": _format_intermediary(intermediary),
            "codigo_tributacao_nacional": _code_and_description(
                _format_national_service_code(national_code), national_description
            ),
            "codigo_tributacao_municipal": _code_and_description(
                municipal_code, municipal_description
            ),
            "local_prestacao": _dash(
                _first_value(
                    service_location,
                    _text(service, "xLocPrestacao"),
                    _text(service, "cLocPrestacao"),
                )
            ),
            "pais_prestacao": _dash(
                _first_value(
                    _text(service, "xPaisPrestacao"),
                    _country(_text(service, "cPaisPrestacao")),
                )
            ),
            "descricao_servico": _plain_text(_text(service, "xDescServ")),
            "tributacao_issqn": _issqn_taxation(_text(municipal_tax, "tribISSQN")),
            "pais_resultado_prestacao": _dash(
                _first_value(
                    _text(municipal_tax, "xPaisResult"),
                    _country(_text(municipal_tax, "cPaisResult")),
                )
            ),
            "municipio_incidencia_issqn": _dash(
                _first_value(
                    _text(municipal_tax, "xLocIncid"),
                    _text(municipal_tax, "cLocIncid"),
                    service_location,
                )
            ),
            "regime_especial_tributacao": _special_tax_regime(
                _text(prest, "regEspTrib")
            ),
            "tipo_imunidade": _immunity(_text(municipal_tax, "tpImunidade")),
            "suspensao_exigibilidade_issqn": _yes_no(suspension_value),
            "numero_processo_suspensao": _dash(
                _first_value(
                    _text(municipal_tax, "nProcesso"),
                    _text(municipal_tax, "nProcessoSusp"),
                )
            ),
            "beneficio_municipal": _dash(
                _first_value(
                    _text(municipal_tax, "cBenef"),
                    _text(municipal_tax, "idBenefMun"),
                )
            ),
            "valor_servico": _money(service_amount),
            "desconto_incondicionado": _money(unconditional_discount),
            "total_deducoes_reducoes": _money(deductions),
            "calculo_bm": _dash(
                _first_value(
                    _money_or_blank(_text(municipal_tax, "vCalcDR")),
                    _percent_or_blank(_text(municipal_tax, "pRedBC")),
                )
            ),
            "bc_issqn": _money(tax_base),
            "aliquota_aplicada": _percent(_text(municipal_tax, "pAliq")),
            "retencao_issqn": _issqn_retention(retention_code),
            "issqn_apurado": _money(issqn_amount),
            "irrf": _money(irrf),
            "contribuicao_previdenciaria_retida": _money(social_security),
            "contribuicoes_sociais_retidas": _money(csll),
            "descricao_contribuicoes_retidas": _dash(
                _first_value(
                    _text(federal_tax, "xDescRet"),
                    _text(federal_tax, "xDescContrib"),
                )
            ),
            "pis_debito_apuracao_propria": _money(pis),
            "cofins_debito_apuracao_propria": _money(cofins),
            "pis_cofins_debito_apuracao_propria": _money(
                _sum_decimal_texts((pis, cofins))
            ),
            "total_retencoes_federais": _money(federal_retention_total),
            "valor_servico_total": _money(service_amount),
            "desconto_condicionado": _money(conditional_discount),
            "desconto_incondicionado_total": _money(unconditional_discount),
            "issqn_retido": _money(issqn_amount if retention_code in {"2", "3"} else ""),
            "outras_retencoes": _money(
                _first_value(_text(dps_values, "vOutrasRet"), _text(info, "vOutrasRet"))
            ),
            "outras_deducoes": _money(
                _first_value(_text(dps_values, "vOutrasDed"), _text(info, "vOutrasDed"))
            ),
            "valor_liquido_nfs": _money(net_amount),
            "tributos_federais": _money(
                _first_value(
                    _text(approximate_tax, "vTotTribFed"),
                    _text(info, "vTotTribFed"),
                )
            ),
            "tributos_estaduais": _money(
                _first_value(
                    _text(approximate_tax, "vTotTribEst"),
                    _text(info, "vTotTribEst"),
                )
            ),
            "tributos_municipais": _money(
                _first_value(
                    _text(approximate_tax, "vTotTribMun"),
                    _text(info, "vTotTribMun"),
                )
            ),
            "informacoes_complementares": _dash(
                _plain_text(
                    _first_value(_text(dps, "xInfComp"), _text(info, "xInfComp"))
                )
            ),
            "nbs": _code_and_description(nbs_code, nbs_description),
        }
        return {name: _pdf_safe(value) for name, value in fields.items()}


class DanfsePdfGenerator:
    """Gera um DANFSe A4 com o sistema visual do modelo fornecido."""

    def __init__(self, parser: DanfseXmlParser | None = None):
        self.parser = parser or DanfseXmlParser()

    def generate(self, xml_bytes: bytes, access_key: str = "") -> bytes:
        fields = self.parser.parse(xml_bytes, access_key)
        output = io.BytesIO()
        pdf = canvas.Canvas(output, pagesize=A4, pageCompression=1)
        pdf.setTitle(f"DANFSe {fields['numero_nfs']}")
        pdf.setAuthor("TaxLink")
        pdf.setCreator("TaxLink NFS-e Collector")
        pdf.setSubject("Documento auxiliar gerado a partir do XML nacional da NFS-e")
        self._draw_page(pdf, fields)
        pdf.showPage()
        pdf.save()
        result = output.getvalue()
        if not result.startswith(b"%PDF-"):
            raise RuntimeError("O gerador não produziu um PDF válido.")
        return result

    def _draw_page(self, pdf: canvas.Canvas, f: Mapping[str, str]) -> None:
        page_width, page_height = A4
        x = 10 * mm
        width = 190 * mm
        top = page_height - 5 * mm

        logo = resources.files("taxlink_nfse").joinpath("assets/nfse_logo.png")
        with logo.open("rb") as stream:
            image_bytes = stream.read()
        pdf.drawImage(
            ImageReader(io.BytesIO(image_bytes)),
            x,
            top - 10.3 * mm,
            width=52 * mm,
            height=10.3 * mm,
            preserveAspectRatio=True,
            anchor="sw",
            mask="auto",
        )
        pdf.setFillColor(_NAVY)
        pdf.setFont(_FONT_BOLD, 8.5)
        pdf.drawRightString(x + width, top - 4.2 * mm, "DOCUMENTO AUXILIAR DA NFS-e")
        pdf.setFillColor(_TEXT)
        pdf.setFont(_FONT_BOLD, 8)
        pdf.drawRightString(
            x + width, top - 8.7 * mm, f"NFS-e nº {f['numero_nfs']}"
        )
        top -= 13 * mm

        top = self._draw_rows(
            pdf,
            x,
            top,
            (
                (
                    10,
                    (
                        _Cell(
                            190,
                            "Chave de Acesso da NFS-e",
                            f["chave_acesso"],
                            True,
                            _HIGHLIGHT,
                        ),
                    ),
                ),
            ),
            outer_color=HexColor("#8EA0AA"),
        )
        top = self._draw_rows(
            pdf,
            x,
            top,
            (
                (
                    9,
                    (
                        _Cell(47.5, "Número da NFS-e", f["numero_nfs"]),
                        _Cell(47.5, "Competência da NFS-e", f["competencia"]),
                        _Cell(
                            47.5,
                            "Data e hora da emissão da NFS-e",
                            f["data_hora_emissao_nfs"],
                        ),
                        _Cell(47.5, "Situação", f["situacao_nfs"]),
                    ),
                ),
                (
                    9,
                    (
                        _Cell(47.5, "Número da DPS", f["numero_dps"]),
                        _Cell(47.5, "Série da DPS", f["serie_dps"]),
                        _Cell(
                            47.5,
                            "Data e hora da emissão da DPS",
                            f["data_hora_emissao_dps"],
                        ),
                        _Cell(47.5, "Ambiente", f["ambiente"]),
                    ),
                ),
            ),
        )

        top = self._draw_section(pdf, x, top, width, "EMITENTE DA NFS-E")
        top = self._draw_rows(
            pdf,
            x,
            top,
            (
                (
                    11,
                    (
                        _Cell(110, "Nome / Nome empresarial", f["emitente_nome"], True),
                        _Cell(43, "CNPJ / CPF / NIF", f["emitente_documento"]),
                        _Cell(
                            37,
                            "Inscrição municipal",
                            f["emitente_inscricao_municipal"],
                        ),
                    ),
                ),
                (
                    11,
                    (
                        _Cell(110, "Endereço", f["emitente_endereco"]),
                        _Cell(43, "E-mail", f["emitente_email"]),
                        _Cell(37, "Telefone", f["emitente_telefone"]),
                    ),
                ),
                (
                    11,
                    (
                        _Cell(66, "Município", f["emitente_municipio"]),
                        _Cell(44, "CEP", f["emitente_cep"]),
                        _Cell(
                            43,
                            "Simples Nacional na competência",
                            f["simples_nacional"],
                        ),
                        _Cell(
                            37,
                            "Regime de apuração pelo SN",
                            f["regime_apuracao_sn"],
                        ),
                    ),
                ),
            ),
        )

        top = self._draw_section(pdf, x, top, width, "TOMADOR DO SERVIÇO")
        top = self._draw_rows(
            pdf,
            x,
            top,
            (
                (
                    11,
                    (
                        _Cell(110, "Nome / Nome empresarial", f["tomador_nome"], True),
                        _Cell(43, "CNPJ / CPF / NIF", f["tomador_documento"]),
                        _Cell(
                            37,
                            "Inscrição municipal",
                            f["tomador_inscricao_municipal"],
                        ),
                    ),
                ),
                (
                    11,
                    (
                        _Cell(110, "Endereço", f["tomador_endereco"]),
                        _Cell(43, "E-mail", f["tomador_email"]),
                        _Cell(37, "Telefone", f["tomador_telefone"]),
                    ),
                ),
                (
                    11,
                    (
                        _Cell(66, "Município", f["tomador_municipio"]),
                        _Cell(44, "CEP", f["tomador_cep"]),
                        _Cell(
                            80,
                            "Intermediário do serviço",
                            f["intermediario_servico"],
                        ),
                    ),
                ),
            ),
        )

        top = self._draw_section(pdf, x, top, width, "SERVIÇO PRESTADO")
        top = self._draw_rows(
            pdf,
            x,
            top,
            (
                (
                    12,
                    (
                        _Cell(
                            54,
                            "Código de tributação nacional",
                            f["codigo_tributacao_nacional"],
                        ),
                        _Cell(
                            43,
                            "Código de tributação municipal",
                            f["codigo_tributacao_municipal"],
                        ),
                        _Cell(50, "Local da prestação", f["local_prestacao"]),
                        _Cell(43, "País da prestação", f["pais_prestacao"]),
                    ),
                ),
                (
                    24,
                    (
                        _Cell(
                            190,
                            "Descrição do serviço",
                            f["descricao_servico"],
                            top_aligned=True,
                        ),
                    ),
                ),
            ),
        )

        top = self._draw_section(pdf, x, top, width, "TRIBUTAÇÃO MUNICIPAL")
        top = self._draw_rows(
            pdf,
            x,
            top,
            (
                (
                    9,
                    (
                        _Cell(47.5, "Tributação do ISSQN", f["tributacao_issqn"]),
                        _Cell(
                            47.5,
                            "País do resultado da prestação",
                            f["pais_resultado_prestacao"],
                        ),
                        _Cell(
                            47.5,
                            "Município de incidência do ISSQN",
                            f["municipio_incidencia_issqn"],
                        ),
                        _Cell(
                            47.5,
                            "Regime especial de tributação",
                            f["regime_especial_tributacao"],
                        ),
                    ),
                ),
                (
                    9,
                    (
                        _Cell(47.5, "Tipo de imunidade", f["tipo_imunidade"]),
                        _Cell(
                            47.5,
                            "Suspensão da exigibilidade do ISSQN",
                            f["suspensao_exigibilidade_issqn"],
                        ),
                        _Cell(
                            47.5,
                            "Número do processo de suspensão",
                            f["numero_processo_suspensao"],
                        ),
                        _Cell(
                            47.5, "Benefício municipal", f["beneficio_municipal"]
                        ),
                    ),
                ),
                (
                    9,
                    (
                        _Cell(47.5, "Valor do serviço", f["valor_servico"]),
                        _Cell(
                            47.5,
                            "Desconto incondicionado",
                            f["desconto_incondicionado"],
                        ),
                        _Cell(
                            47.5,
                            "Total de deduções/reduções",
                            f["total_deducoes_reducoes"],
                        ),
                        _Cell(47.5, "Cálculo do BM", f["calculo_bm"]),
                    ),
                ),
                (
                    9,
                    (
                        _Cell(47.5, "BC ISSQN", f["bc_issqn"]),
                        _Cell(
                            47.5, "Alíquota aplicada", f["aliquota_aplicada"]
                        ),
                        _Cell(47.5, "Retenção do ISSQN", f["retencao_issqn"]),
                        _Cell(47.5, "ISSQN apurado", f["issqn_apurado"]),
                    ),
                ),
            ),
        )

        top = self._draw_section(pdf, x, top, width, "TRIBUTAÇÃO FEDERAL")
        top = self._draw_rows(
            pdf,
            x,
            top,
            (
                (
                    9,
                    (
                        _Cell(47.5, "IRRF", f["irrf"]),
                        _Cell(
                            47.5,
                            "Contribuição previdenciária retida",
                            f["contribuicao_previdenciaria_retida"],
                        ),
                        _Cell(
                            47.5,
                            "Contribuições sociais retidas",
                            f["contribuicoes_sociais_retidas"],
                        ),
                        _Cell(
                            47.5,
                            "Descrição das contribuições retidas",
                            f["descricao_contribuicoes_retidas"],
                        ),
                    ),
                ),
                (
                    9,
                    (
                        _Cell(
                            47.5,
                            "PIS - débito por apuração própria",
                            f["pis_debito_apuracao_propria"],
                        ),
                        _Cell(
                            47.5,
                            "COFINS - débito por apuração própria",
                            f["cofins_debito_apuracao_propria"],
                        ),
                        _Cell(
                            47.5,
                            "PIS/COFINS - débito por apuração própria",
                            f["pis_cofins_debito_apuracao_propria"],
                        ),
                        _Cell(
                            47.5,
                            "Total das retenções federais",
                            f["total_retencoes_federais"],
                        ),
                    ),
                ),
            ),
        )

        top = self._draw_section(pdf, x, top, width, "VALOR TOTAL DA NFS-E")
        top = self._draw_rows(
            pdf,
            x,
            top,
            (
                (
                    9,
                    (
                        _Cell(47.5, "Valor do serviço", f["valor_servico_total"]),
                        _Cell(
                            47.5,
                            "Desconto condicionado",
                            f["desconto_condicionado"],
                        ),
                        _Cell(
                            47.5,
                            "Desconto incondicionado",
                            f["desconto_incondicionado_total"],
                        ),
                        _Cell(47.5, "ISSQN retido", f["issqn_retido"]),
                    ),
                ),
                (
                    9,
                    (
                        _Cell(
                            47.5,
                            "Total das retenções federais",
                            f["total_retencoes_federais"],
                        ),
                        _Cell(47.5, "Outras retenções", f["outras_retencoes"]),
                        _Cell(47.5, "Outras deduções", f["outras_deducoes"]),
                        _Cell(
                            47.5,
                            "Valor líquido da NFS-e",
                            f["valor_liquido_nfs"],
                            True,
                        ),
                    ),
                ),
            ),
        )

        top = self._draw_rows(
            pdf,
            x,
            top,
            (
                (
                    25,
                    (
                        _Cell(
                            81,
                            "TOTAIS APROXIMADOS DOS TRIBUTOS",
                            (
                                f"Federais: {f['tributos_federais']}\n"
                                f"Estaduais: {f['tributos_estaduais']}\n"
                                f"Municipais: {f['tributos_municipais']}"
                            ),
                            fill=_HIGHLIGHT,
                            top_aligned=True,
                        ),
                        _Cell(
                            109,
                            "INFORMAÇÕES COMPLEMENTARES",
                            f"{f['informacoes_complementares']}\nNBS: {f['nbs']}",
                            top_aligned=True,
                        ),
                    ),
                ),
            ),
            green_labels=True,
        )

        if top < 10 * mm:
            raise RuntimeError("O conteúdo do DANFSe ultrapassou a área útil da página.")
        pdf.setFillColor(_LABEL)
        pdf.setFont(_FONT, 6)
        pdf.drawCentredString(
            page_width / 2,
            5 * mm,
            "Documento gerado eletronicamente a partir dos dados da NFS-e.",
        )

    @staticmethod
    def _draw_section(
        pdf: canvas.Canvas, x: float, top: float, width: float, title: str
    ) -> float:
        height = 5 * mm
        bottom = top - height
        pdf.setFillColor(_GREEN)
        pdf.setStrokeColor(_GREEN)
        pdf.rect(x, bottom, width, height, stroke=1, fill=1)
        pdf.setFillColor(_WHITE)
        pdf.setFont(_FONT_BOLD, 8)
        pdf.drawString(x + 1.3 * mm, bottom + 1.55 * mm, title)
        return bottom

    def _draw_rows(
        self,
        pdf: canvas.Canvas,
        x: float,
        top: float,
        rows: Sequence[tuple[float, Sequence[_Cell]]],
        outer_color: object = _BORDER,
        green_labels: bool = False,
    ) -> float:
        table_top = top
        for height_mm, cells in rows:
            height = height_mm * mm
            bottom = top - height
            current_x = x
            for cell in cells:
                width = cell.width_mm * mm
                self._draw_cell(
                    pdf,
                    current_x,
                    bottom,
                    width,
                    height,
                    cell,
                    green_labels,
                )
                current_x += width
            top = bottom

        pdf.setStrokeColor(outer_color)
        pdf.setLineWidth(0.5)
        pdf.rect(x, top, 190 * mm, table_top - top, stroke=1, fill=0)
        return top

    @staticmethod
    def _draw_cell(
        pdf: canvas.Canvas,
        x: float,
        bottom: float,
        width: float,
        height: float,
        cell: _Cell,
        green_label: bool,
    ) -> None:
        pdf.setFillColor(cell.fill or _WHITE)
        pdf.setStrokeColor(_INNER_BORDER)
        pdf.setLineWidth(0.35)
        pdf.rect(x, bottom, width, height, stroke=1, fill=1)

        pad_x = 1.15 * mm
        max_width = max(1, width - 2 * pad_x)
        label_color = _GREEN if green_label else _LABEL
        label_lines = _wrap_lines(cell.label, _FONT_BOLD, 6.5, max_width)
        value_font = _FONT_BOLD if cell.bold_value else _FONT
        value_size = _fit_value_font_size(
            cell.value or "-", value_font, 7.5, max_width
        )
        value_lines = _wrap_lines(
            cell.value or "-", value_font, value_size, max_width
        )
        label_leading = 7.0
        value_leading = value_size + 0.5
        gap = 1.2
        available = height - 2.0 * mm
        required = (
            len(label_lines) * label_leading
            + gap
            + len(value_lines) * value_leading
        )
        max_value_lines = max(
            1,
            int(
                (
                    available
                    - len(label_lines) * label_leading
                    - gap
                )
                // value_leading
            ),
        )
        if len(value_lines) > max_value_lines:
            value_lines = _truncate_lines(
                value_lines,
                max_value_lines,
                value_font,
                value_size,
                max_width,
            )
            required = (
                len(label_lines) * label_leading
                + gap
                + len(value_lines) * value_leading
            )

        if cell.top_aligned:
            baseline = bottom + height - 1.4 * mm - 6.5
        else:
            baseline = bottom + (height + required) / 2 - 6.5

        pdf.setFillColor(label_color)
        pdf.setFont(_FONT_BOLD, 6.5)
        for line in label_lines:
            pdf.drawString(x + pad_x, baseline, line)
            baseline -= label_leading

        baseline -= gap
        pdf.setFillColor(_TEXT)
        pdf.setFont(value_font, value_size)
        for line in value_lines:
            pdf.drawString(x + pad_x, baseline, line)
            baseline -= value_leading


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for child in list(element):
        if _local_name(child.tag) == name:
            return child
    return None


def _first_descendant(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for node in element.iter():
        if _local_name(node.tag) == name:
            return node
    return None


def _text(element: ET.Element | None, name: str) -> str:
    node = _first_descendant(element, name)
    if node is None:
        return ""
    return _clean_text(node.text or "")


def _pick_text(
    elements: Iterable[ET.Element | None], names: Iterable[str]
) -> str:
    for element in elements:
        for name in names:
            value = _text(element, name)
            if value:
                return value
    return ""


def _address_source(element: ET.Element | None) -> ET.Element | None:
    if element is None:
        return None
    address = _first_descendant(element, "end")
    if address is not None:
        return address
    address = _first_descendant(element, "enderNac")
    if address is not None:
        return address
    return None


def _first_value(*values: str) -> str:
    return next((value for value in values if value), "")


def _clean_text(value: str) -> str:
    text = html.unescape(str(value or "")).strip()
    for _ in range(2):
        if not any(marker in text for marker in ("Ã", "Â", "�")):
            break
        try:
            repaired = text.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        old_score = sum(text.count(marker) for marker in ("Ã", "Â", "�"))
        new_score = sum(repaired.count(marker) for marker in ("Ã", "Â", "�"))
        if new_score >= old_score:
            break
        text = repaired
    return re.sub(r"[ \t]+", " ", text)


def _plain_text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _normalize_access_key(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]", "", value or "")
    normalized = re.sub(r"^(?:NFSE|NFS)", "", normalized, flags=re.IGNORECASE)
    return normalized.upper()


def _dash(value: str) -> str:
    return value if value else "-"


def _format_date(value: str) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return value


def _format_datetime(value: str) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return value


def _format_tax_id(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 14:
        return (
            f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/"
            f"{digits[8:12]}-{digits[12:]}"
        )
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return _dash(value)


def _format_cep(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return f"{digits[:5]}-{digits[5:]}" if len(digits) == 8 else _dash(value)


def _format_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return _dash(value)


def _format_address(address: ET.Element | None) -> str:
    if address is None:
        return "-"
    street = _text(address, "xLgr")
    number = _text(address, "nro")
    complement = _text(address, "xCpl")
    neighborhood = _text(address, "xBairro")
    first = ", ".join(part for part in (street, number) if part)
    remainder = " - ".join(part for part in (complement, neighborhood) if part)
    return _dash(" - ".join(part for part in (first, remainder) if part))


def _format_municipality(address: ET.Element | None, fallback: str) -> str:
    city = _first_value(_text(address, "xMun"), fallback, _text(address, "cMun"))
    state = _text(address, "UF")
    if city and state and not city.upper().endswith(f"- {state.upper()}"):
        return f"{city} - {state}"
    return _dash(city)


def _format_intermediary(element: ET.Element | None) -> str:
    if element is None:
        return "Não identificado na NFS-e"
    name = _text(element, "xNome")
    document = _format_tax_id(
        _first_value(_text(element, "CNPJ"), _text(element, "CPF"), _text(element, "NIF"))
    )
    return _dash(" - ".join(part for part in (name, document) if part and part != "-"))


def _format_national_service_code(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 6:
        return f"{digits[:2]}.{digits[2:4]}.{digits[4:]}"
    return value


def _code_and_description(code: str, description: str) -> str:
    if code and description:
        return f"{code} - {description}"
    return _dash(code or description)


def _decimal(value: str) -> Decimal | None:
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return None
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def _sum_decimal_texts(values: Iterable[str]) -> str:
    parsed = [number for value in values if (number := _decimal(value)) is not None]
    return str(sum(parsed, Decimal("0"))) if parsed else ""


def _money(value: str) -> str:
    number = _decimal(value)
    if number is None:
        return "-"
    formatted = f"{number:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {formatted}"


def _money_or_blank(value: str) -> str:
    return _money(value) if value else ""


def _percent(value: str) -> str:
    number = _decimal(value)
    if number is None:
        return "-"
    return f"{number:.2f}".replace(".", ",") + " %"


def _percent_or_blank(value: str) -> str:
    return _percent(value) if value else ""


def _status(value: str) -> str:
    return {
        "100": "Normal",
        "101": "Cancelada",
        "102": "Substituída",
    }.get(value, _dash(value))


def _environment(value: str) -> str:
    return {"1": "Produção", "2": "Produção restrita"}.get(value, _dash(value))


def _simple_national(value: str) -> str:
    return {
        "1": "Não optante",
        "2": "Optante - MEI",
        "3": "Optante - ME/EPP",
    }.get(value, _dash(value))


def _regime_sn(value: str) -> str:
    return {
        "0": "Nenhum",
        "1": "Regime de caixa",
        "2": "Regime de competência",
    }.get(value, value)


def _issqn_taxation(value: str) -> str:
    return {
        "1": "Operação tributável",
        "2": "Imunidade",
        "3": "Exportação de serviço",
        "4": "Não incidência",
    }.get(value, _dash(value))


def _special_tax_regime(value: str) -> str:
    return {
        "0": "Nenhum",
        "1": "Ato cooperado",
        "2": "Estimativa",
        "3": "Microempreendedor individual",
        "4": "Sociedade de profissionais",
        "5": "Cooperativa",
    }.get(value, _dash(value))


def _immunity(value: str) -> str:
    return {
        "0": "Não se aplica",
        "1": "Patrimônio, renda ou serviços",
        "2": "Templos de qualquer culto",
        "3": "Partidos, sindicatos e instituições",
        "4": "Livros, jornais e periódicos",
        "5": "Fonogramas e videofonogramas",
    }.get(value, _dash(value))


def _yes_no(value: str) -> str:
    return {
        "1": "Sim",
        "true": "Sim",
        "S": "Sim",
        "2": "Não",
        "0": "Não",
        "false": "Não",
        "N": "Não",
    }.get(value, _dash(value))


def _issqn_retention(value: str) -> str:
    return {
        "1": "Não retido",
        "2": "Retido pelo tomador",
        "3": "Retido pelo intermediário",
    }.get(value, _dash(value))


def _country(value: str) -> str:
    return {"1058": "Brasil", "BR": "Brasil"}.get(value.upper(), value)


def _pdf_safe(value: str) -> str:
    text = str(value or "-")
    return (
        text.replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
    )


def _wrap_lines(
    text: str, font_name: str, font_size: float, max_width: float
) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text or "-").splitlines() or ["-"]:
        words = [
            piece
            for word in paragraph.split()
            for piece in _split_long_word(word, font_name, font_size, max_width)
        ]
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines or ["-"]


def _fit_value_font_size(
    text: str,
    font_name: str,
    preferred_size: float,
    max_width: float,
    minimum_size: float = 5.5,
) -> float:
    tokens = [token for line in str(text).splitlines() for token in line.split()]
    if not tokens:
        return preferred_size
    widest = max(
        pdfmetrics.stringWidth(token, font_name, preferred_size)
        for token in tokens
    )
    if widest <= max_width:
        return preferred_size
    return max(minimum_size, preferred_size * max_width / widest)


def _split_long_word(
    word: str, font_name: str, font_size: float, max_width: float
) -> list[str]:
    if pdfmetrics.stringWidth(word, font_name, font_size) <= max_width:
        return [word]
    pieces: list[str] = []
    remaining = word
    while remaining:
        cut = 1
        for index in range(2, len(remaining) + 1):
            if (
                pdfmetrics.stringWidth(
                    remaining[:index], font_name, font_size
                )
                > max_width
            ):
                break
            cut = index
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    return pieces


def _truncate_lines(
    lines: Sequence[str],
    maximum: int,
    font_name: str,
    font_size: float,
    max_width: float,
) -> list[str]:
    result = list(lines[:maximum])
    if len(lines) <= maximum:
        return result
    final = result[-1].rstrip()
    suffix = "..."
    while final and pdfmetrics.stringWidth(
        final + suffix, font_name, font_size
    ) > max_width:
        final = final[:-1].rstrip()
    result[-1] = (final + suffix) if final else suffix
    return result
