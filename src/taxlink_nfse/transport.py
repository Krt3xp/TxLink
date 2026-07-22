from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from taxlink_nfse.config import CertificateConfig


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    def get(
        self, url: str, certificate: CertificateConfig, timeout_seconds: int
    ) -> HttpResponse: ...


class MutualTlsTransport:
    def get(
        self, url: str, certificate: CertificateConfig, timeout_seconds: int
    ) -> HttpResponse:
        if certificate.provider == "windows":
            return self._get_with_windows_store(url, certificate, timeout_seconds)
        if certificate.provider == "pfx":
            return self._get_with_pfx(url, certificate, timeout_seconds)
        raise RuntimeError(f"Provedor de certificado desconhecido: {certificate.provider}")

    def certificate_exists(self, certificate: CertificateConfig) -> bool:
        if certificate.provider == "pfx":
            return bool(certificate.pfx_path and certificate.pfx_path.is_file())
        locations = (
            "@('CurrentUser','LocalMachine')"
            if certificate.store_location == "Auto"
            else "@('{0}')".format(certificate.store_location)
        )
        script = (
            "$ErrorActionPreference='Stop'; $cert=$null; "
            "foreach($location in {0}) {{ "
            "$candidate=Get-Item ('Cert:\\'+$location+'\\My\\{1}') -ErrorAction SilentlyContinue; "
            "if($candidate -and $candidate.HasPrivateKey) {{$cert=$candidate; break}} }}; "
            "if ($cert) {{ exit 0 }} else {{ exit 1 }}"
        ).format(locations, certificate.thumbprint)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            check=False,
            creationflags=_no_window_flag(),
        )
        return result.returncode == 0

    def _get_with_windows_store(
        self, url: str, certificate: CertificateConfig, timeout_seconds: int
    ) -> HttpResponse:
        if os.name != "nt":
            raise RuntimeError("O provedor windows so pode ser usado no Windows.")

        script_content = r"""param(
  [Parameter(Mandatory=$true)][string]$Url,
  [Parameter(Mandatory=$true)][string]$Thumbprint,
  [Parameter(Mandatory=$true)][string]$StoreLocation,
  [Parameter(Mandatory=$true)][string]$OutputFile,
  [Parameter(Mandatory=$true)][string]$HeadersFile,
  [Parameter(Mandatory=$true)][int]$TimeoutSeconds
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http
$locations = if ($StoreLocation -eq 'Auto') { @('CurrentUser','LocalMachine') } else { @($StoreLocation) }
$cert = $null
foreach ($location in $locations) {
  $certPath = "Cert:\$location\My\$Thumbprint"
  $candidate = Get-Item -LiteralPath $certPath -ErrorAction SilentlyContinue
  if ($candidate -and $candidate.HasPrivateKey) { $cert = $candidate; break }
}
if (-not $cert) { throw "Certificado nao encontrado em CurrentUser/My ou LocalMachine/My: $Thumbprint" }
if (-not $cert.HasPrivateKey) { throw "Certificado sem chave privada: $Thumbprint" }
$handler = [System.Net.Http.HttpClientHandler]::new()
[void]$handler.ClientCertificates.Add($cert)
$client = [System.Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
try {
  $response = $client.GetAsync($Url).GetAwaiter().GetResult()
  $body = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
  [System.IO.File]::WriteAllBytes($OutputFile, $body)
  $headerLines = New-Object System.Collections.Generic.List[string]
  foreach ($header in $response.Headers) {
    $headerLines.Add(($header.Key + ': ' + ($header.Value -join ', ')))
  }
  foreach ($header in $response.Content.Headers) {
    $headerLines.Add(($header.Key + ': ' + ($header.Value -join ', ')))
  }
  [System.IO.File]::WriteAllLines($HeadersFile, $headerLines)
  Write-Output ([int]$response.StatusCode)
}
finally {
  if ($client) { $client.Dispose() }
  if ($handler) { $handler.Dispose() }
}
"""
        with tempfile.TemporaryDirectory(prefix="taxlink-nfse-") as temp_dir:
            temp_path = Path(temp_dir)
            script_path = temp_path / "request.ps1"
            output_path = temp_path / "response.bin"
            headers_path = temp_path / "headers.txt"
            script_path.write_text(script_content, encoding="utf-8")
            command = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Url",
                url,
                "-Thumbprint",
                certificate.thumbprint,
                "-StoreLocation",
                certificate.store_location,
                "-OutputFile",
                str(output_path),
                "-HeadersFile",
                str(headers_path),
                "-TimeoutSeconds",
                str(timeout_seconds),
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                creationflags=_no_window_flag(),
            )
            if result.returncode != 0:
                details = (result.stderr or result.stdout or "erro desconhecido").strip()
                raise RuntimeError(f"Falha no acesso ao ADN com certificado Windows: {details}")

            status_code = _last_integer(result.stdout)
            body = output_path.read_bytes() if output_path.is_file() else b""
            headers = _read_headers(headers_path)
            return HttpResponse(status_code=status_code, body=body, headers=headers)

    def _get_with_pfx(
        self, url: str, certificate: CertificateConfig, timeout_seconds: int
    ) -> HttpResponse:
        assert certificate.pfx_path is not None
        if not certificate.pfx_path.is_file():
            raise RuntimeError(f"Arquivo PFX nao encontrado: {certificate.pfx_path}")
        password = os.environ.get(certificate.password_env)
        if password is None:
            raise RuntimeError(
                f"Variavel de ambiente com a senha do PFX nao definida: {certificate.password_env}"
            )

        try:
            import requests
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
                pkcs12,
            )
        except ImportError as exc:
            raise RuntimeError("Instale requests e cryptography para utilizar certificado PFX.") from exc

        private_key, public_certificate, chain = pkcs12.load_key_and_certificates(
            certificate.pfx_path.read_bytes(), password.encode("utf-8")
        )
        if private_key is None or public_certificate is None:
            raise RuntimeError("O PFX nao contem certificado e chave privada utilizaveis.")

        cert_pem = public_certificate.public_bytes(Encoding.PEM)
        for chain_certificate in chain or ():
            cert_pem += chain_certificate.public_bytes(Encoding.PEM)
        key_pem = private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )

        with tempfile.TemporaryDirectory(prefix="taxlink-nfse-pfx-") as temp_dir:
            temp_path = Path(temp_dir)
            cert_path = temp_path / "certificate.pem"
            key_path = temp_path / "private-key.pem"
            cert_path.write_bytes(cert_pem)
            key_path.write_bytes(key_pem)
            response = requests.get(
                url,
                cert=(str(cert_path), str(key_path)),
                timeout=timeout_seconds,
                verify=True,
            )
            return HttpResponse(
                status_code=response.status_code,
                body=response.content,
                headers=dict(response.headers),
            )


def _last_integer(output: str) -> int:
    for line in reversed((output or "").splitlines()):
        stripped = line.strip()
        if stripped.isdigit():
            return int(stripped)
    raise RuntimeError(f"A requisicao nao retornou um status HTTP valido: {output!r}")


def _read_headers(path: Path) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not path.is_file():
        return headers
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip()] = value.strip()
    return headers


def _no_window_flag() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
