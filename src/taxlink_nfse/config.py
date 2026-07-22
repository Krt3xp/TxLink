from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def normalize_tax_id(value: object) -> str:
    """Normaliza CPF/CNPJ sem assumir que o identificador sera sempre numerico."""
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def normalize_thumbprint(value: object) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()


def normalize_environment(value: object) -> str:
    normalized = str(value or "production").strip().lower()
    aliases = {
        "producao": "production",
        "prod": "production",
        "production": "production",
        "homologacao": "restricted",
        "homologation": "restricted",
        "teste": "restricted",
        "test": "restricted",
        "restricted": "restricted",
        "producao_restrita": "restricted",
    }
    if normalized not in aliases:
        raise ValueError(f"Ambiente NFS-e invalido: {value}")
    return aliases[normalized]


@dataclass(frozen=True, slots=True)
class CertificateConfig:
    provider: str
    thumbprint: str = ""
    store_location: str = "Auto"
    pfx_path: Path | None = None
    password_env: str = ""
    certificate_tax_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path) -> "CertificateConfig":
        provider = str(data.get("provider", "windows")).strip().lower()
        pfx_path_value = str(data.get("pfx_path", "")).strip()
        pfx_path = _resolve_path(base_dir, pfx_path_value) if pfx_path_value else None
        config = cls(
            provider=provider,
            thumbprint=normalize_thumbprint(data.get("thumbprint")),
            store_location=str(data.get("store_location", "Auto")).strip(),
            pfx_path=pfx_path,
            password_env=str(data.get("password_env", "")).strip(),
            certificate_tax_id=normalize_tax_id(data.get("certificate_tax_id")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.provider not in {"windows", "pfx"}:
            raise ValueError(f"Provedor de certificado nao suportado: {self.provider}")
        if self.provider == "windows":
            if not self.thumbprint:
                raise ValueError("Certificado Windows requer thumbprint.")
            if self.store_location not in {"Auto", "CurrentUser", "LocalMachine"}:
                raise ValueError("store_location deve ser Auto, CurrentUser ou LocalMachine.")
        if self.provider == "pfx":
            if self.pfx_path is None:
                raise ValueError("Certificado PFX requer pfx_path.")
            if not self.password_env:
                raise ValueError("Certificado PFX requer password_env; a senha nao pode ficar no TOML.")


@dataclass(frozen=True, slots=True)
class UnitConfig:
    code: str
    system_unit_id: int | None
    tax_id: str
    name: str
    environment: str
    initial_nsu: int
    enabled: bool
    certificate: CertificateConfig

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path) -> "UnitConfig":
        code = str(data.get("code", "")).strip()
        tax_id = normalize_tax_id(data.get("tax_id"))
        system_unit_raw = data.get("system_unit_id")
        system_unit_id = int(system_unit_raw) if system_unit_raw not in (None, "") else None
        unit = cls(
            code=code,
            system_unit_id=system_unit_id,
            tax_id=tax_id,
            name=str(data.get("name", code)).strip(),
            environment=normalize_environment(data.get("environment")),
            initial_nsu=max(0, int(data.get("initial_nsu", 0))),
            enabled=bool(data.get("enabled", True)),
            certificate=CertificateConfig.from_dict(dict(data.get("certificate") or {}), base_dir),
        )
        unit.validate()
        return unit

    def validate(self) -> None:
        if not self.code:
            raise ValueError("Toda unidade requer um code estavel.")
        if not self.tax_id:
            raise ValueError(f"Unidade {self.code} requer tax_id.")
        certificate_tax_id = self.certificate.certificate_tax_id
        if certificate_tax_id and certificate_tax_id[:8] != self.tax_id[:8]:
            raise ValueError(
                f"O CNPJ raiz do certificado nao corresponde ao CNPJ da unidade {self.code}."
            )


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    database_path: Path
    log_path: Path
    cycle_interval_seconds: int
    idle_poll_seconds: int
    error_backoff_seconds: int
    max_error_backoff_seconds: int
    max_batches_per_cycle: int
    request_timeout_seconds: int
    request_attempts: int


@dataclass(frozen=True, slots=True)
class AdnConfig:
    production_base_url: str
    restricted_base_url: str
    production_danfse_base_url: str
    restricted_danfse_base_url: str
    batch_mode: bool
    download_danfse_pdf: bool

    def base_url_for(self, environment: str) -> str:
        if environment == "production":
            return self.production_base_url.rstrip("/")
        return self.restricted_base_url.rstrip("/")

    def danfse_base_url_for(self, environment: str) -> str:
        if environment == "production":
            return self.production_danfse_base_url.rstrip("/")
        return self.restricted_danfse_base_url.rstrip("/")


@dataclass(frozen=True, slots=True)
class AppConfig:
    source_path: Path
    collector: CollectorConfig
    adn: AdnConfig
    units: tuple[UnitConfig, ...]

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Arquivo de configuracao nao encontrado: {source_path}")
        with source_path.open("rb") as config_file:
            raw = tomllib.load(config_file)

        base_dir = source_path.parent
        collector_raw = dict(raw.get("collector") or {})
        adn_raw = dict(raw.get("adn") or {})
        units_raw = raw.get("units") or []
        if not isinstance(units_raw, list) or not units_raw:
            raise ValueError("A configuracao deve conter ao menos uma [[units]].")

        collector = CollectorConfig(
            database_path=_resolve_path(
                base_dir, str(collector_raw.get("database_path", "data/taxlink-nfse.sqlite3"))
            ),
            log_path=_resolve_path(
                base_dir, str(collector_raw.get("log_path", "logs/taxlink-nfse.log"))
            ),
            cycle_interval_seconds=_positive_int(collector_raw, "cycle_interval_seconds", 60),
            idle_poll_seconds=_positive_int(collector_raw, "idle_poll_seconds", 3600),
            error_backoff_seconds=_positive_int(collector_raw, "error_backoff_seconds", 300),
            max_error_backoff_seconds=_positive_int(
                collector_raw, "max_error_backoff_seconds", 3600
            ),
            max_batches_per_cycle=_positive_int(collector_raw, "max_batches_per_cycle", 20),
            request_timeout_seconds=_positive_int(
                collector_raw, "request_timeout_seconds", 180
            ),
            request_attempts=_positive_int(collector_raw, "request_attempts", 3),
        )
        if collector.max_error_backoff_seconds < collector.error_backoff_seconds:
            raise ValueError("max_error_backoff_seconds nao pode ser menor que error_backoff_seconds.")

        adn = AdnConfig(
            production_base_url=str(
                adn_raw.get("production_base_url", "https://adn.nfse.gov.br/contribuintes")
            ).strip(),
            restricted_base_url=str(
                adn_raw.get(
                    "restricted_base_url",
                    "https://adn.producaorestrita.nfse.gov.br/contribuintes",
                )
            ).strip(),
            production_danfse_base_url=str(
                adn_raw.get("production_danfse_base_url", "https://adn.nfse.gov.br/danfse")
            ).strip(),
            restricted_danfse_base_url=str(
                adn_raw.get(
                    "restricted_danfse_base_url",
                    "https://adn.producaorestrita.nfse.gov.br/danfse",
                )
            ).strip(),
            batch_mode=bool(adn_raw.get("batch_mode", True)),
            download_danfse_pdf=bool(adn_raw.get("download_danfse_pdf", True)),
        )
        units = tuple(UnitConfig.from_dict(dict(item), base_dir) for item in units_raw)
        codes = [unit.code for unit in units]
        if len(codes) != len(set(codes)):
            raise ValueError("Os codes das unidades devem ser unicos.")
        return cls(source_path=source_path, collector=collector, adn=adn, units=units)


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _positive_int(data: dict[str, Any], key: str, default: int) -> int:
    value = int(data.get(key, default))
    if value <= 0:
        raise ValueError(f"{key} deve ser maior que zero.")
    return value
