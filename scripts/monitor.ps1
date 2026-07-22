param(
    [string]$Config = "",
    [int]$RefreshSeconds = 2
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:ConfigPath = if ($Config) {
    [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Config))
} else {
    Join-Path $script:ProjectRoot "config.toml"
}
$script:Python = Join-Path $script:ProjectRoot ".venv\Scripts\python.exe"
$script:SourceRoot = Join-Path $script:ProjectRoot "src"
$script:AdminExe = Join-Path $script:ProjectRoot "dist\taxlink-nfse.exe"
$script:ServiceExe = Join-Path $script:ProjectRoot "dist\taxlink-nfse-service.exe"
$script:CollectorProcess = $null
$script:OnceProcess = $null
$script:BackfillProcess = $null
$script:MonitorData = $null
$script:SelectedUnit = ""
$script:Refreshing = $false
$script:DateFilterChanged = $false
$script:ActivityLines = New-Object System.Collections.Generic.List[string]
$script:TrackedProcessPath = Join-Path $script:ProjectRoot "data\monitor-process.json"

if (-not (Test-Path -LiteralPath $script:ConfigPath -PathType Leaf)) {
    [System.Windows.Forms.MessageBox]::Show(
        "Configuracao nao encontrada:`n$($script:ConfigPath)",
        "TaxLink NFS-e Monitor",
        "OK",
        "Error"
    ) | Out-Null
    exit 1
}

function ConvertTo-Argument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Invoke-CollectorJson {
    param([string[]]$CommandArguments)

    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    if (Test-Path -LiteralPath $script:Python -PathType Leaf) {
        $processInfo.FileName = $script:Python
        $allArguments = @("-m", "taxlink_nfse", "--config", $script:ConfigPath) + $CommandArguments
        $processInfo.EnvironmentVariables["PYTHONPATH"] = $script:SourceRoot
    } elseif (Test-Path -LiteralPath $script:AdminExe -PathType Leaf) {
        $processInfo.FileName = $script:AdminExe
        $allArguments = @("--config", $script:ConfigPath) + $CommandArguments
    } else {
        throw "Python virtual e executavel administrativo nao encontrados."
    }

    $processInfo.Arguments = ($allArguments | ForEach-Object { ConvertTo-Argument ([string]$_) }) -join " "
    $processInfo.WorkingDirectory = $script:ProjectRoot
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $processInfo.StandardOutputEncoding = $utf8
    $processInfo.StandardErrorEncoding = $utf8

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $processInfo
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw (($stderr + "`n" + $stdout).Trim())
    }
    return $stdout | ConvertFrom-Json
}

function Get-CertificateDetails {
    param($CertificateMetadata)
    if (-not $CertificateMetadata -or $CertificateMetadata.provider -ne "windows") {
        return $null
    }
    $locations = if ($CertificateMetadata.store_location -eq "Auto") {
        @("CurrentUser", "LocalMachine")
    } else {
        @([string]$CertificateMetadata.store_location)
    }
    foreach ($location in $locations) {
        $path = "Cert:\$location\My\$($CertificateMetadata.thumbprint)"
        $certificate = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
        if ($certificate) { return $certificate }
    }
    return $null
}

function New-Label {
    param(
        [string]$Text = "",
        [float]$Size = 9,
        [System.Drawing.FontStyle]$Style = [System.Drawing.FontStyle]::Regular,
        [System.Drawing.Color]$Color = [System.Drawing.Color]::FromArgb(45, 55, 72)
    )
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $Text
    $label.Font = New-Object System.Drawing.Font("Segoe UI", $Size, $Style)
    $label.ForeColor = $Color
    $label.AutoSize = $true
    return $label
}

function New-Button {
    param(
        [string]$Text,
        [int]$Width = 115,
        [ValidateSet("Default", "Primary", "Success", "Danger")]
        [string]$Kind = "Default"
    )
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.Width = $Width
    $button.Height = 34
    $button.FlatStyle = "Flat"
    switch ($Kind) {
        "Primary" {
            $button.BackColor = [System.Drawing.Color]::FromArgb(36, 105, 180)
            $button.ForeColor = [System.Drawing.Color]::White
            $button.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(36, 105, 180)
            $button.FlatAppearance.MouseOverBackColor = [System.Drawing.Color]::FromArgb(28, 87, 153)
        }
        "Success" {
            $button.BackColor = [System.Drawing.Color]::FromArgb(30, 137, 82)
            $button.ForeColor = [System.Drawing.Color]::White
            $button.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(30, 137, 82)
            $button.FlatAppearance.MouseOverBackColor = [System.Drawing.Color]::FromArgb(23, 112, 66)
        }
        "Danger" {
            $button.BackColor = [System.Drawing.Color]::FromArgb(186, 55, 64)
            $button.ForeColor = [System.Drawing.Color]::White
            $button.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(186, 55, 64)
            $button.FlatAppearance.MouseOverBackColor = [System.Drawing.Color]::FromArgb(155, 43, 51)
        }
        default {
            $button.BackColor = [System.Drawing.Color]::White
            $button.ForeColor = [System.Drawing.Color]::FromArgb(45, 55, 72)
            $button.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(188, 198, 210)
            $button.FlatAppearance.MouseOverBackColor = [System.Drawing.Color]::FromArgb(235, 240, 246)
        }
    }
    $button.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    return $button
}

function Add-ActivityLine {
    param([string]$Message)
    $script:ActivityLines.Add(("{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $Message))
    while ($script:ActivityLines.Count -gt 80) {
        $script:ActivityLines.RemoveAt(0)
    }
}

function Save-TrackedProcess {
    param(
        [System.Diagnostics.Process]$Process,
        [ValidateSet("collector", "once", "backfill")][string]$Kind,
        [string]$ExecutablePath
    )
    $record = @{
        pid = $Process.Id
        kind = $Kind
        executable_path = [System.IO.Path]::GetFullPath($ExecutablePath)
        started_at = (Get-Date).ToString("o")
    }
    $directory = Split-Path -Parent $script:TrackedProcessPath
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath $script:TrackedProcessPath -Encoding UTF8
}

function Remove-TrackedProcess {
    if (Test-Path -LiteralPath $script:TrackedProcessPath) {
        Remove-Item -LiteralPath $script:TrackedProcessPath -Force -ErrorAction SilentlyContinue
    }
}

function Restore-TrackedProcess {
    if (-not (Test-Path -LiteralPath $script:TrackedProcessPath -PathType Leaf)) { return }
    try {
        $record = Get-Content -LiteralPath $script:TrackedProcessPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
        $actualPath = $process.Path
        if (-not $actualPath -or [System.IO.Path]::GetFullPath($actualPath) -ne [System.IO.Path]::GetFullPath([string]$record.executable_path)) {
            Remove-TrackedProcess
            return
        }
        switch ([string]$record.kind) {
            "collector" { if (-not $script:CollectorProcess) { $script:CollectorProcess = $process } }
            "once" { if (-not $script:OnceProcess) { $script:OnceProcess = $process } }
            "backfill" { if (-not $script:BackfillProcess) { $script:BackfillProcess = $process } }
        }
    } catch {
        Remove-TrackedProcess
    }
}

function Get-OwnedProcesses {
    $items = @()
    foreach ($entry in @(
        @{ kind = "coletor continuo"; process = $script:CollectorProcess },
        @{ kind = "coleta avulsa"; process = $script:OnceProcess },
        @{ kind = "backfill historico"; process = $script:BackfillProcess }
    )) {
        if ($entry.process -and -not $entry.process.HasExited) {
            $items += $entry
        }
    }
    return $items
}

function Stop-ProcessTree {
    param([System.Diagnostics.Process]$Process)
    if (-not $Process -or $Process.HasExited) { return }
    $taskKill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    $result = Start-Process -FilePath $taskKill `
        -ArgumentList @("/PID", [string]$Process.Id, "/T", "/F") `
        -WindowStyle Hidden -Wait -PassThru
    if ($result.ExitCode -ne 0 -and -not $Process.HasExited) {
        throw "Nao foi possivel interromper a arvore do processo PID $($Process.Id)."
    }
}

function Resolve-ReportedPath {
    param(
        [string]$ReportedPath,
        [string]$RelativeFallback
    )
    if ($ReportedPath -and (Test-Path -LiteralPath $ReportedPath)) {
        return [System.IO.Path]::GetFullPath($ReportedPath)
    }
    $fallback = Join-Path $script:ProjectRoot $RelativeFallback
    if (Test-Path -LiteralPath $fallback) {
        return [System.IO.Path]::GetFullPath($fallback)
    }
    return $ReportedPath
}

function New-MetricCard {
    param([string]$Title, [string]$Key, [System.Drawing.Color]$Accent)
    $panel = New-Object System.Windows.Forms.Panel
    $panel.Dock = "Fill"
    $panel.Margin = New-Object System.Windows.Forms.Padding(6)
    $panel.BackColor = [System.Drawing.Color]::White
    $panel.BorderStyle = "FixedSingle"

    $accentBar = New-Object System.Windows.Forms.Panel
    $accentBar.Dock = "Left"
    $accentBar.Width = 5
    $accentBar.BackColor = $Accent
    $panel.Controls.Add($accentBar)

    $titleLabel = New-Label $Title 8.5 ([System.Drawing.FontStyle]::Regular) ([System.Drawing.Color]::FromArgb(100, 112, 130))
    $titleLabel.Location = New-Object System.Drawing.Point(18, 12)
    $panel.Controls.Add($titleLabel)

    $valueLabel = New-Label "0" 19 ([System.Drawing.FontStyle]::Bold) ([System.Drawing.Color]::FromArgb(28, 38, 55))
    $valueLabel.Location = New-Object System.Drawing.Point(17, 34)
    $panel.Controls.Add($valueLabel)
    $script:MetricLabels[$Key] = $valueLabel
    return $panel
}

function ConvertTo-LocalDisplay {
    param($Value)
    if (-not $Value) { return "-" }
    try { return ([datetimeoffset]::Parse([string]$Value)).ToLocalTime().ToString("dd/MM/yyyy HH:mm:ss") }
    catch { return [string]$Value }
}

function Get-FilteredInvoices {
    if (-not $script:MonitorData) { return @() }
    $start = $script:StartDate.Value.Date
    $end = $script:EndDate.Value.Date.AddDays(1).AddTicks(-1)
    return @($script:MonitorData.invoices | Where-Object {
        $unitMatch = (-not $script:SelectedUnit) -or ($_.unit_code -eq $script:SelectedUnit)
        $issued = $null
        if ($_.issued_at) {
            try { $issued = [datetimeoffset]::Parse([string]$_.issued_at).DateTime }
            catch { $issued = $null }
        }
        $dateMatch = ($null -eq $issued) -or ($issued -ge $start -and $issued -le $end)
        $unitMatch -and $dateMatch
    })
}

function Update-InvoiceGrid {
    $invoices = Get-FilteredInvoices
    $table = New-Object System.Data.DataTable
    foreach ($column in @("ID", "NSU", "Emissao", "Fornecedor", "CNPJ", "Valor", "Competencia", "XML", "DANFSe", "Contrato")) {
        [void]$table.Columns.Add($column)
    }
    foreach ($invoice in $invoices) {
        $row = $table.NewRow()
        $row["ID"] = $invoice.id
        $row["NSU"] = $invoice.nsu
        $row["Emissao"] = if ($invoice.issued_at) { ([string]$invoice.issued_at).Substring(0, [Math]::Min(10, ([string]$invoice.issued_at).Length)) } else { "" }
        $row["Fornecedor"] = $invoice.provider_name
        $row["CNPJ"] = $invoice.provider_tax_id
        $row["Valor"] = if ($null -ne $invoice.service_amount_cents) { "R$ {0:N2}" -f ([decimal]$invoice.service_amount_cents / 100) } else { "" }
        $row["Competencia"] = $invoice.competence_date
        $row["XML"] = if ([int]$invoice.xml_bytes -gt 0) { "OK" } else { "Pendente" }
        $row["DANFSe"] = if ([int]$invoice.pdf_bytes -gt 0) { "OK" } else { [string]$invoice.danfse_pdf_status }
        $row["Contrato"] = if ($invoice.contract_number) { $invoice.contract_number } else { "Nao vinculado" }
        [void]$table.Rows.Add($row)
    }
    $script:InvoiceGrid.DataSource = $table
    if ($script:InvoiceGrid.Columns.Count -gt 0) {
        $script:InvoiceGrid.Columns[0].Width = 45
        $script:InvoiceGrid.Columns[1].Width = 60
        $script:InvoiceGrid.Columns[2].Width = 90
        $script:InvoiceGrid.Columns[3].AutoSizeMode = "Fill"
        $script:InvoiceGrid.Columns[4].Width = 115
        $script:InvoiceGrid.Columns[5].Width = 100
        $script:InvoiceGrid.Columns[6].Width = 90
        $script:InvoiceGrid.Columns[7].Width = 55
        $script:InvoiceGrid.Columns[8].Width = 115
        $script:InvoiceGrid.Columns[9].Width = 115
    }

    $script:MetricLabels["invoices"].Text = [string]$invoices.Count
    $script:MetricLabels["xml"].Text = [string]@($invoices | Where-Object { [int]$_.xml_bytes -gt 0 }).Count
    $script:MetricLabels["pdf"].Text = [string]@($invoices | Where-Object { [int]$_.pdf_bytes -gt 0 }).Count
    $script:MetricLabels["contracts"].Text = [string]@($invoices | Where-Object { $null -ne $_.contract_id }).Count
    $script:MetricLabels["pending"].Text = [string]@($invoices | Where-Object { [int]$_.pdf_bytes -le 0 }).Count
}

function Update-UnitDetails {
    if (-not $script:MonitorData -or -not $script:SelectedUnit) { return }
    $status = @($script:MonitorData.status | Where-Object { $_.code -eq $script:SelectedUnit }) | Select-Object -First 1
    if (-not $status) { return }
    $script:UnitCnpj.Text = "CNPJ: $($status.tax_id)"
    if ($status.history_target_nsu) {
        $script:CursorLabel.Text = "Backfill NSU: $($status.next_nsu) de $($status.history_target_nsu)"
        $script:CursorLabel.ForeColor = [System.Drawing.Color]::FromArgb(36, 105, 180)
    } else {
        $historyText = if ($status.history_backfilled_at) { " | Historico completo" } else { "" }
        $script:CursorLabel.Text = "Proximo NSU: $($status.next_nsu)  |  Ultimo: $($status.last_processed_nsu)$historyText"
        $script:CursorLabel.ForeColor = [System.Drawing.Color]::FromArgb(45, 55, 72)
    }
    $script:LastSuccessLabel.Text = "Ultima consulta: $(ConvertTo-LocalDisplay $status.last_success_at)  |  HTTP: $($status.last_http_status)"
    $script:MetricLabels["errors"].Text = [string]$status.consecutive_errors

    $certificateMetadata = @($script:MonitorData.certificates | Where-Object { $_.unit_code -eq $script:SelectedUnit }) | Select-Object -First 1
    $certificate = Get-CertificateDetails $certificateMetadata
    if ($certificate) {
        $days = [Math]::Floor(($certificate.NotAfter - (Get-Date)).TotalDays)
        $script:CertificateLabel.Text = "Certificado A1 instalado - valido ate $($certificate.NotAfter.ToString('dd/MM/yyyy HH:mm')) ($days dias)"
        $script:CertificateLabel.ForeColor = if ($days -le 30) {
            [System.Drawing.Color]::FromArgb(190, 90, 20)
        } else {
            [System.Drawing.Color]::FromArgb(24, 130, 76)
        }
    } else {
        $script:CertificateLabel.Text = "Certificado nao encontrado no perfil atual"
        $script:CertificateLabel.ForeColor = [System.Drawing.Color]::FromArgb(190, 45, 45)
    }
}

function Update-Log {
    $applicationLines = @()
    if ($script:MonitorData -and $script:MonitorData.log_path) {
        $logPath = [string]$script:MonitorData.log_path
        if (Test-Path -LiteralPath $logPath -PathType Leaf) {
            $applicationLines = @(Get-Content -LiteralPath $logPath -Tail 110 -Encoding UTF8 -ErrorAction SilentlyContinue)
        } else {
            $applicationLines = @("Log ainda nao criado: $logPath")
        }
    }

    $consoleLines = @($script:ActivityLines | Select-Object -Last 55)
    if ($script:MonitorData -and [bool]$script:MonitorData.collector_running) {
        $script:LogBox.Lines = @(
            "=== COMANDOS E ATIVIDADE AO VIVO ==="
            $consoleLines
            ""
            "=== CONSULTAS REAIS DO COLETOR ==="
            $applicationLines
        )
    } else {
        $script:LogBox.Lines = @(
            "=== LOG DO COLETOR ==="
            $applicationLines
            ""
            "=== COMANDOS E ATIVIDADE AO VIVO ==="
            $consoleLines
        )
    }
    $script:LogBox.SelectionStart = $script:LogBox.TextLength
    $script:LogBox.ScrollToCaret()
}

function Update-Monitor {
    if ($script:Refreshing) { return }
    $script:Refreshing = $true
    try {
        Add-ActivityLine "> taxlink-nfse --config config.toml monitor-data --limit 1000"
        $data = Invoke-CollectorJson @("monitor-data", "--limit", "1000")
        $data.database_path = Resolve-ReportedPath ([string]$data.database_path) "data\taxlink-nfse.sqlite3"
        $data.log_path = Resolve-ReportedPath ([string]$data.log_path) "logs\taxlink-nfse.log"
        $script:MonitorData = $data

        Restore-TrackedProcess

        if ($script:CollectorProcess -and $script:CollectorProcess.HasExited) {
            Add-ActivityLine ("[PROCESSO] coletor finalizado com codigo {0}" -f $script:CollectorProcess.ExitCode)
            $script:CollectorProcess = $null
            Remove-TrackedProcess
        }
        if ($script:OnceProcess -and $script:OnceProcess.HasExited) {
            Add-ActivityLine ("[PROCESSO] coleta avulsa finalizada com codigo {0}" -f $script:OnceProcess.ExitCode)
            $script:OnceProcess = $null
            Remove-TrackedProcess
        }

        if ($script:BackfillProcess -and $script:BackfillProcess.HasExited) {
            $backfillExitCode = $script:BackfillProcess.ExitCode
            Add-ActivityLine ("[HISTORICO] processo finalizado com codigo {0}" -f $backfillExitCode)
            if ($backfillExitCode -eq 0) { $script:DateFilterChanged = $false }
            $script:BackfillProcess = $null
            Remove-TrackedProcess
        }

        $existingSelection = $script:SelectedUnit
        $unitCodes = @($data.status | ForEach-Object { [string]$_.code })
        $currentItems = @($script:UnitCombo.Items | ForEach-Object { [string]$_ })
        if (($unitCodes -join "|") -ne ($currentItems -join "|")) {
            $script:UnitCombo.Items.Clear()
            foreach ($unitCode in $unitCodes) { [void]$script:UnitCombo.Items.Add($unitCode) }
        }
        if ($existingSelection -and $unitCodes -contains $existingSelection) {
            $script:UnitCombo.SelectedItem = $existingSelection
        } elseif ($script:UnitCombo.Items.Count -gt 0) {
            $script:UnitCombo.SelectedIndex = 0
            $script:SelectedUnit = [string]$script:UnitCombo.SelectedItem
        }

        $running = [bool]$data.collector_running
        $script:StatusLabel.Text = if ($running) { "EXECUTANDO" } else { "PARADO" }
        $script:StatusLabel.ForeColor = if ($running) {
            [System.Drawing.Color]::FromArgb(22, 140, 78)
        } else {
            [System.Drawing.Color]::FromArgb(175, 55, 55)
        }
        $script:UpdatedLabel.Text = "Atualizado: $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss')"
        $script:StartButton.Enabled = -not $running
        $ownedProcesses = @(Get-OwnedProcesses)
        $script:StopButton.Enabled = $running -or $ownedProcesses.Count -gt 0
        $script:CollectButton.Enabled = -not $running
        $script:PeriodButton.Enabled = -not $running
        $script:DatabasePathLabel.Text = "Banco: $($data.database_path)"
        Update-UnitDetails
        Update-InvoiceGrid
        $selectedStatus = @($data.status | Where-Object { $_.code -eq $script:SelectedUnit }) | Select-Object -First 1
        if ($selectedStatus) {
            $nextText = "consulta liberada"
            if ($selectedStatus.next_poll_at) {
                try {
                    $remaining = ([datetimeoffset]::Parse([string]$selectedStatus.next_poll_at)).ToLocalTime() - [datetimeoffset]::Now
                    if ($remaining.TotalSeconds -gt 0) {
                        $nextText = "proxima consulta em {0:00}:{1:00}:{2:00}" -f [Math]::Floor($remaining.TotalHours), $remaining.Minutes, $remaining.Seconds
                    }
                } catch { $nextText = "proxima consulta desconhecida" }
            }
            Add-ActivityLine ("[STATUS] {0} | unidade={1} | proximo NSU={2} | {3}" -f $script:StatusLabel.Text, $script:SelectedUnit, $selectedStatus.next_nsu, $nextText)
        }
        Update-Log
    } catch {
        Add-ActivityLine ("[ERRO] " + $_.Exception.Message)
        $script:StatusLabel.Text = "FALHA NO MONITOR"
        $script:StatusLabel.ForeColor = [System.Drawing.Color]::FromArgb(190, 45, 45)
        $script:UpdatedLabel.Text = $_.Exception.Message
        Update-Log
    } finally {
        $script:Refreshing = $false
    }
}

function Start-Collector {
    if ($script:CollectorProcess -and -not $script:CollectorProcess.HasExited) { return }
    $selectedStatus = @($script:MonitorData.status | Where-Object { $_.code -eq $script:SelectedUnit }) | Select-Object -First 1
    if ($script:DateFilterChanged -and $selectedStatus -and -not $selectedStatus.history_backfilled_at) {
        Add-ActivityLine "[PERIODO] data alterada e historico ainda nao processado; iniciando backfill"
        Start-PeriodSearch
        return
    }
    if (Test-Path -LiteralPath $script:ServiceExe -PathType Leaf) {
        Add-ActivityLine "> taxlink-nfse-service.exe --config config.toml run"
        $script:CollectorProcess = Start-Process -FilePath $script:ServiceExe `
            -ArgumentList @("--config", $script:ConfigPath, "run") `
            -WorkingDirectory $script:ProjectRoot -WindowStyle Hidden -PassThru
        $startedExecutable = $script:ServiceExe
    } elseif (Test-Path -LiteralPath $script:Python -PathType Leaf) {
        Add-ActivityLine "> python -m taxlink_nfse --config config.toml run"
        $previousPythonPath = $env:PYTHONPATH
        $env:PYTHONPATH = $script:SourceRoot
        try {
            $script:CollectorProcess = Start-Process -FilePath $script:Python `
                -ArgumentList @("-m", "taxlink_nfse", "--config", $script:ConfigPath, "run") `
                -WorkingDirectory $script:ProjectRoot -WindowStyle Hidden -PassThru
            $startedExecutable = $script:Python
        } finally {
            $env:PYTHONPATH = $previousPythonPath
        }
    } else {
        throw "Executavel de servico e Python virtual nao encontrados."
    }
    Save-TrackedProcess $script:CollectorProcess "collector" $startedExecutable
    Add-ActivityLine ("[PROCESSO] coletor iniciado com PID {0}" -f $script:CollectorProcess.Id)
    Start-Sleep -Milliseconds 700
    Update-Monitor
}

function Stop-Collector {
    Restore-TrackedProcess
    $ownedProcesses = @(Get-OwnedProcesses)
    if ($ownedProcesses.Count -eq 0) {
        $message = if ($script:MonitorData -and [bool]$script:MonitorData.collector_running) {
            "Existe um coletor em execucao, mas ele nao foi iniciado por esta janela e nao sera encerrado sem identificacao segura."
        } else {
            "Nao ha processo de coleta em execucao para interromper."
        }
        [System.Windows.Forms.MessageBox]::Show($message, "TaxLink NFS-e Monitor", "OK", "Information") | Out-Null
        return
    }
    $details = ($ownedProcesses | ForEach-Object { "$($_.kind) - PID $($_.process.Id)" }) -join "`n"
    $answer = [System.Windows.Forms.MessageBox]::Show(
        "Interromper os processos iniciados por este monitor?`n`n$details`n`nUm backfill interrompido podera ser retomado depois.",
        "TaxLink NFS-e Monitor",
        "YesNo",
        "Question"
    )
    if ($answer -eq "Yes") {
        foreach ($owned in $ownedProcesses) {
            Add-ActivityLine ("[PROCESSO] interrompendo {0}, PID {1}" -f $owned.kind, $owned.process.Id)
            Stop-ProcessTree $owned.process
        }
        $script:CollectorProcess = $null
        $script:OnceProcess = $null
        $script:BackfillProcess = $null
        Remove-TrackedProcess
        Start-Sleep -Milliseconds 500
        Update-Monitor
    }
}

function Start-OneCollection {
    if ($script:OnceProcess -and -not $script:OnceProcess.HasExited) { return }
    if (Test-Path -LiteralPath $script:ServiceExe -PathType Leaf) {
        Add-ActivityLine "> taxlink-nfse-service.exe --config config.toml once --force"
        $script:OnceProcess = Start-Process -FilePath $script:ServiceExe `
            -ArgumentList @("--config", $script:ConfigPath, "once", "--force") `
            -WorkingDirectory $script:ProjectRoot -WindowStyle Hidden -PassThru
        $startedExecutable = $script:ServiceExe
    } else {
        Add-ActivityLine "> python -m taxlink_nfse --config config.toml once --force"
        $previousPythonPath = $env:PYTHONPATH
        $env:PYTHONPATH = $script:SourceRoot
        try {
            $script:OnceProcess = Start-Process -FilePath $script:Python `
                -ArgumentList @("-m", "taxlink_nfse", "--config", $script:ConfigPath, "once", "--force") `
                -WorkingDirectory $script:ProjectRoot -WindowStyle Hidden -PassThru
            $startedExecutable = $script:Python
        } finally {
            $env:PYTHONPATH = $previousPythonPath
        }
    }
    Save-TrackedProcess $script:OnceProcess "once" $startedExecutable
    Add-ActivityLine ("[PROCESSO] coleta avulsa iniciada com PID {0}" -f $script:OnceProcess.Id)
    $script:StatusLabel.Text = "COLETA INICIADA"
    $script:CollectButton.Enabled = $false
}

function Start-PeriodSearch {
    if (-not $script:SelectedUnit) { return }
    if ($script:BackfillProcess -and -not $script:BackfillProcess.HasExited) { return }

    $selectedStatus = @($script:MonitorData.status | Where-Object { $_.code -eq $script:SelectedUnit }) | Select-Object -First 1
    if ($selectedStatus -and $selectedStatus.history_backfilled_at) {
        Add-ActivityLine ("[HISTORICO] NSUs anteriores ja processados em {0}; consultando somente o cursor atual" -f (ConvertTo-LocalDisplay $selectedStatus.history_backfilled_at))
        $script:DateFilterChanged = $false
        Start-OneCollection
        return
    }

    $answer = [System.Windows.Forms.MessageBox]::Show(
        "O ADN pesquisa documentos por NSU, nao diretamente por data de emissao.`n`nPara localizar notas anteriores, o coletor reprocessara os NSUs desde o numero 1 ate o cursor atual. Registros existentes nao serao duplicados. Depois, o periodo selecionado sera aplicado na grade.`n`nDeseja continuar?",
        "Buscar periodo historico",
        "YesNo",
        "Question"
    )
    if ($answer -ne "Yes") { return }

    $arguments = @("--config", $script:ConfigPath, "backfill", "--unit", $script:SelectedUnit, "--from-nsu", "1")
    if (Test-Path -LiteralPath $script:ServiceExe -PathType Leaf) {
        Add-ActivityLine ("> taxlink-nfse-service.exe --config config.toml backfill --unit {0} --from-nsu 1" -f $script:SelectedUnit)
        $script:BackfillProcess = Start-Process -FilePath $script:ServiceExe `
            -ArgumentList $arguments -WorkingDirectory $script:ProjectRoot `
            -WindowStyle Hidden -PassThru
        $startedExecutable = $script:ServiceExe
    } else {
        $previousPythonPath = $env:PYTHONPATH
        $env:PYTHONPATH = $script:SourceRoot
        try {
            Add-ActivityLine ("> python -m taxlink_nfse --config config.toml backfill --unit {0} --from-nsu 1" -f $script:SelectedUnit)
            $script:BackfillProcess = Start-Process -FilePath $script:Python `
                -ArgumentList (@("-m", "taxlink_nfse") + $arguments) `
                -WorkingDirectory $script:ProjectRoot -WindowStyle Hidden -PassThru
            $startedExecutable = $script:Python
        } finally {
            $env:PYTHONPATH = $previousPythonPath
        }
    }
    Save-TrackedProcess $script:BackfillProcess "backfill" $startedExecutable
    Add-ActivityLine ("[HISTORICO] reprocessamento iniciado com PID {0}" -f $script:BackfillProcess.Id)
    $script:StatusLabel.Text = "BUSCANDO HISTORICO"
    $script:PeriodButton.Enabled = $false
    Update-Log
}

function Show-History {
    if (-not $script:MonitorData) { return }
    $historyForm = New-Object System.Windows.Forms.Form
    $historyForm.Text = "TaxLink NFS-e - Historico de execucoes"
    $historyForm.Size = New-Object System.Drawing.Size(930, 480)
    $historyForm.StartPosition = "CenterParent"
    $historyForm.Font = New-Object System.Drawing.Font("Segoe UI", 9)

    $grid = New-Object System.Windows.Forms.DataGridView
    $grid.Dock = "Fill"
    $grid.ReadOnly = $true
    $grid.AllowUserToAddRows = $false
    $grid.AllowUserToDeleteRows = $false
    $grid.AutoSizeColumnsMode = "Fill"
    $grid.BackgroundColor = [System.Drawing.Color]::White
    $table = New-Object System.Data.DataTable
    foreach ($column in @("ID", "Unidade", "Inicio", "Fim", "Resultado", "Consultas", "Recebidos", "Salvos", "Erro")) {
        [void]$table.Columns.Add($column)
    }
    foreach ($run in $script:MonitorData.runs) {
        $row = $table.NewRow()
        $row["ID"] = $run.id
        $row["Unidade"] = $run.unit_code
        $row["Inicio"] = ConvertTo-LocalDisplay $run.started_at
        $row["Fim"] = ConvertTo-LocalDisplay $run.finished_at
        $row["Resultado"] = $run.result
        $row["Consultas"] = $run.requested_batches
        $row["Recebidos"] = $run.received_documents
        $row["Salvos"] = $run.stored_documents
        $row["Erro"] = $run.error_message
        [void]$table.Rows.Add($row)
    }
    $grid.DataSource = $table
    $historyForm.Controls.Add($grid)
    [void]$historyForm.ShowDialog($script:Form)
}

function Find-SqliteViewer {
    $candidates = @(
        "C:\Program Files\DB Browser for SQLite\DB Browser for SQLite.exe",
        "C:\Program Files (x86)\DB Browser for SQLite\DB Browser for SQLite.exe",
        "C:\Program Files\SQLiteStudio\SQLiteStudio.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\DB Browser for SQLite\DB Browser for SQLite.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    foreach ($commandName in @("sqlitebrowser.exe", "sqlitestudio.exe")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    return $null
}

function Show-DatabaseWindow {
    if (-not $script:MonitorData -or -not $script:MonitorData.database_path) {
        [System.Windows.Forms.MessageBox]::Show(
            "Os dados do monitor ainda nao foram carregados.",
            "Banco SQLite",
            "OK",
            "Information"
        ) | Out-Null
        return
    }

    $databasePath = [string]$script:MonitorData.database_path
    if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
        [System.Windows.Forms.MessageBox]::Show(
            "Banco nao encontrado:`n$databasePath",
            "Banco SQLite",
            "OK",
            "Error"
        ) | Out-Null
        return
    }

    Add-ActivityLine ("[BANCO] abrindo visualizador interno: $databasePath")
    Update-Log
    $databaseFile = Get-Item -LiteralPath $databasePath
    $viewer = Find-SqliteViewer

    $databaseForm = New-Object System.Windows.Forms.Form
    $databaseForm.Text = "TaxLink NFS-e - Banco SQLite"
    $databaseForm.Size = New-Object System.Drawing.Size(1080, 650)
    $databaseForm.MinimumSize = New-Object System.Drawing.Size(900, 520)
    $databaseForm.StartPosition = "CenterParent"
    $databaseForm.BackColor = [System.Drawing.Color]::FromArgb(245, 247, 250)
    $databaseForm.Font = New-Object System.Drawing.Font("Segoe UI", 9)

    $layout = New-Object System.Windows.Forms.TableLayoutPanel
    $layout.Dock = "Fill"
    $layout.Padding = New-Object System.Windows.Forms.Padding(14)
    $layout.RowCount = 4
    $layout.ColumnCount = 1
    $layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Absolute", 46)))
    $layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Absolute", 42)))
    $layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Percent", 100)))
    $layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Absolute", 48)))
    $databaseForm.Controls.Add($layout)

    $databaseTitle = New-Label "Banco consolidado de NFS-e" 16 ([System.Drawing.FontStyle]::Bold) ([System.Drawing.Color]::FromArgb(20, 43, 70))
    $databaseTitle.Dock = "Fill"
    $layout.Controls.Add($databaseTitle, 0, 0)

    $infoPanel = New-Object System.Windows.Forms.Panel
    $infoPanel.Dock = "Fill"
    $pathBox = New-Object System.Windows.Forms.TextBox
    $pathBox.ReadOnly = $true
    $pathBox.Text = $databasePath
    $pathBox.Location = New-Object System.Drawing.Point(0, 6)
    $pathBox.Width = 745
    $infoPanel.Controls.Add($pathBox)
    $sizeLabel = New-Label ("Tamanho: {0:N2} MB | Notas: {1}" -f ($databaseFile.Length / 1MB), @($script:MonitorData.invoices).Count) 9
    $sizeLabel.Location = New-Object System.Drawing.Point(765, 9)
    $infoPanel.Controls.Add($sizeLabel)
    $layout.Controls.Add($infoPanel, 0, 1)

    $databaseGrid = New-Object System.Windows.Forms.DataGridView
    $databaseGrid.Dock = "Fill"
    $databaseGrid.ReadOnly = $true
    $databaseGrid.AllowUserToAddRows = $false
    $databaseGrid.AllowUserToDeleteRows = $false
    $databaseGrid.RowHeadersVisible = $false
    $databaseGrid.SelectionMode = "FullRowSelect"
    $databaseGrid.BackgroundColor = [System.Drawing.Color]::White
    $databaseGrid.AutoSizeColumnsMode = "DisplayedCells"
    $databaseGrid.AlternatingRowsDefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(247, 249, 252)

    $table = New-Object System.Data.DataTable
    foreach ($column in @("ID", "NSU", "Chave de acesso", "Unidade CNPJ", "Fornecedor CNPJ", "Fornecedor", "Emissao", "Valor", "Competencia", "XML bytes", "PDF bytes", "Status PDF", "Contrato ID", "Contrato")) {
        [void]$table.Columns.Add($column)
    }
    foreach ($invoice in $script:MonitorData.invoices) {
        $row = $table.NewRow()
        $row["ID"] = $invoice.id
        $row["NSU"] = $invoice.nsu
        $row["Chave de acesso"] = $invoice.access_key
        $row["Unidade CNPJ"] = $invoice.unit_tax_id
        $row["Fornecedor CNPJ"] = $invoice.provider_tax_id
        $row["Fornecedor"] = $invoice.provider_name
        $row["Emissao"] = $invoice.issued_at
        $row["Valor"] = if ($null -ne $invoice.service_amount_cents) { "R$ {0:N2}" -f ([decimal]$invoice.service_amount_cents / 100) } else { "" }
        $row["Competencia"] = $invoice.competence_date
        $row["XML bytes"] = $invoice.xml_bytes
        $row["PDF bytes"] = $invoice.pdf_bytes
        $row["Status PDF"] = $invoice.danfse_pdf_status
        $row["Contrato ID"] = $invoice.contract_id
        $row["Contrato"] = $invoice.contract_number
        [void]$table.Rows.Add($row)
    }
    $databaseGrid.DataSource = $table
    $databaseGrid.AutoSizeColumnsMode = "None"
    if ($databaseGrid.Columns.Count -gt 0) {
        $databaseGrid.Columns[0].Width = 45
        $databaseGrid.Columns[1].Width = 60
        $databaseGrid.Columns[2].Width = 340
        $databaseGrid.Columns[5].Width = 260
    }
    $layout.Controls.Add($databaseGrid, 0, 2)

    $actions = New-Object System.Windows.Forms.FlowLayoutPanel
    $actions.Dock = "Fill"
    $actions.FlowDirection = "RightToLeft"
    $closeButton = New-Button "Fechar" 90 "Default"
    $copyButton = New-Button "Copiar caminho" 120 "Default"
    $folderButton = New-Button "Mostrar arquivo" 120 "Primary"
    $externalButton = New-Button "Abrir externamente" 135 "Success"
    $externalButton.Enabled = [bool]$viewer
    if (-not $viewer) { $externalButton.Text = "Visualizador nao instalado"; $externalButton.Width = 175 }
    foreach ($button in @($closeButton, $externalButton, $folderButton, $copyButton)) {
        $button.Margin = New-Object System.Windows.Forms.Padding(6, 7, 0, 0)
        $actions.Controls.Add($button)
    }
    $layout.Controls.Add($actions, 0, 3)

    $closeButton.Add_Click({ $databaseForm.Close() })
    $copyButton.Add_Click({
        [System.Windows.Forms.Clipboard]::SetText($databasePath)
    })
    $folderButton.Add_Click({
        Start-Process -FilePath "explorer.exe" -ArgumentList ('/select,"{0}"' -f $databasePath)
    })
    $externalButton.Add_Click({
        if ($viewer) { Start-Process -FilePath $viewer -ArgumentList @($databasePath) }
    })

    [void]$databaseForm.ShowDialog($script:Form)
}

# Janela principal
$script:Form = New-Object System.Windows.Forms.Form
$script:Form.Text = "TaxLink NFS-e - Monitor de coleta"
$script:Form.Size = New-Object System.Drawing.Size(1180, 840)
$script:Form.MinimumSize = New-Object System.Drawing.Size(1050, 720)
$script:Form.StartPosition = "CenterScreen"
$script:Form.BackColor = [System.Drawing.Color]::FromArgb(245, 247, 250)
$script:Form.Font = New-Object System.Drawing.Font("Segoe UI", 9)

$main = New-Object System.Windows.Forms.TableLayoutPanel
$main.Dock = "Fill"
$main.Padding = New-Object System.Windows.Forms.Padding(18, 14, 18, 14)
$main.RowCount = 6
$main.ColumnCount = 1
$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Absolute", 58)))
$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Absolute", 116)))
$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Absolute", 98)))
$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Percent", 54)))
$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Absolute", 28)))
$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Percent", 46)))
$script:Form.Controls.Add($main)

$header = New-Object System.Windows.Forms.Panel
$header.Dock = "Fill"
$title = New-Label "TaxLink NFS-e" 19 ([System.Drawing.FontStyle]::Bold) ([System.Drawing.Color]::FromArgb(20, 43, 70))
$title.Location = New-Object System.Drawing.Point(4, 2)
$header.Controls.Add($title)
$script:StatusLabel = New-Label "CARREGANDO" 9 ([System.Drawing.FontStyle]::Bold) ([System.Drawing.Color]::FromArgb(100, 112, 130))
$script:StatusLabel.Location = New-Object System.Drawing.Point(7, 35)
$header.Controls.Add($script:StatusLabel)
$script:UpdatedLabel = New-Label "" 8.5 ([System.Drawing.FontStyle]::Regular) ([System.Drawing.Color]::FromArgb(100, 112, 130))
$script:UpdatedLabel.Location = New-Object System.Drawing.Point(115, 35)
$header.Controls.Add($script:UpdatedLabel)

$buttons = New-Object System.Windows.Forms.FlowLayoutPanel
$buttons.Dock = "Right"
$buttons.Width = 690
$buttons.FlowDirection = "LeftToRight"
$script:StartButton = New-Button "Iniciar" 95 "Success"
$script:StopButton = New-Button "Interromper" 105 "Danger"
$script:CollectButton = New-Button "Coletar agora" 115 "Primary"
$refreshButton = New-Button "Atualizar" 90 "Default"
$historyButton = New-Button "Historico" 90 "Default"
$openDatabaseButton = New-Button "Abrir banco" 105 "Default"
foreach ($button in @($openDatabaseButton, $historyButton, $refreshButton, $script:CollectButton, $script:StopButton, $script:StartButton)) {
    $button.Margin = New-Object System.Windows.Forms.Padding(5, 8, 0, 0)
    $buttons.Controls.Add($button)
}
$header.Controls.Add($buttons)
$main.Controls.Add($header, 0, 0)

$queryGroup = New-Object System.Windows.Forms.GroupBox
$queryGroup.Text = "Coleta e unidade fiscal"
$queryGroup.Dock = "Fill"
$queryGroup.BackColor = [System.Drawing.Color]::White
$queryGroup.Padding = New-Object System.Windows.Forms.Padding(14)

$unitTitle = New-Label "Unidade" 8.5 ([System.Drawing.FontStyle]::Regular) ([System.Drawing.Color]::FromArgb(100, 112, 130))
$unitTitle.Location = New-Object System.Drawing.Point(18, 25)
$queryGroup.Controls.Add($unitTitle)
$script:UnitCombo = New-Object System.Windows.Forms.ComboBox
$script:UnitCombo.Location = New-Object System.Drawing.Point(18, 45)
$script:UnitCombo.Width = 225
$script:UnitCombo.DropDownStyle = "DropDownList"
$queryGroup.Controls.Add($script:UnitCombo)
$script:UnitCnpj = New-Label "CNPJ: -" 9 ([System.Drawing.FontStyle]::Bold)
$script:UnitCnpj.Location = New-Object System.Drawing.Point(265, 48)
$queryGroup.Controls.Add($script:UnitCnpj)

$startTitle = New-Label "Filtro - emissao inicial" 8.5 ([System.Drawing.FontStyle]::Regular) ([System.Drawing.Color]::FromArgb(100, 112, 130))
$startTitle.Location = New-Object System.Drawing.Point(520, 25)
$queryGroup.Controls.Add($startTitle)
$script:StartDate = New-Object System.Windows.Forms.DateTimePicker
$script:StartDate.Format = "Short"
$script:StartDate.Value = (Get-Date).AddMonths(-3)
$script:StartDate.Location = New-Object System.Drawing.Point(520, 45)
$script:StartDate.Width = 125
$queryGroup.Controls.Add($script:StartDate)
$endTitle = New-Label "Filtro - emissao final" 8.5 ([System.Drawing.FontStyle]::Regular) ([System.Drawing.Color]::FromArgb(100, 112, 130))
$endTitle.Location = New-Object System.Drawing.Point(665, 25)
$queryGroup.Controls.Add($endTitle)
$script:EndDate = New-Object System.Windows.Forms.DateTimePicker
$script:EndDate.Format = "Short"
$script:EndDate.Value = Get-Date
$script:EndDate.Location = New-Object System.Drawing.Point(665, 45)
$script:EndDate.Width = 125
$queryGroup.Controls.Add($script:EndDate)
$script:PeriodButton = New-Button "Buscar periodo" 135 "Primary"
$script:PeriodButton.Location = New-Object System.Drawing.Point(815, 39)
$queryGroup.Controls.Add($script:PeriodButton)

$script:CertificateLabel = New-Label "Certificado: verificando..." 9 ([System.Drawing.FontStyle]::Bold)
$script:CertificateLabel.Location = New-Object System.Drawing.Point(18, 79)
$queryGroup.Controls.Add($script:CertificateLabel)
$script:CursorLabel = New-Label "Proximo NSU: -" 9
$script:CursorLabel.Location = New-Object System.Drawing.Point(440, 79)
$queryGroup.Controls.Add($script:CursorLabel)
$script:LastSuccessLabel = New-Label "Ultima consulta: -" 9
$script:LastSuccessLabel.Location = New-Object System.Drawing.Point(760, 79)
$queryGroup.Controls.Add($script:LastSuccessLabel)
$main.Controls.Add($queryGroup, 0, 1)

$script:MetricLabels = @{}
$metrics = New-Object System.Windows.Forms.TableLayoutPanel
$metrics.Dock = "Fill"
$metrics.ColumnCount = 6
$metrics.RowCount = 1
for ($index = 0; $index -lt 6; $index++) {
    $metrics.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle("Percent", 16.6667)))
}
$metrics.Controls.Add((New-MetricCard "Notas fiscais" "invoices" ([System.Drawing.Color]::FromArgb(40, 110, 190))), 0, 0)
$metrics.Controls.Add((New-MetricCard "XML armazenados" "xml" ([System.Drawing.Color]::FromArgb(32, 150, 105))), 1, 0)
$metrics.Controls.Add((New-MetricCard "DANFSe PDF" "pdf" ([System.Drawing.Color]::FromArgb(118, 82, 180))), 2, 0)
$metrics.Controls.Add((New-MetricCard "Contratos vinculados" "contracts" ([System.Drawing.Color]::FromArgb(225, 145, 45))), 3, 0)
$metrics.Controls.Add((New-MetricCard "PDF pendentes" "pending" ([System.Drawing.Color]::FromArgb(110, 120, 135))), 4, 0)
$metrics.Controls.Add((New-MetricCard "Erros consecutivos" "errors" ([System.Drawing.Color]::FromArgb(195, 60, 60))), 5, 0)
$main.Controls.Add($metrics, 0, 2)

$script:InvoiceGrid = New-Object System.Windows.Forms.DataGridView
$script:InvoiceGrid.Dock = "Fill"
$script:InvoiceGrid.ReadOnly = $true
$script:InvoiceGrid.AllowUserToAddRows = $false
$script:InvoiceGrid.AllowUserToDeleteRows = $false
$script:InvoiceGrid.AllowUserToResizeRows = $false
$script:InvoiceGrid.SelectionMode = "FullRowSelect"
$script:InvoiceGrid.MultiSelect = $false
$script:InvoiceGrid.BackgroundColor = [System.Drawing.Color]::White
$script:InvoiceGrid.BorderStyle = "FixedSingle"
$script:InvoiceGrid.RowHeadersVisible = $false
$script:InvoiceGrid.AlternatingRowsDefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(247, 249, 252)
$script:InvoiceGrid.ColumnHeadersDefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(229, 235, 242)
$script:InvoiceGrid.EnableHeadersVisualStyles = $false
$main.Controls.Add($script:InvoiceGrid, 0, 3)

$script:DatabasePathLabel = New-Label "Banco: -" 8.5 ([System.Drawing.FontStyle]::Regular) ([System.Drawing.Color]::FromArgb(100, 112, 130))
$script:DatabasePathLabel.Dock = "Fill"
$main.Controls.Add($script:DatabasePathLabel, 0, 4)

$script:LogBox = New-Object System.Windows.Forms.RichTextBox
$script:LogBox.Dock = "Fill"
$script:LogBox.ReadOnly = $true
$script:LogBox.BackColor = [System.Drawing.Color]::FromArgb(25, 32, 43)
$script:LogBox.ForeColor = [System.Drawing.Color]::FromArgb(218, 226, 236)
$script:LogBox.Font = New-Object System.Drawing.Font("Consolas", 8.5)
$script:LogBox.WordWrap = $false
$main.Controls.Add($script:LogBox, 0, 5)

$toolTip = New-Object System.Windows.Forms.ToolTip
$toolTip.SetToolTip($openDatabaseButton, "Abre o visualizador interno do SQLite e permite localizar o arquivo.")
$toolTip.SetToolTip($historyButton, "Mostra o resultado dos ultimos ciclos de coleta.")
$toolTip.SetToolTip($refreshButton, "Atualiza imediatamente todos os indicadores.")
$toolTip.SetToolTip($script:CollectButton, "Executa uma consulta imediata, ignorando o horario da proxima verificacao.")
$toolTip.SetToolTip($script:PeriodButton, "Reprocessa os NSUs historicos e aplica o intervalo de emissao na grade.")
$toolTip.SetToolTip($script:StopButton, "Interrompe somente o processo iniciado por esta janela.")
$toolTip.SetToolTip($script:StartButton, "Inicia o coletor continuo em segundo plano.")

$script:UnitCombo.Add_SelectedIndexChanged({
    $script:SelectedUnit = [string]$script:UnitCombo.SelectedItem
    Update-UnitDetails
    Update-InvoiceGrid
})
$script:StartDate.Add_ValueChanged({ $script:DateFilterChanged = $true; Update-InvoiceGrid })
$script:EndDate.Add_ValueChanged({ $script:DateFilterChanged = $true; Update-InvoiceGrid })
$refreshButton.Add_Click({ Update-Monitor })
$script:StartButton.Add_Click({
    try { Start-Collector } catch { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "Erro", "OK", "Error") | Out-Null }
})
$script:StopButton.Add_Click({
    try { Stop-Collector } catch { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "Erro ao interromper", "OK", "Error") | Out-Null }
})
$script:CollectButton.Add_Click({
    try { Start-OneCollection } catch { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "Erro", "OK", "Error") | Out-Null }
})
$script:PeriodButton.Add_Click({
    try { Start-PeriodSearch } catch { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "Erro", "OK", "Error") | Out-Null }
})
$historyButton.Add_Click({ Show-History })
$openDatabaseButton.Add_Click({ Show-DatabaseWindow })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [Math]::Max(2, $RefreshSeconds) * 1000
$timer.Add_Tick({ Update-Monitor })
$script:Form.Add_Shown({
    Update-Monitor
    $timer.Start()
})
$script:Form.Add_FormClosing({
    $timer.Stop()
    $ownedProcesses = @(Get-OwnedProcesses)
    if ($ownedProcesses.Count -gt 0) {
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "Existem processos iniciados por esta janela. Deseja mante-los executando em segundo plano?",
            "TaxLink NFS-e Monitor",
            "YesNo",
            "Question"
        )
        if ($answer -eq "No") {
            foreach ($owned in $ownedProcesses) {
                try { Stop-ProcessTree $owned.process } catch { }
            }
            Remove-TrackedProcess
        }
    }
})

[void][System.Windows.Forms.Application]::Run($script:Form)
