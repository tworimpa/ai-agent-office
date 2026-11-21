$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'This installer check requires a disposable CI runner.' }
$evidence = Join-Path (Get-Location).Path 'comparison-evidence'
$identity = Join-Path $evidence 'identity'
New-Item -ItemType Directory -Force $identity | Out-Null

function Install-TestApp([string]$installer) {
    $run = Start-Process -FilePath $installer -ArgumentList '/S' -WindowStyle Hidden -PassThru -Wait
    if ($run.ExitCode -ne 0) { throw "Installer failed with exit code $($run.ExitCode)" }
}
function Single-File([string]$directory, [string]$pattern) {
    $files = @(Get-ChildItem -LiteralPath $directory -Filter $pattern -File)
    if ($files.Count -ne 1) { throw "Expected exactly one $pattern in $directory" }
    return $files[0].FullName
}

$runtime = Get-ChildItem -LiteralPath "${env:ProgramFiles(x86)}/Microsoft/EdgeWebView/Application" -Directory |
    Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
if (!$runtime) { throw 'WebView2 runtime is missing' }
$driverDir = Join-Path $env:RUNNER_TEMP 'office-edge-driver'
New-Item -ItemType Directory $driverDir | Out-Null
$archive = Join-Path $driverDir 'edge.zip'
Invoke-WebRequest -Uri "https://msedgedriver.microsoft.com/$($runtime.Name)/edgedriver_win64.zip" -OutFile $archive
Expand-Archive -LiteralPath $archive -DestinationPath $driverDir
$env:TAURI_NATIVE_DRIVER = Join-Path $driverDir 'msedgedriver.exe'
$signature = Get-AuthenticodeSignature -FilePath $env:TAURI_NATIVE_DRIVER
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft') {
    throw 'Could not verify the Edge driver publisher'
}
Install-TestApp (Single-File 'electron-installer' '*Setup*.exe')
Install-TestApp (Single-File 'rust-desktop/target/release/bundle/nsis' '*setup.exe')
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'AI Agent Office.lnk'
$electron = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut).TargetPath
$roots = @(Get-ItemProperty 'HKCU:/Software/Microsoft/Windows/CurrentVersion/Uninstall/*' |
    Where-Object DisplayName -eq 'AI Agent Office (Rust 비교판)')
if ($roots.Count -ne 1) { throw 'Missing independent Rust app registration' }
$uninstaller = $roots[0].UninstallString.Trim('"')
$rust = Join-Path (Split-Path -LiteralPath $uninstaller) 'ai-agent-office-rust.exe'
if (!(Test-Path -LiteralPath $rust) -or !(Test-Path -LiteralPath $electron) -or
    (Split-Path -LiteralPath $rust) -eq (Split-Path -LiteralPath $electron)) { throw 'Installer paths overlap or a binary is missing' }
node desktop/comparison/verify-connection.mjs connect $electron $rust $identity
if ($LASTEXITCODE) { exit $LASTEXITCODE }
$user = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (!$user.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    node desktop/comparison/verify-handoff.mjs $electron $rust (Join-Path $evidence 'handoff')
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
} else {
    Write-Host 'This runner is elevated. The regular-user Windows acceptance uses the packaged apps for the edit and pytest journey.'
}
Install-TestApp $uninstaller
$deadline = (Get-Date).AddSeconds(40)
while ((Test-Path -LiteralPath $rust) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
if ((Test-Path -LiteralPath $rust) -or !(Test-Path -LiteralPath $electron)) { throw 'Uninstall did not isolate the Rust app' }
node desktop/comparison/verify-connection.mjs survivor $electron $rust $identity
exit $LASTEXITCODE
