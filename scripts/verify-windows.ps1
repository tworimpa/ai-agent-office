param([switch]$Live)
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

$PythonCommand = @(& (Join-Path $PSScriptRoot 'python-command.ps1'))
$PythonArgs = @($PythonCommand | Select-Object -Skip 1)
& $PythonCommand[0] @PythonArgs -m venv .venv
if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
$PythonExe = Join-Path (Get-Location) ".venv\Scripts\python.exe"
& $PythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $PythonExe -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }
& $PythonExe -m compileall -q src tests
if ($LASTEXITCODE -ne 0) { throw "compileall failed" }
& $PythonExe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "tests failed" }

Write-Host "== Available CLI versions (not required for unit tests) =="
& $PythonExe -m agent_office.cli doctor
if ($Live) {
  & $PythonExe -m agent_office.cli doctor --require codex antigravity
  if ($LASTEXITCODE -ne 0) { throw "Install and authenticate Codex and Antigravity first" }
  & $PythonExe -m agent_office.cli run --repo . --developer codex --task "Read this repository and propose one safe improvement. Do not modify files."
  if ($LASTEXITCODE -ne 0) { throw "Codex live communication smoke failed" }
  & $PythonExe -m agent_office.cli run --repo . --developer antigravity --task "Review the architecture in planning mode. Do not modify files."
  if ($LASTEXITCODE -ne 0) { throw "Antigravity live communication smoke failed" }
}
