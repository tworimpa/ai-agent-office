"""Exercise the reviewed bootstrap without a service, UAC, SAM writes, or live ledgers."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
LAUNCHER = SCRIPTS / "start-operator-session.ps1"
BOOTSTRAP = SCRIPTS / "split-session-bootstrap.ps1"
NATIVE = SCRIPTS / "SplitSessionAcl.cs"
WINDOWS = pytest.mark.skipif(os.name != "nt", reason="requires inbox Windows PowerShell and .NET Framework")


def test_elevated_payload_matches_reviewed_canonical_sources():
    launcher = LAUNCHER.read_text(encoding="utf-8")
    pins = re.search(r"\$pins = @\('([0-9A-F]{64})','([0-9A-F]{64})'\)", launcher).groups()
    assert pins == tuple(hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest().upper()
                         for path in (BOOTSTRAP, NATIVE))


def shell():
    return Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@WINDOWS
def test_preview_performs_no_provisioning():
    result = subprocess.run([shell(), "-NoProfile", "-NonInteractive", "-File", LAUNCHER, "-Preview"],
                            capture_output=True, check=True, timeout=30)
    preview = json.loads(result.stdout)
    assert preview["service_mask"] == "0x35"
    assert preview["operator"] == "AgentOfficeOperator"
    assert preview["persistent_task"] is False


@WINDOWS
def test_inline_elevated_command_parses_without_executing_it(tmp_path):
    launcher = LAUNCHER.read_text(encoding="utf-8")
    strings = launcher[launcher.index("$attest = @'"):launcher.index("$elevated = $null")]
    script = tmp_path / "parse.ps1"
    script.write_text("""
$ErrorActionPreference='Stop'
$pins=@(('a'*64),('b'*64))
$id='c'*32
$sid='S-1-5-21-100-200-300-400'
$pipeName='AgentOfficeSetup-'+('d'*32)
$recoverFlag='$false'
$pidStart='111111111111111111'
$TargetId=$ReleaseId=$JournalId=$id
$Image='sha256:'+('e'*64)
$RuntimeSha256='f'*64
$RuntimeArtifactId='1234'
""" + strings + """
[void][scriptblock]::Create($firstStage)
if([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($firstStage)).Length -gt 31000){throw 'Oversized command line'}
""", encoding="utf-8-sig")
    subprocess.run([shell(), "-NoProfile", "-NonInteractive", "-File", script], check=True, timeout=30)


@WINDOWS
def test_acl_grant_and_undo_on_disposable_databases_and_slots(tmp_path):
    """Compile the production handle code with a disposable root in place of the host anchor."""
    root = tmp_path / "office"
    root.mkdir()
    for folder in ("release", "deploy/slots/stable", "data/tasks.sqlite3.locks", "release/objects.git/objects",
                   "release/objects.git/refs/candidates", "release/objects.git/refs/releases",
                   "release/release.sqlite3.locks", "release/operator-policy.sqlite3.locks"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    for file in ("release/release.sqlite3", "release/operator-policy.sqlite3", "deploy/deployment.sqlite3",
                 "deploy/slots/stable/keep.txt", "data/tasks.sqlite3"):
        (root / file).write_bytes(b"test data")
    native = NATIVE.read_text(encoding="utf-8").replace('const string Host = @"C:\\ProgramData\\AI-Agent-Office";',
                                                        'const string Host = @"' + str(root) + '";')
    start = native.index("        var drive = CreateFile(")
    end = native.index("    public void AcceptRetiredSession", start)
    native = native[:start] + r"""
        var drive = CreateFile(Host, 0x60080, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
        if (drive.IsInvalid) throw new Win32Exception();
        anchors.Add(Host, drive);
    }
""" + native[end:]
    source = tmp_path / "fixture.cs"
    source.write_text(native, encoding="utf-8-sig")
    test_script = tmp_path / "check.ps1"
    test_script.write_text(r"""
param($source,$root,$logs)
$ErrorActionPreference='Stop'
$env:PSModulePath="$PSHOME\Modules"
Add-Type -Path $source
$who=[Security.Principal.WindowsIdentity]::GetCurrent().User
$group='S-1-5-21-111111-222222-333333-1501'
$protected=[Security.AccessControl.DirectorySecurity]::new()
$protected.SetSecurityDescriptorSddlForm("D:P(A;OICI;FA;;;$($who.Value))(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",'Access')
[IO.Directory]::SetAccessControl($root,$protected)
$paths=@($root)+@(Get-ChildItem -Recurse -LiteralPath $root | ForEach-Object FullName)
$before=@{}
foreach($p in $paths){$before[$p]=(Get-Acl -LiteralPath $p).Sddl}
for($i=1;$i -le 2;$i++){
    $a=[SplitSessionAcl]::new("$logs-$i",$group,$who.Value)
    try {
        $a.Provision()
        $f=(Get-Acl -LiteralPath "$root\deploy\slots\stable\keep.txt").GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])
        $r=@($f|Where-Object {$_.IdentityReference.Value -eq $group})
        if($r.Count -ne 1 -or $r[0].FileSystemRights.value__ -ne 0x1200a9){throw 'Historical slot must be read only'}
        $db=(Get-Acl -LiteralPath "$root\data\tasks.sqlite3").GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])
        if(!@($db | Where-Object {$_.IdentityReference.Value -eq $group -and ($_.FileSystemRights.value__ -band 2)}).Count){throw 'Missing DB write'}
        $a.Recover()
        $a.Recover()
        foreach($p in $paths){if((Get-Acl -LiteralPath $p).Sddl -ne $before[$p]){throw "ACL mismatch at $p"}}
    } finally {$a.Dispose()}
}
""", encoding="utf-8-sig")
    result = subprocess.run([shell(), "-NoProfile", "-NonInteractive", "-File", test_script, source, root, tmp_path / "undo"],
                            capture_output=True, timeout=90)
    assert result.returncode == 0, (result.stdout, result.stderr)
