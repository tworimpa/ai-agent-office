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

서비스 계정과 작업자가 서비스의 추가 실행 훅, 환경 설정, Python 의존성 또는 NSSM 바이너리를 수정할 수 없어야 한다. Linux에서 기동 실패와 복구를 배포기가 담당하도록 구성할 때는 `Restart=no`를 사용할 수 있다. 시스템의 기동 횟수 제한이나 복구 불가능한 서비스 설정 오류가 직전 버전의 시작까지 막으면, 운영자가 원인을 해결한 뒤 복구 명령을 실행한다.

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
