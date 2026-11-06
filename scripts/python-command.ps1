$ErrorActionPreference = "Stop"
$Launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
if ($Launcher) {
    Write-Output $Launcher.Source
    Write-Output '-3'
} else {
    $Interpreter = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
    if (-not $Interpreter) { throw 'Install Python 3.11 or later first.' }
    Write-Output $Interpreter.Source
}
