# This file is input to the pinned in-memory launcher. It has no standalone elevated entry point.
param($SessionId, $ValidatorSid, $TargetId, $ReleaseId, $JournalId, $Image, $Recovery, $LauncherPid, $StartTicks, $AclSource, $RuntimeSha256, $Pipe, $RuntimeArtifactId, $OfficialLauncher, $OfficialBootstrap, $ExpectedPins)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:SetupPhase = 'Checking official runtime provenance'
function Assert-ReleaseArtifact([long]$artifactId, [string]$digest) {
    if ($artifactId -le 0 -or $digest -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid GitHub artifact identifier.' }
    $api = 'https://api.github.com/repos/tworimpa/ai-agent-office'
    function Read-ReleaseMetadata([string]$suffix) {
        Invoke-RestMethod -UseBasicParsing -Method Get -Uri "$api/$suffix" -TimeoutSec 20 -MaximumRedirection 0 -Headers @{
            'User-Agent' = 'AI-Agent-Office-Operator'; 'Accept' = 'application/vnd.github+json'
        }
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $artifact = Read-ReleaseMetadata "actions/artifacts/$artifactId"
    $runId = [long]$artifact.workflow_run.id
    if ($artifact.id -ne $artifactId -or $artifact.name -cne 'split-operator-runtime-Windows' -or
        $artifact.expired -ne $false -or $artifact.digest -cne "sha256:$digest" -or $runId -le 0 -or
        $artifact.workflow_run.repository_id -ne 1447735369 -or $artifact.workflow_run.head_repository_id -ne 1447735369) {
        throw 'The supplied archive is not the expected published Windows runtime.'
    }
    $run = Read-ReleaseMetadata "actions/runs/$runId"
    if ($run.workflow_id -ne 424727684 -or $run.path -cne '.github/workflows/desktop.yml' -or
        $run.event -cne 'pull_request' -or $run.status -cne 'completed' -or $run.conclusion -cne 'success' -or
        $run.head_sha -cne $artifact.workflow_run.head_sha -or $run.head_sha -cnotmatch '^[0-9a-f]{40}$' -or
        @($run.referenced_workflows).Count -ne 1 -or
        $run.referenced_workflows[0].ref -cnotmatch '^refs/pull/([1-9][0-9]*)/merge$') {
        throw 'Expected the successful desktop installer workflow on the reviewed pull request.'
    }
    $number = [long]$Matches[1]
    $synthetic = $run.referenced_workflows[0].sha
    if ($synthetic -cnotmatch '^[0-9a-f]{40}$' -or $run.referenced_workflows[0].path -cne
        "tworimpa/ai-agent-office/.github/workflows/tested-merge.yml@$synthetic") { throw 'Unexpected tested ref.' }
    $tip = (Read-ReleaseMetadata 'git/ref/heads/main').object.sha
    $pr = Read-ReleaseMetadata "pulls/$number"
    if ($tip -cnotmatch '^[0-9a-f]{40}$' -or $pr.merged -ne $true -or $pr.merge_commit_sha -cne $tip -or
        $pr.base.ref -cne 'main' -or $pr.head.sha -cne $run.head_sha -or
        $pr.base.repo.id -ne 1447735369 -or $pr.head.repo.id -ne 1447735369) { throw 'Runtime PR is not the current main merge.' }
    $tested = Read-ReleaseMetadata "git/commits/$synthetic"
    $merged = Read-ReleaseMetadata "git/commits/$tip"
    if (@($merged.parents).Count -ne 2 -or @($tested.parents).Count -ne 2 -or
        $tested.sha -cne $synthetic -or $merged.sha -cne $tip -or
        $tested.tree.sha -cne $merged.tree.sha -or $tested.parents[1].sha -cne $run.head_sha -or
        $merged.parents[1].sha -cne $run.head_sha -or $merged.parents[0].sha -cne $tested.parents[0].sha) {
        throw 'The published runtime was built from a different merge tree.'
    }
}
if ($SessionId -cnotmatch '^[a-f0-9]{32}$' -or $ValidatorSid -notmatch '^S-1-(5-21|12-1)-[0-9-]+$' -or
    (!$Recovery -and (@($TargetId,$ReleaseId,$JournalId).Where({$_ -cnotmatch '^[a-f0-9]{32}$'}).Count -or
        $Image -cnotmatch '^sha256:[a-f0-9]{64}$' -or $RuntimeSha256 -cnotmatch '^[a-f0-9]{64}$' -or $RuntimeArtifactId -cnotmatch '^[1-9][0-9]{0,18}$'))) { throw 'Invalid fixed session parameters.' }
$system = [Environment]::SystemDirectory
$root = 'C:\Program Files\AI-Agent-Office'
$base = "$root\SplitSession"
$session = "$base\$SessionId"
$private = "$session\private"
$binding = "$session\binding.json"
$accountName = 'AgentOfficeOperator'
$groupName = 'AO-' + $SessionId.Substring(0,16)
$admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$utf8 = [Text.UTF8Encoding]::new($false)
$module = "$system\WindowsPowerShell\v1.0\Modules\Microsoft.PowerShell.LocalAccounts\Microsoft.PowerShell.LocalAccounts.psd1"
Import-Module -Name $module -ErrorAction Stop
function DirectoryAcl([string]$sddl) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetSecurityDescriptorSddlForm("O:BAD:P(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)$sddl",
        ([Security.AccessControl.AccessControlSections]::Owner -bor [Security.AccessControl.AccessControlSections]::Access))
    return $acl
}
function Assert-Private([string]$path) {
    $entry = [IO.DirectoryInfo]::new($path)
    if (!$entry.Exists -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Unsafe setup directory: $path" }
    $acl = $entry.GetAccessControl()
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin @('S-1-5-32-544','S-1-5-18','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')) { throw "Unsupported setup owner: $path" }
    $raw = [Security.AccessControl.RawSecurityDescriptor]::new($acl.GetSecurityDescriptorBinaryForm(),0)
    if ($null -eq $raw.DiscretionaryAcl) { throw "Missing setup DACL: $path" }
    foreach ($rule in $raw.DiscretionaryAcl) {
        if ($rule -isnot [Security.AccessControl.CommonAce] -or $rule.IsCallback) { throw "Unsupported setup DACL: $path" }
        if (($rule.AceFlags -band [Security.AccessControl.AceFlags]::InheritOnly) -and $rule.SecurityIdentifier.Value -eq 'S-1-3-0') { continue }
        if ($rule.AceQualifier -eq 'AccessAllowed' -and ($rule.AccessMask -band 0x500d0156) -and
            $rule.SecurityIdentifier.Value -notin @('S-1-5-32-544','S-1-5-18','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')) {
            throw "Writable setup path: $path"
        }
    }
}
function PrivateDirectory([string]$path, [string]$extra = '') {
    [void][IO.Directory]::CreateDirectory($path,(DirectoryAcl $extra))
    Assert-Private $path
}
function Save-New([string]$name,[string]$value) {
    $bytes = $utf8.GetBytes($value)
    $f = [IO.FileStream]::new($name,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read,4096,[IO.FileOptions]::WriteThrough)
    try { $f.Write($bytes,0,$bytes.Length); $f.Flush($true) } finally { $f.Dispose() }
}
function Install-Runtime {
    $store = "$root\SplitRuntime"
    PrivateDirectory $store '(A;;0x200a0;;;AU)'
    $dest = "$store\$RuntimeSha256"
    $archive = "$private\runtime.zip"
    $stream = [IO.FileStream]::new($archive,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    try {
        $reader = [IO.BinaryReader]::new($Pipe,[Text.Encoding]::UTF8,$true)
        $size = $reader.ReadInt64()
        if ($size -eq 0 -and $cached) { return }
        if ($size -le 0 -or $size -gt 1GB) { throw 'Invalid runtime archive size.' }
        $buffer = [byte[]]::new(262144)
        while ($size -gt 0) {
            $count = $reader.Read($buffer,0,[int][Math]::Min([long]$buffer.Length,$size))
            if (!$count) { throw 'Incomplete runtime archive.' }
            $stream.Write($buffer,0,$count)
            $size -= $count
        }
        $stream.Position = 0
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $actual = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
        if ($actual -cne $RuntimeSha256) { throw 'Runtime archive does not match the reviewed Actions artifact digest.' }
        if ([IO.Directory]::Exists($dest)) {
            Assert-Private $dest
            if ([IO.File]::ReadAllText("$dest\.split-runtime-digest") -cne "$RuntimeArtifactId $actual") { throw 'Missing protected runtime publication receipt.' }
            return
        }
        $stage = "$store\.stage-$SessionId"
        if ([IO.Directory]::Exists($stage) -or [IO.File]::Exists($stage)) { throw 'Runtime staging path already exists.' }
        [void][IO.Directory]::CreateDirectory($stage,(DirectoryAcl '(A;OICI;0x1200a9;;;BU)'))
        $stream.Position = 0
        Add-Type -AssemblyName System.IO.Compression
        $zip = [IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Read,$true)
        try {
            $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
            $spelling = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::OrdinalIgnoreCase)
            if ($zip.Entries.Count -gt 80000) { throw 'Too many runtime archive entries.' }
            [long]$total = 0
            foreach ($entry in $zip.Entries) {
                $name = $entry.FullName.TrimEnd('/')
                $directory = $entry.FullName.EndsWith('/')
                $kind = ($entry.ExternalAttributes -shr 16) -band 0xf000
                if ($entry.FullName.EndsWith('//') -or !$name -or $entry.Length -gt 512MB -or
                    ($entry.ExternalAttributes -band 0x400) -or $kind -notin @(0,0x4000,0x8000) -or
                    ($directory -and $entry.Length) -or !$seen.Add($name)) { throw 'Unsafe runtime archive entry.' }
                $parts = $name.Split('/')
                $relative = ''
                foreach ($part in $parts) {
                    if (!$part -or $part -match '[<>:"\\|?*~\x00-\x1f]|[ .]$' -or
                        $part -match '^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(\.|$)' -or
                        $part -ieq '.split-runtime-digest' -or $part -cne $part.Normalize()) {
                        throw 'Unsafe runtime archive path.'
                    }
                    $relative = if ($relative) { "$relative\$part" } else { $part }
                    if ($spelling.ContainsKey($relative) -and $spelling[$relative] -cne $relative) { throw 'Runtime path collision.' }
                    $spelling[$relative] = $relative
                }
                $total += $entry.Length
                if ($total -gt 3GB) { throw 'Unpacked runtime exceeds limit.' }
                $path = [IO.Path]::GetFullPath("$stage\$relative")
                if (!$path.StartsWith($stage+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime path escaped staging.' }
                if ($directory) { [void][IO.Directory]::CreateDirectory($path); continue }
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
                $output = [IO.File]::Open($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
                try {
                    $inputStream = $entry.Open()
                    try {
                        [long]$remaining = $entry.Length
                        while ($remaining -gt 0) {
                            $count = $inputStream.Read($buffer,0,[int][Math]::Min([long]$buffer.Length,$remaining))
                            if (!$count) { throw 'Truncated runtime file.' }
                            $output.Write($buffer,0,$count)
                            $remaining -= $count
                        }
                        if ($inputStream.ReadByte() -ne -1) { throw 'Runtime file exceeds advertised length.' }
                    } finally { $inputStream.Dispose() }
                } finally { $output.Dispose() }
            }
            foreach ($required in @('python.exe','Lib\site-packages\agent_office\split_windows_session.py')) {
                if (![IO.File]::Exists("$stage\$required")) { throw "Missing runtime component: $required" }
            }
            Save-New "$stage\.split-runtime-digest" "$RuntimeArtifactId $actual"
            [IO.Directory]::Move($stage,$dest)
        } finally { $zip.Dispose() }
    } finally { $stream.Dispose(); [IO.File]::Delete($archive) }
}
# Built-in, full Windows paths only. All compiler inputs and outputs live in the
# administrators-only directory. Invoke the compiler directly, without response files.
Assert-Private 'C:\Program Files'
PrivateDirectory $root '(A;;0x200a0;;;AU)'
PrivateDirectory $base '(A;;0x200a0;;;AU)'
if (!$Recovery) {
    $store = "$root\SplitRuntime"
    $cached = $false
    if ([IO.Directory]::Exists($store)) {
        Assert-Private $store
        $existing = "$store\$RuntimeSha256"
        if ([IO.Directory]::Exists($existing)) {
            Assert-Private $existing
            $cached = [IO.File]::ReadAllText("$existing\.split-runtime-digest") -ceq "$RuntimeArtifactId $RuntimeSha256"
            if (!$cached) { throw 'Unrecognized protected runtime receipt.' }
        }
    }
    if (!$cached) { Assert-ReleaseArtifact $RuntimeArtifactId $RuntimeSha256 }
}
$lock = [IO.FileStream]::new("$base\session.lock",[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try {
    $tools = "$root\SplitTools"
    PrivateDirectory $tools '(A;;0x200a0;;;AU)'
    $version = "$tools\$($ExpectedPins[0])-$($ExpectedPins[1])"
    if (![IO.Directory]::Exists($version)) {
        $newTools = "$tools\.stage-$([Guid]::NewGuid().ToString('N'))"
        [void][IO.Directory]::CreateDirectory($newTools,(DirectoryAcl '(A;OICI;0x1200a9;;;BU)'))
        Save-New "$newTools\start-operator-session.ps1" $OfficialLauncher
        Save-New "$newTools\split-session-bootstrap.ps1" $OfficialBootstrap
        Save-New "$newTools\SplitSessionAcl.cs" $AclSource
        [IO.Directory]::Move($newTools,$version)
    }
    if ($Recovery) {
        Assert-Private $session
        Assert-Private $private
    } else {
        $script:SetupPhase = 'Installing verified runtime archive'
        if ([IO.Directory]::Exists($session) -or [IO.File]::Exists($session)) { throw 'A session ID cannot be reused.' }
        PrivateDirectory $session "(A;OICI;0x1200a9;;;$ValidatorSid)"
        PrivateDirectory $private
        Install-Runtime
    }
    $env:TEMP = $private
    $env:TMP = $private
    [Environment]::CurrentDirectory = $private
    Set-Location -LiteralPath $private
    $sourceFile = "$private\source-$([Guid]::NewGuid().ToString('N')).cs"
    $script:SetupPhase = 'Building verified Windows ACL helper'
    $assembly = "$private\acl-$([Guid]::NewGuid().ToString('N')).dll"
    Save-New $sourceFile $AclSource
    $framework = "$([IO.Directory]::GetParent($system).FullName)\Microsoft.NET\Framework64\v4.0.30319"
    & "$framework\csc.exe" /nologo /noconfig /target:library "/reference:$framework\System.dll" "/reference:$framework\System.Core.dll" "/out:$assembly" $sourceFile
    if ($LASTEXITCODE -ne 0) { throw 'Could not build the reviewed ACL helper.' }
    [void][Reflection.Assembly]::LoadFrom($assembly)
    [IO.File]::Delete($sourceFile)
    $stateFile = "$private\account.json"
    $intentFile = "$private\setup-intent.json"
    $aclHelper = $null
    $launchProcess = $null
    $createdUser = $null
    $createdGroup = $null
    $unrestored = [Collections.Generic.List[string]]::new()
    try {
        if ($Recovery) {
            if ([IO.File]::Exists("$private\completed")) { return }
            if ([IO.File]::Exists($stateFile)) {
                $state = [IO.File]::ReadAllText($stateFile) | ConvertFrom-Json
                $createdUser = $state.operator_sid
                $createdGroup = $state.session_group_sid
                $ValidatorSid = $state.validator_sid
                $aclHelper = [SplitSessionAcl]::new("$private\dacl.log",$createdGroup,$ValidatorSid)
            } elseif ([IO.File]::Exists($intentFile)) {
                # Setup may have stopped between SAM creation and recording both SIDs.
                $user = Get-LocalUser -Name $accountName -ErrorAction SilentlyContinue
                $group = Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue
                if ($user -and $user.Description -ceq "AgentOffice split session $SessionId") { $createdUser=$user.SID.Value }
                if ($group -and $group.Description -ceq "AgentOffice split session $SessionId") { $createdGroup=$group.SID.Value }
            } else { return } # No host grants or temporary accounts could have been created.
        } else {
            $script:SetupPhase = 'Checking launcher and provisioning isolated local account'
            $launchProcess = [Diagnostics.Process]::GetProcessById($LauncherPid)
            if ($launchProcess.StartTime.ToUniversalTime().Ticks -ne $StartTicks -or $launchProcess.HasExited) { throw 'Launching process no longer exists.' }
            $launcher = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$([int]$LauncherPid)" -OperationTimeoutSec 10
            $owner = Invoke-CimMethod -InputObject $launcher -MethodName GetOwnerSid -OperationTimeoutSec 10
            if ($owner.ReturnValue -ne 0 -or $owner.Sid -cne $ValidatorSid -or $launchProcess.HasExited) { throw 'Launcher token differs from the authorized validator.' }
            $stop = [Threading.EventWaitHandle]::OpenExisting("Local\AgentOfficeStop-$SessionId",[Security.AccessControl.EventWaitHandleRights]::Synchronize)
            $serviceKey = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Services\AgentOfficeStudio')
            try {
                if (!$serviceKey -or $serviceKey.GetValue('ObjectName') -ine 'NT SERVICE\AgentOfficeStudio') { throw 'Expected the dedicated AgentOfficeStudio service.' }
            } finally { if ($serviceKey) { $serviceKey.Dispose() } }
            if (Get-LocalUser -Name $accountName -ErrorAction SilentlyContinue) { throw 'Existing AgentOfficeOperator account. Recover the previous session or use a clean host.' }
            if (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue) { throw 'Unexpected existing session group.' }
            Save-New $intentFile (@{validator_sid=$ValidatorSid;session=$SessionId} | ConvertTo-Json -Compress)
            $password = [Security.SecureString]::new()
            $entropy = [byte[]]::new(64)
            $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
            try {
                $rng.GetBytes($entropy)
                foreach ($char in 'aA1!'.ToCharArray()) { $password.AppendChar($char) }
                foreach ($byte in $entropy) { $password.AppendChar([char](33 + $byte % 94)) }
                $password.MakeReadOnly()
            } finally { [Array]::Clear($entropy,0,$entropy.Length); $rng.Dispose() }
            $createdUser = (New-LocalUser -Name $accountName -Password $password -Description "AgentOffice split session $SessionId").SID.Value
            $createdGroup = (New-LocalGroup -Name $groupName -Description "AgentOffice split session $SessionId").SID.Value
            Save-New $stateFile (@{operator_sid=$createdUser;session_group_sid=$createdGroup;validator_sid=$ValidatorSid} | ConvertTo-Json -Compress)
            Add-LocalGroupMember -Name $groupName -Member ([Security.Principal.SecurityIdentifier]::new($createdUser))
            $aclHelper = [SplitSessionAcl]::new("$private\dacl.log",$createdGroup,$ValidatorSid)
            foreach ($previous in [IO.Directory]::EnumerateDirectories($base)) {
                if ($previous -eq $session -or [IO.Path]::GetFileName($previous) -cnotmatch '^[a-f0-9]{32}$') { continue }
                Assert-Private $previous
                Assert-Private "$previous\private"
                if (![IO.File]::Exists("$previous\private\account.json")) { continue }
                if (![IO.File]::Exists("$previous\private\completed")) { throw "Recover previous session first: $previous" }
                $old = [IO.File]::ReadAllText("$previous\private\account.json") | ConvertFrom-Json
                if (Get-LocalUser -SID ([Security.Principal.SecurityIdentifier]::new($old.operator_sid)) -ErrorAction SilentlyContinue) { throw 'Previous operator account is still present.' }
                if (Get-LocalGroup -SID ([Security.Principal.SecurityIdentifier]::new($old.session_group_sid)) -ErrorAction SilentlyContinue) { throw 'Previous session group is still present.' }
                $aclHelper.AcceptRetiredSession($old.operator_sid,$old.session_group_sid)
            }
            $script:SetupPhase = 'Granting scoped file and service rights'
            $aclHelper.Provision()
            $aclHelper.Service($false)
            $readRules = "(A;OICI;0x1200a9;;;$ValidatorSid)(A;OICI;0x1200a9;;;$createdGroup)"
            [IO.Directory]::SetAccessControl($session,(DirectoryAcl $readRules))
            Assert-Private $session
            $channelRules = "(A;;0x1201bf;;;$ValidatorSid)(A;OICIIO;0x1301bf;;;$ValidatorSid)(A;OICI;0x1200a9;;;$createdGroup)"
            [void][IO.Directory]::CreateDirectory("$session\channel",(DirectoryAcl $channelRules))
            # The operator may list/create files and modify or atomically replace its
            # status records. The status directory and validator channel remain distinct.
            $statusRules = "(A;OICI;0x1200a9;;;$ValidatorSid)(A;;0x1200ab;;;$createdGroup)(A;OIIO;0x1301bf;;;$createdGroup)"
            [void][IO.Directory]::CreateDirectory("$session\status",(DirectoryAcl $statusRules))
            $desc = @{validator_sid=$ValidatorSid;operator_sid=$createdUser;session_group_sid=$createdGroup;target_id=$TargetId;release_id=$ReleaseId;journal_id=$JournalId;image=$Image;runtime_sha256=$RuntimeSha256;enroll=$true}
            Save-New "$session\binding.new" ($desc | ConvertTo-Json -Compress)
            [IO.File]::Move("$session\binding.new",$binding)
            $script:SetupPhase = 'Waiting for the validator endpoint'
            $deadline = [DateTime]::UtcNow.AddMinutes(3)
            while (![IO.File]::Exists("$session\channel\endpoint.json")) {
                if ($launchProcess.HasExited -or $stop.WaitOne(250)) { return }
                if ([DateTime]::UtcNow -ge $deadline) { throw 'Validator did not publish its endpoint.' }
            }
            $runtime = "$root\SplitRuntime\$RuntimeSha256"
            $start = [Diagnostics.ProcessStartInfo]::new()
            $start.UseShellExecute = $false
            $start.CreateNoWindow = $true
            $start.FileName = "$runtime\python.exe"
            $start.Arguments = "-I -m agent_office.split_windows_session operator --session-dir `"$session`""
            $start.WorkingDirectory = $runtime
            $start.Domain = [Environment]::MachineName
            $start.UserName = $accountName
            $start.Password = $password
            $start.LoadUserProfile = $true
            # Leave EnvironmentVariables untouched so CreateProcessWithLogonW
            # receives NULL and constructs the temporary account's profile environment.
            $script:SetupPhase = 'Starting isolated deployment operator'
            $operator = [Diagnostics.Process]::Start($start)
            try {
                Save-New "$session\operator-started.json" (@{pid=$operator.Id;sid=$createdUser;start=$operator.StartTime.ToUniversalTime().Ticks} | ConvertTo-Json -Compress)
                while (!$operator.HasExited -and !$launchProcess.HasExited -and !$stop.WaitOne(250)) {}
                [IO.File]::Delete($binding)
                # The worker observes revocation at its ordinary transaction boundary.
                $operator.WaitForExit()
                if ($operator.ExitCode -ne 0) { throw "Dedicated operator exited with code $($operator.ExitCode)." }
            } finally { $operator.Dispose() }
        }
    } finally {
        if (Get-Variable password -Scope Local -ErrorAction SilentlyContinue) { $password.Dispose() }
        if (!$Recovery -and $launchProcess) { $launchProcess.Dispose() }
        if ([IO.File]::Exists($binding)) { [IO.File]::Delete($binding) }
        if ($createdUser) {
            try {
                $user = Get-LocalUser -Name $accountName -ErrorAction SilentlyContinue
                if ($user -and $user.SID.Value -eq $createdUser) { Disable-LocalUser -SID $user.SID }
            } catch { $unrestored.Add($_.Exception.Message) }
        }
        if ($createdUser) {
            # Refuse to remove grants under a running deployment. A crashed guardian
            # may have missed recording its child PID; the generated account is unique.
            $deadline = [DateTime]::UtcNow.AddMinutes(2)
            $account = [Environment]::MachineName + '\' + $accountName
            while (@(Get-Process -IncludeUserName | Where-Object { $_.UserName -ieq $account }).Count) {
                if ([DateTime]::UtcNow -ge $deadline) { throw 'Dedicated processes are still active. Retry recovery after they have drained.' }
                Start-Sleep -Milliseconds 500
            }
        }
        if ($aclHelper) {
            try { $aclHelper.Service($true) } catch { $unrestored.Add($_.Exception.Message) }
            try { $aclHelper.Recover() } catch { $unrestored.Add($_.Exception.Message) }
            $aclHelper.Dispose()
        }
        if ($createdGroup) {
            try {
                $group = Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue
                if ($group -and $group.SID.Value -eq $createdGroup) { Remove-LocalGroup -SID $group.SID }
            } catch { $unrestored.Add($_.Exception.Message) }
        }
        if ($createdUser) {
            try {
                $user = Get-LocalUser -Name $accountName -ErrorAction SilentlyContinue
                if ($user -and $user.SID.Value -eq $createdUser) { Remove-LocalUser -SID $user.SID }
            } catch { $unrestored.Add($_.Exception.Message) }
        }
        if ($unrestored.Count) { throw "Recovery required for session $SessionId : $($unrestored -join ' | ')" }
        if ([IO.File]::Exists($stateFile) -and ![IO.File]::Exists("$private\completed")) { Save-New "$private\completed" 'restored' }
    }
} catch {
    if ([IO.Directory]::Exists($session)) {
        Assert-Private $session
        Save-New "$session\error-$([guid]::NewGuid().ToString('N')).txt" ([string]$_.Exception.Message)
    }
    throw
} finally { $lock.Dispose() }
