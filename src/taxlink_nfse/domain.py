from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class InvoiceItem:
    item_number: int
    code: str = ""
    description: str = ""
    quantity: str = ""
    total_amount_cents: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedInvoice:
    access_key: str
    document_number: str = ""
    series: str = ""
    issued_at: str = ""
    competence_date: str = ""
    provider_tax_id: str = ""
    provider_name: str = ""
    taker_tax_id: str = ""
    taker_name: str = ""
    service_code: str = ""
    service_description: str = ""
    service_amount_cents: int | None = None
    net_amount_cents: int | None = None
    status: str = "NORMAL"
    items: tuple[InvoiceItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ParsedFiscalEvent:
    """Evento fiscal extraido de CancNfse, SubNfse ou evtConfRecebNfse."""
    invoice_access_key: str
    event_type: str
    event_sequence: int = 0
    occurred_at: str = ""
    protocol: str = ""
    status: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DecodedDfe:
    nsu: int
    access_key: str
    schema_name: str
    document_type: str
    compressed_xml: bytes
    xml_bytes: bytes
    xml_sha256: str
    generated_at: str = ""
    invoice: ParsedInvoice | None = None
    fiscal_event: ParsedFiscalEvent | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_nsu: int
    http_status: int
    documents: tuple[DecodedDfe, ...]


@dataclass(frozen=True, slots=True)
class DanfseResult:
    status: str
    http_status: int
    pdf_bytes: bytes | None = None
