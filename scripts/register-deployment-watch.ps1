[CmdletBinding(DefaultParameterSetName='Register')]
param(
    [Parameter(ParameterSetName='Preview')][switch]$Preview,
    [Parameter(ParameterSetName='Check')][switch]$CheckOnly,
    [Parameter(ParameterSetName='Session')][switch]$WatchSession
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $Preview -and ($sid -notmatch '^S-1-(5-21|12-1)-' -or
    $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))) {
    throw 'Run from the unelevated human operator session that will own the task.'
}
$root = 'C:\ProgramData\AI-Agent-Office'
$controller = "$root\operator"
$python = "$controller\Scripts\python.exe"
$module = '-I -m agent_office.windows_auto_task'
$taskName = 'AgentOfficeStudio-DeploymentWatch'

if (-not $CheckOnly -and -not $WatchSession) {
    # The root folder is fixed and this task never replaces another definition.
    $scheduler = New-Object -ComObject Schedule.Service
    $scheduler.Connect()
    $definition = $scheduler.NewTask(0)
    $definition.RegistrationInfo.Description = 'Evaluate the enrolled AgentOfficeStudio deployment policy every five minutes.'
    $definition.Principal.UserId = $sid
    $definition.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN
    $definition.Principal.RunLevel = 0  # TASK_RUNLEVEL_LUA
    $definition.Settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
    $definition.Settings.StartWhenAvailable = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    # An interrupted deployment may require recovery. Do not kill one at an arbitrary deadline.
    $definition.Settings.ExecutionTimeLimit = 'PT0S'
    $trigger = $definition.Triggers.Create(1) # TASK_TRIGGER_TIME
    $trigger.StartBoundary = [DateTime]::Now.AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
    $trigger.Repetition.Interval = 'PT5M'
    $action = $definition.Actions.Create(0) # TASK_ACTION_EXEC
    $action.Path = $python
    $action.Arguments = $module
    $action.WorkingDirectory = $controller
    if ($Preview) {
        $definition.XmlText
        return
    }
}

$trustedSids = @($sid, 'S-1-5-18', 'S-1-5-32-544',
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464') # TrustedInstaller
function Assert-TrustedPath([string]$Path, [switch]$ParentOnly) {
    $item = Get-Item -Force -LiteralPath $Path
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Unexpected reparse point: $Path"
    }
    $acl = Get-Acl -LiteralPath $Path
    $raw = [Security.AccessControl.RawSecurityDescriptor]::new($acl.GetSecurityDescriptorBinaryForm(), 0)
    if ($null -eq $raw.DiscretionaryAcl) { throw "Missing DACL: $Path" }
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trustedSids) {
        throw "Unexpected owner: $Path"
    }
    # A parent may allow creating siblings, but never removal/replacement of the pinned child.
    $writes = if ($ParentOnly) { 0x500D0040 } else { 0x500D0156 }
    foreach ($ace in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($ace.AccessControlType -ne 'Allow') { continue }
        $inheritOnly = $ace.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly
        if ($inheritOnly -and ($ParentOnly -or $ace.IdentityReference.Value -eq 'S-1-3-0')) { continue }
        if (($ace.FileSystemRights.value__ -band $writes) -and
            $ace.IdentityReference.Value -notin ($trustedSids + 'S-1-3-4')) {
            throw "Unexpected write permission: $Path"
        }
    }
}
function Assert-TrustedTree([string]$Path) {
    Assert-TrustedPath $Path
    if ([IO.Directory]::Exists($Path)) {
        foreach ($child in [IO.Directory]::EnumerateFileSystemEntries($Path)) {
            Assert-TrustedTree $child
        }
    }
}
Assert-TrustedPath 'C:\' -ParentOnly
Assert-TrustedPath 'C:\ProgramData' -ParentOnly
Assert-TrustedPath $root
Assert-TrustedTree $controller
$sitePackages = "$controller\Lib\site-packages"
if (-not [IO.File]::Exists("$sitePackages\agent_office\windows_auto_task.py") -or
    @(Get-ChildItem -LiteralPath $sitePackages -Filter '*.egg-link').Count) {
    throw 'Install the controller from a reviewed wheel; editable packages are not supported.'
}
# These startup files run before a Python module can validate its own location.
# Only allow the normal wheel/venv bootstraps, and only paths inside the protected venv.
foreach ($pth in Get-ChildItem -LiteralPath $sitePackages -Filter '*.pth') {
    if ($pth.Name -notin @('pywin32.pth', 'distutils-precedence.pth', '_virtualenv.pth')) {
        throw "Unexpected Python startup file: $($pth.Name)"
    }
    foreach ($line in Get-Content -LiteralPath $pth.FullName) {
        $entry = $line.Trim()
        if (-not $entry -or $entry.StartsWith('#') -or $entry -match '^import\s') { continue }
        if ([IO.Path]::IsPathRooted($entry) -or
            -not [IO.Path]::GetFullPath("$sitePackages\$entry").StartsWith("$sitePackages\", [StringComparison]::OrdinalIgnoreCase)) {
            throw "External Python startup path in $($pth.Name)"
        }
    }
}
Assert-TrustedPath "$root\python-install"
$config = Get-Content -LiteralPath "$controller\pyvenv.cfg" -Raw
$base = [regex]::Matches($config, '(?im)^home = (C:\\ProgramData\\AI-Agent-Office\\python-install\\cpython-[\w.-]+)\s*$')
if ($base.Count -ne 1 -or $config -notmatch '(?im)^include-system-site-packages = false\s*$') {
    throw 'Create a non-system-site-packages venv with a versioned Python under the trusted python-install folder.'
}
Assert-TrustedTree $base[0].Groups[1].Value
foreach ($dir in @("$root\release", "$root\deploy")) {
    Assert-TrustedTree $dir
}
foreach ($tool in @('C:\Program Files\Git\cmd\git.exe', 'C:\Program Files\GitHub CLI\gh.exe',
                    'C:\Program Files\Docker\Docker\resources\bin\docker.exe')) {
    for ($path = $tool; $path -ne 'C:\'; $path = [IO.Path]::GetDirectoryName($path)) {
        Assert-TrustedPath $path
    }
}
Push-Location -LiteralPath $controller
try {
    & $python -I -m agent_office.windows_auto_task --preflight
    if ($LASTEXITCODE -ne 0) { throw 'Operator preflight failed. Task was not registered.' }
} finally { Pop-Location }
if ($CheckOnly) {
    Write-Output 'Operator paths, credentials, service binding and access checks passed.'
    return
}
if ($WatchSession) {
    & $python -I -m agent_office.windows_auto_task --watch
    if ($LASTEXITCODE -ne 0) { throw "Policy watch exited with code $LASTEXITCODE." }
    return
}
$taskFolder = $scheduler.GetFolder('\')
# TASK_CREATE; no password, overwrite, service account or highest-privilege fallback.
$taskAcl = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;$sid)"
$registered = $taskFolder.RegisterTaskDefinition($taskName, $definition, 2, $sid, $null, 3, $taskAcl)
Write-Output "Registered $($registered.Path) for $sid."
