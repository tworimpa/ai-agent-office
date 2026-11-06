param([switch]$Live)
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

py -3 -m venv .venv
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
  & $PythonExe -m agent_office.cli doctor --require claude codex
  if ($LASTEXITCODE -ne 0) { throw "Install and authenticate Claude Code and Codex first" }
  & $PythonExe -m agent_office.cli run --repo . --developer claude --task "Read this repository and propose one safe improvement. Do not modify files."
  if ($LASTEXITCODE -ne 0) { throw "Claude Code live smoke failed" }
  & $PythonExe -m agent_office.cli run --repo . --developer codex --task "Review the architecture in read-only mode. Do not modify files."
  if ($LASTEXITCODE -ne 0) { throw "Codex live smoke failed" }
}
