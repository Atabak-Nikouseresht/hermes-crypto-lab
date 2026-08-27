param()
$ErrorActionPreference = "Continue"
$Project = "C:\Users\ataba\hermes-crypto-lab"
$Hermes = "C:\Users\ataba\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe"
$Python = "C:\Users\ataba\hermes-crypto-lab\.venv\Scripts\python.exe"
$Paper = "C:\Users\ataba\hermes-crypto-lab\run_paper.py"
$LogDir = Join-Path $Project "logs"
$Log = Join-Path $LogDir "gateway_watchdog.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
function Write-AuditLog([string]$Message) {
    $Timestamp = [DateTime]::UtcNow.ToString("o")
    Add-Content -Path $Log -Value "$Timestamp $Message"
}
Set-Location $Project
& $Hermes gateway status *> $null
$GatewayCode = $LASTEXITCODE
Write-AuditLog "gateway_status_exit=$GatewayCode"
if ($GatewayCode -ne 0) {
    & $Hermes gateway start *> $null
    $StartCode = $LASTEXITCODE
    Write-AuditLog "gateway_start_exit=$StartCode"
} else {
    $StartCode = 0
}
& $Python $Paper --startup-audit *> $null
$AuditCode = $LASTEXITCODE
Write-AuditLog "startup_audit_exit=$AuditCode"
if ($StartCode -ne 0 -or $AuditCode -ne 0) { exit 1 }
exit 0
