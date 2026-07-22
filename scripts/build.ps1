param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & $Python -m PyInstaller --clean --noconfirm packaging/taxlink-nfse.spec
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao gerar o executavel com PyInstaller."
    }
    Write-Output "Executaveis gerados:"
    Write-Output "  dist\taxlink-nfse.exe (administracao)"
    Write-Output "  dist\taxlink-nfse-service.exe (segundo plano)"
}
finally {
    Pop-Location
}
