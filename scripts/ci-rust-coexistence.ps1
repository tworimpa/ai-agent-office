$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'This installer check requires a disposable CI runner.' }
$evidence = Join-Path (Get-Location).Path 'comparison-evidence'
$identity = Join-Path $evidence 'identity'
New-Item -ItemType Directory -Force $identity | Out-Null

function Invoke-TestInstaller([string]$installer) {
    $run = Start-Process -FilePath $installer -ArgumentList '/S' -WindowStyle Hidden -PassThru -Wait
    if ($run.ExitCode -ne 0) { throw "Installer failed with exit code $($run.ExitCode)" }
}
function Wait-TestAppRemoval([string]$binary) {
    $deadline = (Get-Date).AddSeconds(45)
    while ((Test-Path -LiteralPath $binary) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
    if (Test-Path -LiteralPath $binary) { throw "Uninstall did not remove test binary: $binary" }
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
$env:OFFICE_COMPARISON_PYTHON = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$electronInstaller = Single-File 'electron-installer' '*Setup*.exe'
Invoke-TestInstaller $electronInstaller
Invoke-TestInstaller (Single-File 'rust-desktop/target/release/bundle/nsis' '*setup.exe')
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'AI Agent Office.lnk'
$electron = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut).TargetPath
$roots = @(Get-ItemProperty 'HKCU:/Software/Microsoft/Windows/CurrentVersion/Uninstall/*' |
    Where-Object DisplayName -eq 'AI Agent Office (Rust 비교판)')
if ($roots.Count -ne 1) { throw 'Missing independent Rust app registration' }
$uninstaller = $roots[0].UninstallString.Trim('"')
$rust = Join-Path (Split-Path -LiteralPath $uninstaller) 'ai-agent-office-rust.exe'
$menus = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('CommonPrograms')) | Select-Object -Unique
$rustLinks = @(Get-ChildItem -LiteralPath $menus -Recurse -File -Filter 'AI Agent Office (Rust 비교판).lnk')
if ($rustLinks.Count -ne 1) {
    throw "Expected one Rust shortcut in $($menus -join ', '), found $($rustLinks.Count)"
}
# Validate the actual Windows Shell launch. A COM shortcut property can contain
# an 8.3 alias or a localized path, even when Explorer opens the correct app.
$probeProfile = Join-Path $env:RUNNER_TEMP ('rust-shortcut-' + [guid]::NewGuid().ToString('n'))
Start-Process -FilePath $rustLinks[0].FullName -ArgumentList "--profile-dir `"$probeProfile`"" -WindowStyle Normal
$deadline = (Get-Date).AddSeconds(25)
do {
    Start-Sleep -Milliseconds 250
    $launched = Get-Process -Name 'ai-agent-office-rust' -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $rust -and $_.MainWindowHandle }
} until ($launched -or (Get-Date) -ge $deadline)
if (!$launched -or !(Test-Path -LiteralPath (Join-Path $probeProfile 'profile.lock'))) {
    throw "Shortcut $($rustLinks[0].FullName) did not open $rust with its verification profile"
}
if (!($launched.CloseMainWindow()) -or !$launched.WaitForExit(20000)) { throw 'Test shortcut window did not close' }
if (!(Test-Path -LiteralPath $rust) -or !(Test-Path -LiteralPath $electron) -or
    (Split-Path -LiteralPath $rust) -eq (Split-Path -LiteralPath $electron)) { throw 'Installer paths overlap or a binary is missing' }
# WebView2 150+ intentionally ignores user debugging arguments in elevated apps.
# Run the production binaries with the runner user's ordinary token. Use that
# user's profile for fixture files, so the checkout and its ACLs are untouched.
$suite = Join-Path $env:LOCALAPPDATA ('office-ci-' + [guid]::NewGuid().ToString('n'))
foreach ($dir in @('temp','evidence','desktop/comparison','desktop/fixtures','desktop/node_modules')) {
    New-Item -ItemType Directory -Force (Join-Path $suite $dir) | Out-Null
}
Copy-Item -Path 'desktop/comparison/*.mjs','desktop/comparison/handoff-worker.py' -Destination "$suite/desktop/comparison"
Copy-Item -LiteralPath 'desktop/fixtures/goal_worker.py' -Destination "$suite/desktop/fixtures"
Copy-Item -LiteralPath 'desktop/studio.mjs','desktop/verify-lifecycle.mjs' -Destination "$suite/desktop"
Copy-Item -LiteralPath 'desktop/node_modules/playwright','desktop/node_modules/playwright-core' -Destination "$suite/desktop/node_modules" -Recurse
Copy-Item -LiteralPath (Get-Command node -CommandType Application | Select-Object -First 1).Source -Destination "$suite/node.exe"
Copy-Item -LiteralPath (Get-Command tauri-driver -CommandType Application | Select-Object -First 1).Source -Destination "$suite/tauri-driver.exe"
Copy-Item -LiteralPath $env:TAURI_NATIVE_DRIVER -Destination "$suite/msedgedriver.exe"
Copy-Item -LiteralPath 'scripts/ci-rust-user-session.ps1' -Destination $suite
@{electron=$electron;rust=$rust;python=$env:OFFICE_COMPARISON_PYTHON} | ConvertTo-Json | Set-Content "$suite/session.json"
$user = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
$launcher = $null
if ($user.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $portable = Join-Path $env:RUNNER_TEMP 'gsudo-v2.6.1.zip'
    Invoke-WebRequest 'https://github.com/gerardog/gsudo/releases/download/v2.6.1/gsudo.portable.zip' -OutFile $portable
    if ((Get-FileHash $portable).Hash -ne '21130BF178D7B9891207F00BFF56F05B6B363CE9CEC7B2D084E45FB12EE51F44') {
        throw 'Portable gsudo 2.6.1 release digest mismatch'
    }
    Expand-Archive -LiteralPath $portable -DestinationPath (Join-Path $driverDir 'gsudo')
    $launcher = Join-Path $driverDir 'gsudo/x64/gsudo.exe'
}
function Invoke-DesktopCheck([string]$mode) {
    $shell = (Get-Command pwsh -CommandType Application | Select-Object -First 1).Source
    $arguments = @('-NoLogo','-NoProfile','-NonInteractive','-File',"$suite/ci-rust-user-session.ps1",$mode)
    Push-Location $suite
    try {
        if ($launcher) { & $launcher --integrity Medium --direct --wait $shell @arguments }
        else { & $shell @arguments }
        if ($LASTEXITCODE) { throw "$mode failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
        Copy-Item -Path "$suite/evidence/*" -Destination $evidence -Recurse -Force
    }
}
Invoke-DesktopCheck connect
Invoke-DesktopCheck handoff
Invoke-TestInstaller (Single-File (Split-Path -LiteralPath $electron) 'Uninstall AI Agent Office.exe')
Wait-TestAppRemoval $electron
Invoke-DesktopCheck rust-survivor
Invoke-TestInstaller $electronInstaller
Invoke-TestInstaller $uninstaller
Wait-TestAppRemoval $rust
if (!(Test-Path -LiteralPath $electron)) { throw 'Uninstall did not isolate the Rust app' }
Invoke-DesktopCheck survivor
