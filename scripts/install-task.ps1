param(
    [string]$TaskName = "TaxLink NFS-e Collector",
    [switch]$StartNow,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$executable = Join-Path $projectRoot "dist\taxlink-nfse-service.exe"
$config = Join-Path $projectRoot "config.toml"

if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Executavel nao encontrado: $executable. Execute scripts\build.ps1 primeiro."
}
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    throw "Configuracao nao encontrada: $config"
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    throw "A tarefa '$TaskName' ja existe. Use -Force para substitui-la."
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction `
    -Execute $executable `
    -Argument ('--config "{0}" run' -f $config) `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal `
    -UserId $identity `
    -LogonType Interactive `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Coleta NFS-e do ADN e grava no SQLite do TaxLink."

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Output "Tarefa '$TaskName' instalada para o usuario $identity."
Write-Output "Log: $(Join-Path $projectRoot 'logs\taxlink-nfse.log')"
