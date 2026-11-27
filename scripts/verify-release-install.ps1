# Invoke only inside the disposable hosted VM matrix.
param(
    [Parameter(Mandatory)][ValidateSet('clean', 'upgrade')][string] $Scenario,
    [Parameter(Mandatory)][string] $Version,
    [Parameter(Mandatory)][string] $ReleaseDirectory
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
if ($env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:GITHUB_ACTIONS -ne 'true') {
    throw 'Native release acceptance requires a disposable GitHub-hosted Actions runner.'
}
$evidence = Join-Path $env:RUNNER_TEMP 'office-install-journey'
$baseline = Join-Path $env:RUNNER_TEMP 'office-baseline'
if ($IsWindows) {
    $shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'AI Agent Office.lnk'
    if (Test-Path -LiteralPath $shortcut) { throw 'Expected a clean Windows runner.' }
    $candidate = Join-Path $ReleaseDirectory "AI-Agent-Office-Setup-$Version.exe"
    $signature = Get-AuthenticodeSignature -LiteralPath $candidate
    $status = $signature.Status.ToString()
    $record = [ordered]@{
        installer = [IO.Path]::GetFileName($candidate)
        sha256 = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        authenticodeStatus = $status
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:RUNNER_TEMP 'office-authenticode.json') -Encoding utf8
    Write-Host "Draft installer Authenticode status: $status"
    if ($status -ne 'NotSigned') { throw "Release notes declare an unsigned installer, observed Authenticode status $status." }
} elseif ($IsLinux) {
    $executable = '/usr/bin/ai-agent-office-desktop'
    if (Test-Path -LiteralPath $executable) { throw 'Expected a clean Ubuntu runner.' }
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends xvfb xauth
    $candidate = Join-Path $ReleaseDirectory "ai-agent-office-desktop_${Version}_amd64.deb"
} else {
    throw 'Unsupported OS.'
}

function Install-Desktop([string] $Installer) {
    if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) { throw "Missing installer $Installer" }
    Write-Host "Installing $Installer"
    if ($IsWindows) {
        $process = Start-Process -FilePath $Installer -ArgumentList '/S' -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "NSIS exited with $($process.ExitCode)" }
        $target = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut).TargetPath
    } else {
        sudo apt-get install -y --no-install-recommends $Installer | Out-Host
        $target = $executable
    }
    return (Get-Item -LiteralPath $target).FullName
}

function Invoke-DesktopCheck([string[]] $Arguments) {
    if ($IsLinux) { xvfb-run -a node @Arguments } else { node @Arguments }
}

$env:OFFICE_VERIFY_DIRECTORY = $evidence
if ($Scenario -eq 'clean') {
    $env:OFFICE_VERIFY_VERSION = $Version
    $installed = Install-Desktop $candidate
    Invoke-DesktopCheck @('desktop/verify-install.mjs', $installed)
} else {
    $receipt = Get-Content -LiteralPath (Join-Path $baseline 'baseline.json') -Raw | ConvertFrom-Json
    if ($receipt.version -notin @('0.8.1','0.9.0')) { throw "Unexpected upgrade baseline version $($receipt.version)" }
    $old = Join-Path $baseline $receipt.historical_artifact.installer
    $configHome = if ($IsWindows) { $env:APPDATA } elseif ($env:XDG_CONFIG_HOME) {
        $env:XDG_CONFIG_HOME
    } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.config' }
    $env:OFFICE_VERIFY_DEFAULT_PROFILE = Join-Path $configHome 'AI Agent Office'
    if (Test-Path -LiteralPath $env:OFFICE_VERIFY_DEFAULT_PROFILE) {
        throw "The disposable runner already has a desktop profile at $env:OFFICE_VERIFY_DEFAULT_PROFILE"
    }
    $installed = Install-Desktop $old
    $env:OFFICE_VERIFY_VERSION = $receipt.version
    $env:OFFICE_VERIFY_BASELINE_VERSION = $receipt.version
    Invoke-DesktopCheck @('desktop/verify-install.mjs', $installed)
    # Keep the installation and its profile. The journey has stopped its local Studio.
    $upgraded = Install-Desktop $candidate
    Invoke-DesktopCheck @('desktop/verify-upgrade.mjs', $upgraded, $evidence, $Version)
}
