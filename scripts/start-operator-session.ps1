[CmdletBinding(DefaultParameterSetName='Start')]
param(
    [Parameter(Mandatory,ParameterSetName='Start')][ValidatePattern('^[a-f0-9]{32}$')][string]$TargetId,
    [Parameter(Mandatory,ParameterSetName='Start')][ValidatePattern('^[a-f0-9]{32}$')][string]$ReleaseId,
    [Parameter(Mandatory,ParameterSetName='Start')][ValidatePattern('^[a-f0-9]{32}$')][string]$JournalId,
    [Parameter(Mandatory,ParameterSetName='Start')][ValidatePattern('^sha256:[a-f0-9]{64}$')][string]$Image,
    [Parameter(ParameterSetName='Start')][string]$RuntimeArchive,
    [Parameter(Mandatory,ParameterSetName='Start')][ValidatePattern('^[1-9][0-9]{0,18}$')][string]$RuntimeArtifactId,
    [Parameter(Mandatory,ParameterSetName='Start')][ValidatePattern('^[a-f0-9]{64}$')][string]$RuntimeSha256,
    [Parameter(Mandatory,ParameterSetName='Recover')][ValidatePattern('^[a-f0-9]{32}$')][string]$Recover,
    [Parameter(Mandatory,ParameterSetName='Preview')][switch]$Preview
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$system = [Environment]::SystemDirectory
$ps = "$system\WindowsPowerShell\v1.0\powershell.exe"
$python = 'C:\ProgramData\AI-Agent-Office\operator\Scripts\python.exe'
$base = 'C:\Program Files\AI-Agent-Office\SplitSession'
$utf8 = [Text.UTF8Encoding]::new($false,$true)
# These digests bind exactly the text compiled/executed by the elevated process.
# Canonical source is UTF-8, LF line endings. Recompute only after code review.
$pins = @('67FBB68396224571C5AA97BE74DA9A43F0ABE25F73749E3F615A3CDF1BAB5446','0DF27CDA3579E2024FD37E7B176298DCF5CEDCAA5196B1B02214834490891E9A')
$source = @()
foreach ($name in @('split-session-bootstrap.ps1','SplitSessionAcl.cs')) {
    $bytes = $utf8.GetBytes([IO.File]::ReadAllText("$PSScriptRoot\$name",$utf8).Replace("`r`n","`n"))
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','') } finally { $sha.Dispose() }
    if ($hash -cne $pins[$source.Count]) { throw "Reviewed bootstrap digest mismatch: $name" }
    $source += ,$bytes
}
if ($Preview) {
    [pscustomobject]@{service='AgentOfficeStudio';service_mask='0x35';operator='AgentOfficeOperator';persistent_task=$false;
        session_parent=$base;validator=$python;bootstrap_sha256=$pins[0];acl_sha256=$pins[1]} | ConvertTo-Json -Compress
    return
}
$who = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $who.User.Value
if ($PSVersionTable.PSEdition -ne 'Desktop' -or ![Environment]::Is64BitProcess -or
    $sid -notmatch '^S-1-5-21-\d+-\d+-\d+-\d+$' -or
    ([Security.Principal.WindowsPrincipal]::new($who)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Use an unelevated 64-bit Windows PowerShell 5.1 session under the validator account.'
}
# Do not start the CLR elevated with a caller-configured profiler or application domain manager.
foreach ($name in [Environment]::GetEnvironmentVariables().Keys) {
    if ($name -match '^(COR_|CORECLR_|COMPLUS_|APPDOMAIN_MANAGER_|DEVPATH$|DOTNET_STARTUP_HOOKS$)') { throw 'Unsupported CLR startup environment.' }
}
$id = if ($Recover) { $Recover } else { [Guid]::NewGuid().ToString('N') }
$session = "$base\$id"
$archive = $null
if (!$Recover) {
    if (![IO.File]::Exists($python) -or ![IO.File]::Exists('C:\ProgramData\AI-Agent-Office\operator\Lib\site-packages\agent_office\split_windows_session.py')) { throw 'Install the reviewed validator wheel first.' }
    if ($RuntimeArchive) {
        $archive = [IO.File]::Open((Get-Item -LiteralPath $RuntimeArchive).FullName,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        if ($archive.Length -le 0 -or $archive.Length -gt 1GB) { $archive.Dispose(); throw 'Unsupported runtime artifact size.' }
    }
}
$pipeName = "AgentOfficeSetup-$([guid]::NewGuid().ToString('N'))"
$pipeAcl = [IO.Pipes.PipeSecurity]::new()
$pipeAcl.SetAccessRuleProtection($true,$false)
foreach ($identity in @($sid,'S-1-5-32-544','S-1-5-18')) {
    $pipeAcl.AddAccessRule([IO.Pipes.PipeAccessRule]::new([Security.Principal.SecurityIdentifier]::new($identity),'FullControl','Allow'))
}
$pipe = [IO.Pipes.NamedPipeServerStream]::new($pipeName,'Out',1,'Byte','Asynchronous',65536,65536,$pipeAcl)
$eventSecurity = [Security.AccessControl.EventWaitHandleSecurity]::new()
foreach ($identity in @($sid,'S-1-5-32-544','S-1-5-18')) {
    $eventSecurity.AddAccessRule([Security.AccessControl.EventWaitHandleAccessRule]::new([Security.Principal.SecurityIdentifier]::new($identity),'FullControl','Allow'))
}
$created = $false
$event = [Threading.EventWaitHandle]::new($false,'ManualReset',"Local\AgentOfficeStop-$id",[ref]$created,$eventSecurity)
if (!$created) { throw 'Unexpected existing session stop event.' }
$pidStart = [Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().Ticks
$recoverFlag = if ($Recover) { '$true' } else { '$false' }
# This independent source check runs in Windows PowerShell before any payload
# from the unelevated client is interpreted. A successfully verified installation
# also keeps the reviewed launcher and sources under an administrators-only writer ACL.
$attest = @'
function Assert-CacheObject($item) {
  if (!$item.Exists -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'Invalid protected setup cache'}
  $acl=$item.GetAccessControl()
  $trusted=@('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
  if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted){throw 'Untrusted setup cache owner'}
  $sd=[Security.AccessControl.RawSecurityDescriptor]::new($acl.GetSecurityDescriptorBinaryForm(),0)
  if($null -eq $sd.DiscretionaryAcl){throw 'Missing setup cache DACL'}
  foreach($ace in $sd.DiscretionaryAcl){
    if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'Unsupported setup cache DACL'}
    if(($ace.AceFlags -band 8) -and $ace.SecurityIdentifier.Value -eq 'S-1-3-0'){continue}
    if($ace.AceQualifier -eq 'AccessAllowed' -and ($ace.AccessMask -band 0x500d0156) -and $ace.SecurityIdentifier.Value -notin $trusted){throw 'Writable setup cache'}
  }
}
$cache="C:\Program Files\AI-Agent-Office\SplitTools\$($expectedPins[0])-$($expectedPins[1])"
$cached=[IO.Directory]::Exists($cache)
$names=@('split-session-bootstrap.ps1','SplitSessionAcl.cs','start-operator-session.ps1')
if($cached){
  foreach($dir in @('C:\Program Files','C:\Program Files\AI-Agent-Office','C:\Program Files\AI-Agent-Office\SplitTools',$cache)){
    Assert-CacheObject ([IO.DirectoryInfo]::new($dir))
  }
}else{
  [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
  $api='https://api.github.com/repos/tworimpa/ai-agent-office'
  $headers=@{'User-Agent'='AI-Agent-Office-Setup';'Accept'='application/vnd.github+json'}
  $tip=(Invoke-RestMethod -UseBasicParsing -Uri "$api/git/ref/heads/main" -TimeoutSec 20 -MaximumRedirection 0 -Headers $headers).object.sha
  if($tip -cnotmatch '^[a-f0-9]{40}$'){throw 'Missing reviewed main commit'}
}
for($i=0;$i -lt $names.Count;$i++){
  $name=$names[$i]
  if($cached){
    $file=[IO.FileInfo]::new("$cache\$name")
    Assert-CacheObject $file
    $data=[IO.File]::ReadAllBytes($file.FullName)
  }else{
    $meta=Invoke-RestMethod -UseBasicParsing -Uri "$api/contents/scripts/${name}?ref=$tip" -TimeoutSec 20 -MaximumRedirection 0 -Headers $headers
    if($meta.type -cne 'file' -or $meta.path -cne "scripts/$name" -or $meta.encoding -cne 'base64' -or $meta.size -gt 131072){throw 'Invalid official source metadata'}
    $data=[Convert]::FromBase64String($meta.content)
  }
  $canonical=[Text.UTF8Encoding]::new($false,$true).GetString($data).Replace("`r`n","`n")
  if($i -lt 2){
    if(![string]::Equals($canonical,$items[$i],[StringComparison]::Ordinal)){throw 'Privileged payload differs from reviewed official source'}
  }else{ $officialLauncher=$canonical }
}
'@
# All substitutions are validated UUIDs, hashes, numeric identifiers, and a SID.
# Client connects only to a local pipe and explicitly prohibits server impersonation.
$firstStage = @"
`$ErrorActionPreference='Stop'
try {
  `$sys=[Environment]::SystemDirectory
  `$env:PSModulePath="`$sys\WindowsPowerShell\v1.0\Modules"
  `$env:PATH="`$sys;`$sys\WindowsPowerShell\v1.0"
  [Environment]::CurrentDirectory=`$sys
  Set-Location -LiteralPath `$sys
  `$pipe=[IO.Pipes.NamedPipeClientStream]::new('.','$pipeName','In','None','Anonymous')
  `$pipe.Connect(30000)
  `$reader=[IO.BinaryReader]::new(`$pipe)
  `$items=@()
  foreach(`$expected in @('$($pins[0])','$($pins[1])')){
    `$n=`$reader.ReadInt32()
    if(`$n -le 0 -or `$n -gt 131072){throw 'Invalid bootstrap length'}
    `$bytes=`$reader.ReadBytes(`$n)
    `$sha=[Security.Cryptography.SHA256]::Create()
    try{`$hash=[BitConverter]::ToString(`$sha.ComputeHash(`$bytes)).Replace('-','')}finally{`$sha.Dispose()}
    if(`$bytes.Length -ne `$n -or `$hash -cne `$expected){throw 'Bootstrap digest mismatch'}
    `$items+=[Text.UTF8Encoding]::new(`$false,`$true).GetString(`$bytes)
  }
  `$expectedPins=@('$($pins[0])','$($pins[1])')
  $attest
  & ([ScriptBlock]::Create(`$items[0])) '$id' '$sid' '$TargetId' '$ReleaseId' '$JournalId' '$Image' $recoverFlag $PID ([long]$pidStart) `$items[1] '$RuntimeSha256' `$pipe '$RuntimeArtifactId' `$officialLauncher `$items[0] `$expectedPins
  exit 0
}catch{[Console]::Error.WriteLine(`$_.Exception.Message);exit 1}
"@
$elevated = $null
$validator = $null
try {
    $connect = $pipe.BeginWaitForConnection($null,$null)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($firstStage))
    $elevated = Start-Process -FilePath $ps -ArgumentList "-NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded" -Verb RunAs -WorkingDirectory $system -WindowStyle Hidden -PassThru
    Write-Output "Deployment session $id"
    while (!$connect.AsyncWaitHandle.WaitOne(250)) { if ($elevated.HasExited) { throw 'Administrator setup exited before connecting.' } }
    $pipe.EndWaitForConnection($connect)
    $writer = [IO.BinaryWriter]::new($pipe,[Text.Encoding]::UTF8,$true)
    foreach ($bytes in $source) { $writer.Write([int]$bytes.Length); $writer.Write([byte[]]$bytes) }
    if ($archive) { $writer.Write([long]$archive.Length); $writer.Flush(); $archive.CopyTo($pipe) }
    elseif (!$Recover) { $writer.Write([long]0) }
    $writer.Flush()
    $pipe.Dispose()
    if (!$Recover) {
        while (![IO.File]::Exists("$session\binding.json")) {
            if ($elevated.WaitForExit(250)) { throw "Setup failed. Protected session receipt: $session" }
        }
        $validator = Start-Process -FilePath $python -ArgumentList "-I -m agent_office.split_windows_session validator --session-dir `"$session`"" -WorkingDirectory $system -NoNewWindow -PassThru
        while (!$validator.WaitForExit(250)) { if ($elevated.HasExited) { break } }
        if ($validator.HasExited -and $validator.ExitCode -ne 0) { throw 'Validator exited with an error.' }
    }
} finally {
    [void]$event.Set()
    $pipe.Dispose()
    if ($archive) { $archive.Dispose() }
    if ($elevated) {
        $elevated.WaitForExit()
        if ($elevated.ExitCode -ne 0) { Write-Error "Setup or recovery failed for session $id. Inspect the protected receipt before retrying." -ErrorAction Continue }
    }
    if ($validator) { $validator.WaitForExit(); $validator.Dispose() }
    $event.Dispose()
}
if ($elevated.ExitCode -ne 0) { throw "Administrator session failed: $id" }
