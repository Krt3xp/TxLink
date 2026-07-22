from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping
from urllib.parse import urlencode

from taxlink_nfse.config import AdnConfig, CollectorConfig, UnitConfig
from taxlink_nfse.domain import DanfseResult, FetchResult
from taxlink_nfse.parser import DfeDecoder
from taxlink_nfse.transport import HttpResponse, HttpTransport, MutualTlsTransport


class AdnApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AdnClient:
    RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        adn_config: AdnConfig,
        collector_config: CollectorConfig,
        transport: HttpTransport | None = None,
        decoder: DfeDecoder | None = None,
        sleeper: Any = time.sleep,
    ):
        self.adn_config = adn_config
        self.collector_config = collector_config
        self.transport = transport or MutualTlsTransport()
        self.decoder = decoder or DfeDecoder()
        self.sleeper = sleeper
        self.logger = logging.getLogger(__name__)

    def fetch_batch(self, unit: UnitConfig, nsu: int) -> FetchResult:
        base_url = self.adn_config.base_url_for(unit.environment)
        query = {"cnpjConsulta": unit.tax_id}
        if self.adn_config.batch_mode:
            query["lote"] = "true"
        url = f"{base_url}/DFe/{nsu}?{urlencode(query)}"
        response = self._get_with_retry(url, unit)
        if response.status_code in {204, 404}:
            return FetchResult(requested_nsu=nsu, http_status=response.status_code, documents=())
        if response.status_code >= 400:
            details = response.body.decode("utf-8", errors="replace")[:1200]
            raise AdnApiError(
                f"ADN retornou HTTP {response.status_code} para {unit.code}: {details}",
                response.status_code,
            )

        envelopes = self._read_envelopes(response.body)
        documents = tuple(self.decoder.decode(envelope) for envelope in envelopes)
        return FetchResult(
            requested_nsu=nsu,
            http_status=response.status_code,
            documents=documents,
        )

    def fetch_danfse(self, unit: UnitConfig, access_key: str) -> DanfseResult:
        base_url = self.adn_config.danfse_base_url_for(unit.environment)
        response = self._get_with_retry(f"{base_url}/{access_key}", unit)
        if response.status_code == 200 and response.body.startswith(b"%PDF-"):
            return DanfseResult("BAIXADO_OFICIAL", 200, response.body)
        if response.status_code == 200:
            return DanfseResult("PDF_OFICIAL_INVALIDO", 200)
        return DanfseResult(f"NAO_DISPONIVEL_{response.status_code}", response.status_code)

    def _get_with_retry(self, url: str, unit: UnitConfig) -> HttpResponse:
        last_error: Exception | None = None
        attempts = self.collector_config.request_attempts
        for attempt in range(1, attempts + 1):
            try:
                response = self.transport.get(
                    url, unit.certificate, self.collector_config.request_timeout_seconds
                )
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    raise AdnApiError(f"Falha de transporte ao consultar o ADN: {exc}") from exc
                self._wait_before_retry(unit.code, attempt, str(exc))
                continue

            if response.status_code not in self.RETRYABLE_STATUS_CODES or attempt >= attempts:
                return response
            self._wait_before_retry(unit.code, attempt, f"HTTP {response.status_code}")

        raise AdnApiError(f"Falha ao consultar o ADN: {last_error}")

    def _wait_before_retry(self, unit_code: str, attempt: int, reason: str) -> None:
        delay = min(60, 5 * (2 ** (attempt - 1)))
        self.logger.warning(
            "Falha temporaria para unidade %s (%s); nova tentativa em %ss",
            unit_code,
            reason,
            delay,
        )
        self.sleeper(delay)

    @staticmethod
    def _read_envelopes(body: bytes) -> tuple[Mapping[str, Any], ...]:
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdnApiError("A resposta do ADN nao e um JSON UTF-8 valido.") from exc

        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            candidates = (
                payload.get("LoteDFe")
                or payload.get("loteDFe")
                or payload.get("loteDfe")
            )
            if candidates is None and _looks_like_envelope(payload):
                candidates = [payload]
        else:
            candidates = None

        if candidates is None:
            return ()
        if isinstance(candidates, dict):
            candidates = [candidates]
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, dict) for candidate in candidates
        ):
            raise AdnApiError("O campo LoteDFe retornado pelo ADN possui formato inesperado.")
        return tuple(candidates)


def _looks_like_envelope(payload: Mapping[str, Any]) -> bool:
    names = {str(name).lower() for name in payload}
    return "nsu" in names and bool({"arquivoxml", "xml"} & names)
