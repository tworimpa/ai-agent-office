# 안정 버전을 Studio 서비스에 배포하기

안정 버전은 운영자 릴리스 원장의 소스 순번과 커밋이다. 배포는 그중 한 버전을 **지정한 호스트의 Studio 서비스**로 실행하는 절차다. 과거 순번을 서비스에 배포해도 안정 원장의 현재 순번과 다른 호스트의 서비스는 그대로다.

## 운영 준비

- 관리할 각 호스트에 전용 서비스 계정과 운영자 계정을 따로 둔다. 서비스 계정에는 런타임 저널·작업공간에 필요한 권한과 배포 디렉터리에 대한 읽기·통과 권한을 제공한다. 릴리스 원장, 배포 원장, 코드 디렉터리 및 신뢰된 Python 설치는 작업자와 서비스 계정이 수정할 수 없어야 한다. 운영자에게는 해당 서비스의 구성·상태·리스닝 프로세스를 확인하고 재시작할 권한이 필요하다.
- 신뢰하는 Python 환경에 이 버전의 Agent Office와 배포할 소스가 요구하는 의존성을 준비한다. 배포 중에는 후보의 설치 스크립트나 패키지 설치를 실행하지 않는다. 관리 런처는 승인된 커밋의 `src/agent_office`와 웹 자산을 사용한다.
- 관리할 런타임 저널은 기존 Studio에서 초기화한 것을 사용한다. 해당 Studio의 Journal ID를 확인하고 고정된 루프백 포트를 선택한다. 별도 관제 Studio를 함께 띄울 때는 서로 다른 저널과 포트를 사용한다.

## 대상 등록

다음은 Linux 시스템 서비스의 예다. 경로, 서비스 이름, 포트와 32자리 저널 ID는 실제 호스트의 값으로 바꾼다. `--target`은 처음 등록할 때 새 디렉터리여야 한다.

```shell
agent-office improve --release /srv/office/release deployment-init --target /srv/office/deploy --manager systemd --service office-studio.service --runtime-python /opt/office/venv/bin/python --port 8766 --journal-id JOURNAL_ID
```

서비스 등록과 권한 설정은 호스트 운영자가 관리한다. 위 명령은 전용 디렉터리에 대상 ID·릴리스 ID·저널 ID, 포트와 서비스 정보를 등록한다. 서비스가 시작할 때 사용할 명령은 다음 형태다. 인수의 경로는 절대 경로를 쓴다.

```shell
/opt/office/venv/bin/python -I -m agent_office.managed_runtime --target /srv/office/deploy --db /var/lib/office/runtime.sqlite3
```

Linux에서는 미리 등록한 `Type=simple` 또는 `Type=exec` systemd 서비스의 `ExecStart`에 이 명령을 직접 지정한다. 시스템 서비스에는 전용 비루트 `User=`를 지정한다. 운영자는 systemd의 D-Bus 구성과 `/proc/<서비스 PID>/fd`의 리스닝 소켓 소유자를 확인할 수 있어야 한다. 서비스 재시작 권한만 위임한 계정은 이 조회 권한이 부족할 수 있다. 같은 사용자로 운영자와 런타임을 함께 실행하는 구성은 배포 대상이 될 수 없다. `systemd-user`도 서비스 계정과 다른 운영자 신원으로 해당 사용자 관리자를 조회·제어할 수 있을 때 사용한다.

Windows에서는 미리 등록한 **NSSM** 서비스 이름을 `--manager windows --service NAME`으로 지정한다. SCM의 실행 경로는 NSSM이고, NSSM의 `Application`은 등록한 신뢰된 Python 인터프리터의 절대 경로다. `AppParameters` 예시는 아래와 같다. 서비스는 전용 비관리자 계정으로 실행한다.

```text
-I -m agent_office.managed_runtime --target "D:\Office\deploy" --db "D:\OfficeData\runtime.sqlite3"
```

서비스 계정이 배포 디렉터리의 기존 파일과 앞으로 만들어질 코드 디렉터리를 읽을 수 있게 호스트에서 권한을 준비한다. 작업자가 파일을 교체할 수 있는 상위 디렉터리나 Python 환경에 런처를 설치하지 않는다. 서비스가 원격 Studio 요청도 받으려면 서비스 명령에 `--accept-remote`를 추가한다. `--release-view`, `--improvement-source`, 기존의 `--remote` 옵션도 서비스 구성에 지정할 수 있다.

Windows에서는 `deployment-init`으로 만든 대상 디렉터리 자체에 전용 서비스 계정의 읽기·실행 권한을 부여하고 하위 디렉터리와 파일에 상속한다. 새 소스 슬롯은 `slots` 디렉터리의 ACL을 상속한다. 런타임 저널과 서비스 로그의 쓰기 권한은 이 디렉터리 밖의 서비스 데이터 경로에 부여한다.

서비스 계정과 작업자가 서비스의 추가 실행 훅, 환경 설정, Python 의존성 또는 NSSM 바이너리를 수정할 수 없어야 한다. Linux에서 기동 실패와 복구를 배포기가 담당하도록 구성할 때는 `Restart=no`를 사용할 수 있다. 시스템의 기동 횟수 제한이나 복구 불가능한 서비스 설정 오류가 직전 버전의 시작까지 막으면, 운영자가 원인을 해결한 뒤 복구 명령을 실행한다.

### Windows 서비스의 Codex 작업

비대화형 Windows Session 0에서 작업실의 Codex는 호환 셸로 `cmd.exe`를 사용하라는 지침을 자동으로 받는다. 요청함 모드에서는 세션 시작 시 앱 지침이 전달되고, 요청함을 끄면 최초 작업 메시지에 같은 안내가 붙는다. Codex가 셸 도구를 호출하며, 서비스 전용 프로필에 설정한 MXC와 해당 실행의 기존 승인 규칙이 적용된다.

Windows 11 빌드 26300에서 MXC를 사용하는 Session 0은 PowerShell과 Git for Windows를 시작할 때 `0xC0000142`를 반환할 수 있다. 작업실 조정자는 테스트가 끝난 작업 트리에서 기존 변경 미리보기를 5초 제한으로 수집해 Codex 리뷰어에게 전달한다. 리뷰어는 이 미리보기와 테스트 결과를 사용해 검토하며, 미리보기에 없는 근거가 필요하면 작업 트리에서 파일을 직접 확인한다. 샌드박스 안에서 Git 자체를 호출해야 하는 기능에는 이 Windows 제약이 적용된다. [Microsoft MXC 이슈](https://github.com/microsoft/mxc/issues/1465)

## 검토·배포·복구

```shell
agent-office improve --release /srv/office/release history
agent-office improve --release /srv/office/release deployment-status --target /srv/office/deploy
agent-office improve --release /srv/office/release deploy 3 --target /srv/office/deploy
```

운영자는 호스트 터미널에서 서비스와 포트, 실제 실행 중인 버전, 배포할 순번·전체 SHA, 실패 시 돌아갈 버전을 읽고 표시된 확인 문구를 입력한다. 첫 배포는 등록된 서비스가 중지되어 있고 포트가 비어 있을 때 시작한다. 이후 배포는 현재 버전과 부팅 ID가 실제 서비스 응답과 일치해야 한다. 재시작으로 진행 중인 작업이나 원격 연결이 중단될 수 있으므로 작업실의 실행 상태를 먼저 확인한다.

새 버전은 별도의 코드 디렉터리에 준비한다. 런처는 저장된 파일 목록·내용·실행 권한을 검사하고 기존 런타임 저널로 Studio를 시작한다. 운영자 CLI는 새 부팅 ID가 확인되고 같은 부팅에서 일반 작업실 API까지 응답해야 배포 완료를 기록한다.

같은 순번·SHA의 서비스가 이미 정상이라면 중복 요청으로 다시 시작하지 않는다. 배포가 성공한 뒤 운영자가 예전 서비스 버전으로 돌아가고 싶으면 `deployment-status`에서 직전 실행 순번을 확인하고 `deploy 이전순번 --target DIR`로 다시 검토한다.

기동 검증 실패 시 직전 정상 버전을 다시 선택해 재시작하고 복구된 프로세스의 응답을 확인한다. 첫 배포 실패에는 복구할 버전이 없어 서비스를 중지하고 포트가 비었는지 확인한다. **배포 요청은 실패했지만 복원이 확인된 경우에도 배포 명령은 실패 종료 코드를 반환**한다. 이미 서비스가 정상 기동하고 배포가 끝난 뒤의 장기 장애 감시는 별도 운영 절차다.

운영자 프로세스가 중단되었거나 복구 확인에 실패한 경우에는 상태와 대상 서비스 구성을 점검한 뒤 같은 운영자 터미널에서 다음 명령으로 재검토한다.

```shell
agent-office improve --release /srv/office/release deployment-recover --target /srv/office/deploy
```

## 로컬·원격 화면

관리 런처가 시작한 최신 Studio는 해당 배포 대상을 사용한다. 독립적인 관제 Studio에서는 시작할 때 `--deployment-target /srv/office/deploy`를 지정한다. 관제 저널에는 해당 릴리스의 `studio-sync --stable-history` 조회 파일을 `--release-view`로 연결한다. 이렇게 분리하면 대상이 재시작하거나 이전 소스에 배포 화면이 없어도 관제 Studio가 상태를 확인할 수 있다.

**안정 버전 이력 → 서비스 배포 상태**에서 선택한 호스트의 지정 서비스와 **대상 버전**을 비교한다. 서비스 이름과 실제 응답에서 확인한 순번·SHA, 마지막 배포 및 복원 결과가 표시된다. 같은 SHA로 돌아온 다른 안정 순번과, 다른 릴리스의 같은 번호도 구분한다. 원격 호스트는 기존 SSH 연결로 상태를 읽고, 운영 명령은 그 서비스를 소유한 호스트의 터미널에서 실행한다.

배포 대상은 관제 Studio를 시작한 운영자가 경로로 지정한 서비스다. 서비스 이름 옆 런타임 저널 ID로 실제 대상을 식별한다. 관제 저널과 대상 서비스 저널을 별도로 운용할 수 있다. 포트 연결이 끊기거나 부팅 정보·서비스 소유권을 확인할 수 없으면 화면은 확인된 실행 버전을 표시하지 않는다. [구현 계획](superpowers/plans/2026-11-19-stable-deployment.md)

## Windows 운영자 예약 작업

### 최초 위임과 취소

운영자가 이미 등록된 정상 서비스의 대상·저널·저장소·검증 이미지를 확인하고 정책을 한 번 등록한다. 기본 검증 이미지는 현재 안정 버전을 승격할 때 사용한 이미지다. 등록할 때의 짧은 확인 뒤에는 일치하는 배포마다 `approve …`와 `deploy …`를 입력할 필요가 없다.

```text
agent-office improve --release RELEASE deployment-auto-enable --target TARGET
agent-office improve --release RELEASE deployment-auto-status --target TARGET
agent-office improve --release RELEASE deployment-auto-run --target TARGET
agent-office improve --release RELEASE deployment-auto-disable --target TARGET
```

`deployment-auto-run`은 한 번 검사한다. `--watch`는 운영자 프로세스가 살아 있는 동안 5분마다 검사한다. 작업실 프로세스, 웹 API와 코딩 에이전트에는 정책 등록·변경·호출 기능을 제공하지 않는다. 독립된 신뢰 런타임에서 이 명령을 실행한다. 후보가 배포 도구를 변경해도 현재 검증을 담당하는 운영자 런타임과 정책 파일을 자동으로 덮어쓰지 않는다.

- 등록 대상은 `tworimpa/ai-agent-office`의 `main`과 한 서비스다. 운영자 소유의 별도 Git 미러에 공식 HTTPS 원격의 객체를 받아 기존 안정 버전 바로 다음의 병합을 순서대로 처리한다. 직접 푸시·미병합·불일치·만료된 검증 로그는 보류한다.
- 해당 PR의 최종 헤드와 병합 부모, 실제 checkout된 테스트 트리를 GitHub 자료와 로컬 Git 객체로 대조한다. Windows·Ubuntu × Python 3.11·3.13 및 두 OS의 Electron·Rust 설치 검사까지 8개가 모두 성공해야 한다. 워크플로 이름뿐 아니라 등록된 워크플로 ID, 최신 시도와 각 작업의 checkout 증거를 확인한다. 테스트 코드의 품질은 PR 리뷰 대상이다.
- 배포 도구가 포함된 변경에도 같은 조건이 적용된다. 기존 격리 검증이 통과해야 하며, 승격 직전 정책 세대와 CI 증거를 재확인한다. 실제 서비스 등록·프로세스·저널 파일을 대조하고 SQLite에서 작업 진입을 예약한다. 실행 중인 작업이 있으면 다음 회차로 미룬다. 이전 프로세스가 멈춘 뒤 새 프로세스를 시작하기 전에 예약을 해제한다.
- 승인 근거, 대상 커밋, 후보, 서비스 작업 ID와 결과는 운영자 원장의 `operator-policy.sqlite3`와 기존 안정·배포 이력에 남긴다. 새 버전 기동 실패는 기존 직전 버전 복구 경로를 사용한다. 복원이 필요할 때 이미 새 작업이 시작됐거나 운영자의 수동 선택이 바뀌면 운영자 확인 상태를 남긴다. 실패한 같은 버전을 타이머가 반복 배포하지 않는다.

`AgentOfficeStudio-DeploymentWatch`는 **운영자가 Windows에 로그인한 동안** 5분마다 정책을 확인한다. 화면을 잠가도 실행되고, 로그아웃하거나 재부팅하면 다시 로그인해야 한다. 이전 회차가 실행 중이면 다음 회차를 기다린다. 대상은 `C:\ProgramData\AI-Agent-Office\release`, `deploy`, 서비스 `AgentOfficeStudio`로 고정된다. [Microsoft 작업 스케줄러 로그온 유형](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype)

1. 작업자·서비스와 다른 Windows 운영자 계정을 사용한다. 이 계정의 **일반 권한 토큰**에 기존 원장 읽기·쓰기와 해당 서비스의 `SERVICE_QUERY_CONFIG`, `SERVICE_QUERY_STATUS`, `SERVICE_START`, `SERVICE_STOP` 권한이 필요하다. NSSM의 Application·AppParameters 조회, 리스닝 프로세스부터 NSSM까지 각 프로세스의 생성 시각 조회(`PROCESS_QUERY_LIMITED_INFORMATION`), 루프백 Studio 응답 확인도 필요하다. 필요한 권한은 호스트 관리자가 별도로 구성한다.
2. 운영자가 검토한 병합 버전의 컨트롤러 wheel과 의존성 wheel을 준비한다. 아래는 검증된 wheel을 운영자 전용 `D:\OfficeWheels`에 둔 경우다. 기반 인터프리터는 `python-install` 바로 아래 **버전 번호가 있는 실제 CPython 디렉터리**에서 선택한다. 컨트롤러 패키지와 의존성을 전용 venv 안에 설치하고 운영자가 갱신한다. 작업자와 서비스에는 이 환경, 상위 폴더 및 원장에 대한 쓰기 권한이 없어야 한다.

   ```powershell
   $controller = 'C:\ProgramData\AI-Agent-Office\operator'
   $basePython = 'C:\ProgramData\AI-Agent-Office\python-install\cpython-3.11.15-windows-x86_64-none\python.exe'
   & $basePython -I -m venv --copies $controller
   & "$controller\Scripts\python.exe" -I -m pip --isolated install --no-index --find-links D:\OfficeWheels D:\OfficeWheels\ai_agent_office-0.1.0-py3-none-any.whl
   ```

3. 운영자 프로필의 `gh` 로그인으로 해당 저장소와 Actions 증거를 읽을 수 있어야 한다. Git, GitHub CLI, Docker CLI는 `C:\Program Files` 아래의 기본 설치를 사용한다. 로컬 Docker 엔진의 `docker_engine` 파이프와 원장에 고정된 Linux 검증 이미지에도 접근할 수 있어야 한다. 작업 정의에는 암호나 토큰을 저장하지 않는다.
4. 정책을 등록한 뒤 검토한 버전의 저장소에서 **일반 PowerShell**로 아래 명령을 실행한다. 첫 명령은 작업 XML을 출력한다. 두 번째는 경로·ACL, 서비스 제어 권한과 실제 서비스 신원, GitHub 읽기, Docker 이미지 접근을 확인한 뒤 새 작업을 등록한다. 같은 이름의 작업이 있으면 등록이 실패한다.

   ```powershell
   .\scripts\register-deployment-watch.ps1 -Preview
   .\scripts\register-deployment-watch.ps1
   Get-ScheduledTaskInfo -TaskName AgentOfficeStudio-DeploymentWatch
   ```

다음 예약 실행을 중단하려면 `Disable-ScheduledTask -TaskName AgentOfficeStudio-DeploymentWatch`, 다시 허용하려면 `Enable-ScheduledTask -TaskName AgentOfficeStudio-DeploymentWatch`를 사용한다. 스케줄러의 최근 결과 코드는 프로세스 종료 상태다. 실제 배포·복원 결과는 대상 원장과 Studio 상태에서 확인한다.

스케줄러 없이 **같은 권한으로 열어 둔 운영자의 일반 PowerShell 세션**에서 아래처럼 실행할 수도 있다. 사전 점검과 각 회차가 성공하면 다음 회차까지 대기한다. 대기 중 Ctrl+C로 끝낸다.

```powershell
.\scripts\register-deployment-watch.ps1 -WatchSession
```

### Windows에서 검증 계정과 배포 계정을 분리하는 세션

로그인한 사용자가 이미 GitHub CLI와 Docker를 사용할 수 있고, Studio는 전용 서비스 계정으로 실행되는 PC에 적용한다. 시작할 때 관리자가 UAC로 승인한다. 시작한 창을 열어 두면 화면을 잠가도 5분마다 다음 병합을 확인한다. 세션 종료 또는 재부팅 뒤에는 운영자가 다시 시작한다.

- **검증기**는 로그인한 일반 사용자로 실행된다. 별도 Git 미러에서 해당 병합의 GitHub 검사 8개와 checkout 트리를 확인하고 고정 이미지에서 검증한다. 요청마다 대상 서비스·릴리스·저널·기준 및 후보 커밋·이미지·일회용 난수를 포함한 결과에 서명한다.
- **배포기**는 임시 로컬 계정 `AgentOfficeOperator`로 실행된다. UAC로 만든 세션에 소속된 토큰으로만 시작할 수 있다. 자기 미러에서 병합 부모와 트리를 대조하고, 검증기 응답·실행 중인 작업·실제 서비스 신원을 다시 확인한다. 승인 근거에는 두 SID와 세션 ID, 검증기의 공개키가 기록된다.
- **종료와 복구** 시 새 승격과 재시작에는 유효한 세션이 필요하다. 기동 도중 이미 시작한 배포는 같은 작업 ID로 확인하거나 직전 정상 버전으로 복원한다. 중단된 Windows 권한 설정은 시작 시 출력한 세션 ID로 복구할 수 있다.

관리자는 공식 Windows 설치 CI가 발행한 `split-operator-runtime-Windows` 아카이브를 사용한다. 설치 도구는 GitHub가 기록한 아카이브 ID와 SHA-256을 대조한다. 보호된 Program Files 아래에 설치한 Python은 배포 계정의 일반 권한으로 기동한다. Windows PowerShell에서 실행하는 예시는 다음과 같다.

```powershell
.\scripts\start-operator-session.ps1 -Preview
.\scripts\start-operator-session.ps1 -TargetId $targetId -ReleaseId $releaseId -JournalId $journalId -Image $image -RuntimeArchive $archive -RuntimeArtifactId $artifactId -RuntimeSha256 $digest
```

정상 종료는 세션 창에서 Ctrl+C를 사용한다. 종료 처리가 중단된 경우 `start-operator-session.ps1 -Recover $sessionId`로 기록된 서비스·파일 권한의 복구를 요청한다. 원래 접근 권한이나 파일 신원이 다른 작업에 의해 바뀌었으면 충돌 내용을 확인한다.

로그인한 사용자는 세션 디렉터리의 `status/last.json`에서 최근 검사 결과를, `status/last-deployment.json`에서 마지막 서비스 전환의 커밋·안정 순번·작업 ID를 읽을 수 있다. 세션 중 새로 생성된 파일에는 종료 후 삭제된 계정이나 그룹의 SID가 남을 수 있다. 접근 권한 복원은 기존 객체의 DACL과 살아 있는 운영자 프로세스의 종료를 확인한다.
