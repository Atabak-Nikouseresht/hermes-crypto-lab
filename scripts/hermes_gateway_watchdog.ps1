param()
$ErrorActionPreference = "Continue"
$Project = Split-Path -Parent $PSScriptRoot
$Hermes = Join-Path $env:LOCALAPPDATA "hermes\hermes-agent\venv\Scripts\hermes.exe"
$Python = Join-Path $Project ".venv\Scripts\python.exe"
$Paper = Join-Path $Project "run_paper.py"
$LogDir = Join-Path $Project "logs"
$Log = Join-Path $LogDir "gateway_watchdog.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
function Write-AuditLog([string]$Message) {
    $Timestamp = [DateTime]::UtcNow.ToString("o")
    Add-Content -Path $Log -Value "$Timestamp $Message"
}
function Invoke-AuditedCommand([string]$Label, [string]$Executable, [string[]]$Arguments) {
    if (-not (Test-Path $Executable)) {
        Write-AuditLog "${Label}_exit=127"
        Write-AuditLog "${Label}_failure_detail=executable not found"
        return 127
    }
    try {
        $Output = @(& $Executable @Arguments 2>&1)
        $Code = $LASTEXITCODE
    } catch {
        $Output = @($_.Exception.Message)
        $Code = 1
    }
    Write-AuditLog "${Label}_exit=$Code"
    if ($Code -ne 0) {
        $Safe = $Output | ForEach-Object {
            $Line = [string]$_
            if ($Line -match '(?i)(token|secret|password|telegram|destination|target|api[_ -]?key)') {
                '[REDACTED]'
            } else {
                $Compact = ($Line -replace '\s+', ' ').Trim()
                if ($Compact.Length -gt 300) { $Compact.Substring(0, 300) } else { $Compact }
            }
        }
        $Detail = (($Safe | Where-Object { $_ } | Select-Object -Last 5) -join ' | ')
        if (-not $Detail) { $Detail = '<no output>' }
        Write-AuditLog "${Label}_failure_detail=$Detail"
    }
    return $Code
}
Set-Location $Project
$GatewayCode = Invoke-AuditedCommand "gateway_status" $Hermes @("gateway", "status")
if ($GatewayCode -ne 0) {
    $StartCode = Invoke-AuditedCommand "gateway_start" $Hermes @("gateway", "start")
} else {
    $StartCode = 0
}
$AuditCode = Invoke-AuditedCommand "startup_audit" $Python @($Paper, "--startup-audit")
if ($StartCode -ne 0 -or $AuditCode -ne 0) { exit 1 }
exit 0
