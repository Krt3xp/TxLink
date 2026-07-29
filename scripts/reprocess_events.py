import sqlite3
import xml.etree.ElementTree as ET
from taxlink_nfse.parser import NationalNfseParser
from taxlink_nfse.domain import DecodedDfe
from taxlink_nfse.storage import SqliteRepository

def main():
    db_path = "data/taxlink-nfse.sqlite3"
    repo = SqliteRepository(db_path)
    parser = NationalNfseParser()
    
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        artifacts = conn.execute(
            """
            SELECT a.id, a.unit_id, u.code AS unit_code, a.nsu, a.access_key,
                   a.schema_name, a.document_type, a.xml_content, a.xml_sha256, a.generated_at
            FROM dfe_artifact a
            JOIN fiscal_unit u ON u.id = a.unit_id
            """
        ).fetchall()
        
    print(f"Analisando {len(artifacts)} artefatos para reprocessamento de eventos fiscais...")
    updated = 0
    with repo.transaction(immediate=True) as connection:
        for row in artifacts:
            try:
                xml_bytes = bytes(row["xml_content"] or b"")
                if not xml_bytes:
                    continue
                root = ET.fromstring(xml_bytes)
                fiscal_event = parser.parse_fiscal_event(root, row["access_key"] or "")
                if fiscal_event:
                    doc = DecodedDfe(
                        nsu=int(row["nsu"]),
                        access_key=str(row["access_key"] or ""),
                        schema_name=str(row["schema_name"] or ""),
                        document_type=str(row["document_type"] or ""),
                        compressed_xml=b"",
                        xml_bytes=xml_bytes,
                        xml_sha256=str(row["xml_sha256"] or ""),
                        fiscal_event=fiscal_event,
                    )
                    repo._upsert_fiscal_event(connection, int(row["unit_id"]), int(row["id"]), doc, "2026-07-29T11:20:00Z")
                    updated += 1
                    print(f"  [OK] Evento fiscal aplicado: NSU {row['nsu']} -> {fiscal_event.event_type} para chave {fiscal_event.invoice_access_key}")
            except Exception as exc:
                print(f"  [ERRO] NSU {row['nsu']}: {exc}")
                
    print(f"Total de eventos fiscais atualizados com sucesso: {updated}")

if __name__ == "__main__":
    main()
