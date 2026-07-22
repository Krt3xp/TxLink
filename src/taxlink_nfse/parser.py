from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence

from taxlink_nfse.config import normalize_tax_id
from taxlink_nfse.domain import DecodedDfe, InvoiceItem, ParsedInvoice


class DfeDecodeError(ValueError):
    pass


class DfeDecoder:
    """Decodifica os envelopes JSON retornados pela API de distribuicao do ADN."""

    def decode(self, envelope: Mapping[str, Any]) -> DecodedDfe:
        nsu = self._integer(self._get(envelope, "NSU", "Nsu", "nsu"), "NSU")
        envelope_key = str(
            self._get(envelope, "ChaveAcesso", "chaveAcesso", "chave_acesso") or ""
        ).strip()
        payload = self._get(envelope, "ArquivoXml", "arquivoXml", "Xml", "xml")
        if payload in (None, ""):
            raise DfeDecodeError(f"Documento NSU {nsu} nao possui ArquivoXml.")

        xml_bytes, compressed_xml = self._decode_payload(payload, nsu)
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise DfeDecodeError(f"XML invalido no NSU {nsu}: {exc}") from exc

        parser = NationalNfseParser()
        xml_key = parser.extract_access_key(root)
        access_key = self._normalize_access_key(envelope_key or xml_key)
        document_type = str(
            self._get(envelope, "TipoDocumento", "tipoDocumento", "tipo_documento")
            or parser.local_name(root.tag)
        ).strip()
        schema_name = str(
            self._get(envelope, "Schema", "schema", "schemaXml", "schema_xml") or ""
        ).strip()
        generated_at = normalize_datetime(
            self._get(envelope, "DataHoraGeracao", "dataHoraGeracao", "geradoEm")
        )
        invoice = parser.parse(root, access_key)
        if invoice and not access_key:
            access_key = invoice.access_key

        return DecodedDfe(
            nsu=nsu,
            access_key=access_key,
            schema_name=schema_name,
            document_type=document_type,
            compressed_xml=compressed_xml,
            xml_bytes=xml_bytes,
            xml_sha256=hashlib.sha256(xml_bytes).hexdigest(),
            generated_at=generated_at,
            invoice=invoice,
        )

    @staticmethod
    def _get(envelope: Mapping[str, Any], *names: str) -> Any:
        lowered = {str(key).lower(): value for key, value in envelope.items()}
        for name in names:
            if name.lower() in lowered:
                return lowered[name.lower()]
        return None

    @staticmethod
    def _integer(value: Any, field: str) -> int:
        try:
            parsed = int(str(value))
        except (TypeError, ValueError) as exc:
            raise DfeDecodeError(f"{field} invalido: {value!r}") from exc
        if parsed < 0:
            raise DfeDecodeError(f"{field} nao pode ser negativo.")
        return parsed

    @staticmethod
    def _decode_payload(payload: Any, nsu: int) -> tuple[bytes, bytes]:
        if isinstance(payload, bytes):
            encoded = payload
        else:
            encoded = re.sub(r"\s+", "", str(payload)).encode("ascii", errors="strict")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DfeDecodeError(f"ArquivoXml do NSU {nsu} nao e Base64 valido.") from exc

        if decoded.lstrip().startswith(b"<"):
            return decoded, gzip.compress(decoded, compresslevel=9)
        try:
            return gzip.decompress(decoded), decoded
        except (gzip.BadGzipFile, EOFError, OSError) as exc:
            raise DfeDecodeError(f"ArquivoXml do NSU {nsu} nao e GZip valido.") from exc

    @staticmethod
    def _normalize_access_key(value: str) -> str:
        normalized = re.sub(r"[^0-9A-Za-z]", "", value or "")
        if normalized.lower().startswith("nfse"):
            normalized = normalized[4:]
        return normalized.upper()


class NationalNfseParser:
    """Parser tolerante a namespaces, mas restrito aos caminhos do leiaute nacional."""

    def parse(self, root: ET.Element, envelope_access_key: str = "") -> ParsedInvoice | None:
        info = self._first_element(root, "infNFSe")
        if info is None:
            return None

        access_key = envelope_access_key or self.extract_access_key(root)
        provider_tax_id = normalize_tax_id(
            self._find_text(info, ("prest", "CNPJ"), ("prest", "CPF"), ("emit", "CNPJ"))
        )
        taker_tax_id = normalize_tax_id(
            self._find_text(
                info,
                ("toma", "CNPJ"),
                ("toma", "CPF"),
                ("toma", "NIF"),
                ("dest", "CNPJ"),
                ("dest", "CPF"),
            )
        )
        service_amount = money_to_cents(
            self._find_text(
                info,
                ("valores", "vServPrest", "vServ"),
                ("vServPrest", "vServ"),
                ("valores", "vServ"),
            )
        )
        net_amount = money_to_cents(
            self._find_text(info, ("valores", "vLiq"), ("vLiq",))
        )
        service_description = self._find_text(
            info,
            ("serv", "cServ", "xDescServ"),
            ("cServ", "xDescServ"),
            ("xDescServ",),
        )
        items: tuple[InvoiceItem, ...] = ()
        if service_description or service_amount is not None:
            items = (
                InvoiceItem(
                    item_number=1,
                    code=self._find_text(
                        info,
                        ("serv", "cServ", "cTribNac"),
                        ("cServ", "cTribNac"),
                    ),
                    description=service_description,
                    total_amount_cents=service_amount,
                ),
            )

        return ParsedInvoice(
            access_key=access_key,
            document_number=self._find_text(info, ("nNFSe",), ("nDFSe",)),
            series=self._find_text(info, ("DPS", "infDPS", "serie"), ("infDPS", "serie")),
            issued_at=normalize_datetime(
                self._find_text(
                    info,
                    ("DPS", "infDPS", "dhEmi"),
                    ("infDPS", "dhEmi"),
                    ("dhEmi",),
                    ("dhProc",),
                )
            ),
            competence_date=normalize_date(
                self._find_text(
                    info,
                    ("DPS", "infDPS", "dCompet"),
                    ("infDPS", "dCompet"),
                    ("dCompet",),
                )
            ),
            provider_tax_id=provider_tax_id,
            provider_name=self._find_text(
                info, ("prest", "xNome"), ("emit", "xNome"), ("prest", "razaoSocial")
            ),
            taker_tax_id=taker_tax_id,
            taker_name=self._find_text(
                info, ("toma", "xNome"), ("dest", "xNome"), ("toma", "razaoSocial")
            ),
            service_code=self._find_text(
                info,
                ("serv", "cServ", "cTribNac"),
                ("cServ", "cTribNac"),
            ),
            service_description=service_description,
            service_amount_cents=service_amount,
            net_amount_cents=net_amount,
            status=self._status(info),
            items=items,
        )

    def extract_access_key(self, root: ET.Element) -> str:
        info = self._first_element(root, "infNFSe")
        if info is not None:
            raw_id = str(info.attrib.get("Id") or info.attrib.get("id") or "")
            normalized = DfeDecoder._normalize_access_key(raw_id)
            if normalized:
                return normalized
        key = self._find_text(root, ("chNFSe",))
        return DfeDecoder._normalize_access_key(key)

    def _status(self, info: ET.Element) -> str:
        status = self._find_text(info, ("cStat",))
        return status or "NORMAL"

    def _find_text(self, root: ET.Element, *paths: Sequence[str]) -> str:
        for path in paths:
            nodes: Iterable[ET.Element]
            if len(path) == 1:
                nodes = (
                    element for element in root.iter() if self.local_name(element.tag) == path[0]
                )
            else:
                nodes = (
                    element for element in root.iter() if self.local_name(element.tag) == path[0]
                )
                for segment in path[1:]:
                    nodes = tuple(
                        child
                        for node in nodes
                        for child in list(node)
                        if self.local_name(child.tag) == segment
                    )
            for node in nodes:
                value = (node.text or "").strip()
                if value:
                    return value
        return ""

    def _first_element(self, root: ET.Element, local_name: str) -> ET.Element | None:
        for element in root.iter():
            if self.local_name(element.tag) == local_name:
                return element
        return None

    @staticmethod
    def local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]


def money_to_cents(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        amount = Decimal(text.replace(",", "."))
    except InvalidOperation as exc:
        raise DfeDecodeError(f"Valor monetario invalido no XML: {text}") from exc
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def normalize_datetime(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError:
        return text


def normalize_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10]
