from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import pkcs12

from taxlink_nfse.config import CertificateConfig


@dataclass(frozen=True, slots=True)
class CertificateMetadata:
    thumbprint: str
    valid_from: str
    valid_until: str


def inspect_certificate(certificate: CertificateConfig) -> CertificateMetadata | None:
    """Extrai metadados publicos sem persistir senha ou chave privada."""
    public_certificate: x509.Certificate | None = None
    if certificate.provider == "pfx":
        if certificate.pfx_path is None or not certificate.pfx_path.is_file():
            return None
        password = os.environ.get(certificate.password_env)
        if password is None:
            return None
        _, public_certificate, _ = pkcs12.load_key_and_certificates(
            certificate.pfx_path.read_bytes(), password.encode("utf-8")
        )
    elif certificate.provider == "pem":
        if certificate.pem_cert_path is None or not certificate.pem_cert_path.is_file():
            return None
        public_certificate = x509.load_pem_x509_certificate(
            certificate.pem_cert_path.read_bytes()
        )
    else:
        return None

    if public_certificate is None:
        raise ValueError("O arquivo nao contem um certificado X.509 utilizavel.")
    if hasattr(public_certificate, "not_valid_before_utc"):
        valid_from = public_certificate.not_valid_before_utc
        valid_until = public_certificate.not_valid_after_utc
    else:
        valid_from = public_certificate.not_valid_before.replace(tzinfo=timezone.utc)
        valid_until = public_certificate.not_valid_after.replace(tzinfo=timezone.utc)
    return CertificateMetadata(
        thumbprint=public_certificate.fingerprint(hashes.SHA1()).hex().upper(),
        valid_from=_iso_utc(valid_from),
        valid_until=_iso_utc(valid_until),
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")
