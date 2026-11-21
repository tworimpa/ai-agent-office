param([Parameter(Mandatory)][ValidateSet('connect','handoff','rust-survivor','survivor')][string]$Mode)
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'session.json') -Raw | ConvertFrom-Json
$identity = Join-Path $PSScriptRoot 'evidence/identity'
$handoff = Join-Path $PSScriptRoot 'evidence/handoff'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'The desktop verification process must run with a non-administrator token.'
}
$session = (Get-Process -Id $PID).SessionId
Write-Host "Verifying installed applications in Windows session $session with a non-administrator token ($Mode)."
$env:TEMP = $env:TMP = Join-Path $PSScriptRoot 'temp'
$env:TAURI_DRIVER = Join-Path $PSScriptRoot 'tauri-driver.exe'
$env:TAURI_NATIVE_DRIVER = Join-Path $PSScriptRoot 'msedgedriver.exe'
$env:OFFICE_COMPARISON_PYTHON = $config.python

if ($Mode -eq 'handoff') {
    if ($session -eq 0) {
        Write-Host 'Studio gates repository pytest in service session 0. The regular interactive Windows journey records the handoff.'
        exit 0
    }
    & "$PSScriptRoot/node.exe" "$PSScriptRoot/desktop/comparison/verify-handoff.mjs" $config.electron $config.rust $handoff
} else {
    $receipt = Join-Path $handoff 'journal-receipt.json'
    if (Test-Path -LiteralPath $receipt) { $env:OFFICE_HANDOFF_RECEIPT = $receipt }
    & "$PSScriptRoot/node.exe" "$PSScriptRoot/desktop/comparison/verify-connection.mjs" $Mode $config.electron $config.rust $identity
}
exit $LASTEXITCODE
