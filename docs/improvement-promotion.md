# 작업 이력 기반 개선안과 승격

작업 호스트에서 에이전트가 개선안을 작성하고, 운영자 환경에서 접수·검증·승격한다. 두 환경 사이로 전달하는 것은 제안 JSON, 작업 이력을 대조할 신뢰된 저널, 승인된 기준 소스다. 한 컴퓨터를 쓰면 OS 계정과 파일 접근 권한을 분리한다. 작업자는 운영자 계정, Docker 소켓, 릴리스 디렉터리에 접근할 수 없어야 한다. 터미널 확인은 작업자와 운영자가 같은 OS 권한을 공유할 때의 보안 경계가 될 수 없다.

## 1. 최초 안정 소스 등록

운영자가 검토한 Office 체크아웃에서 아래 명령을 실행한다. `BASE_SHA`에는 `git rev-parse HEAD`의 전체 값을, `IMAGE_ID`에는 두 번째 명령이 출력한 `sha256:` 값을 넣는다. `../office-releases`는 새 디렉터리여야 한다.

```shell
docker build -t office-validator:reviewed validation
docker image inspect --format "{{.Id}}" office-validator:reviewed
agent-office improve --release ../office-releases init --repo . --revision BASE_SHA --image IMAGE_ID
agent-office improve --release ../office-releases status
```

릴리스 기록의 안정 커밋이 확보되는 시점이다. GitHub의 브랜치, 설치된 패키지와 실행 중인 Studio는 이 명령의 대상에 포함되지 않는다.

## 2. 작업 호스트에서 제안 생성

화면에서 작업할 때는 아래의 [작업실 화면 연결](#작업실-화면-연결)을 사용한다. 터미널에서 개별 제안 파일을 다루는 방법도 지원한다.

Windows 또는 Linux 작업 호스트에서 저널을 조회하고 종료된 실행의 `current_run_id`를 고른다. 기준 소스의 커밋은 운영자가 `status`로 확인한 값과 같아야 한다. 승인된 소스가 필요하면 5단계로 내보낸 Git 체크아웃을 사용한다.

```shell
agent-office --db ../office-data/tasks.sqlite3 tasks list
agent-office --db ../office-data/tasks.sqlite3 improve propose --repo . --base BASE_SHA --run RUN_ID --engine codex --out proposal.json
```

`--run`을 반복해 1~8건을 선택할 수 있다. 제안 엔진은 각 실행의 요청 요약과 최근 이벤트 최대 12개를 받는다. 기준 커밋만 담은 임시 Git 복사본에서 읽기 모드로 동작하며 실행마다 실제 이벤트 하나를 인용한다. 근거가 없거나 패치가 기준 커밋에 적용되지 않으면 제안 파일을 만들지 않는다. 이 명령은 선택한 CLI 서비스의 계정과 사용량을 쓴다.

## 3. 운영자 환경에서 접수와 검증

제안이 완성되면 작업 호스트는 엔진, 근거, 패치를 포함한 제안 전체의 SHA-256과 생성 시각을 원본 저널에 기록한다. 운영자는 제안 JSON과 별도로 신뢰하는 원본 저널에 읽기 접근할 수 있어야 한다. 원격 작업의 경우 제안이 생성된 후 해당 호스트에서 SQLite 백업 API나 `.backup`으로 만든 일관된 사본을 사용한다. 접수 시 생성 기록과 제안 전체를 대조하고 각 인용과 원래 실행 기록도 다시 확인한다.

```shell
agent-office --db ../audit/tasks.sqlite3 improve --release ../office-releases submit proposal.json
agent-office improve --release ../office-releases verify CANDIDATE_ID
agent-office improve --release ../office-releases review CANDIDATE_ID --html review.html
```

`submit`의 결과에 후보 ID와 전체 커밋 SHA가 나온다. `verify`는 그 커밋의 파일을 로컬 Linux 컨테이너의 표준입력으로 전달한다. Windows에서는 Linux 컨테이너 모드가 필요하다. 검증 컨테이너는 네트워크와 호스트 볼륨 없이 일반 사용자로 실행된다. 검사 이미지, 후보 SHA, 종료 코드, 출력이 릴리스 기록에 남는다. 실패 원인을 해결한 뒤 같은 ID에 `verify`를 다시 실행할 수 있다. 후보 내용을 바꾸려면 새 제안을 접수한다.

**기본 검사 범위**는 Python 소스 컴파일, 실행 ID 처리, 이벤트 메타데이터 제한, 요청 마스킹에 대한 여섯 동작 검사다. 예를 들어 CLI 재시도 동작을 수정하면 운영자가 신뢰하는 이미지에 해당 CLI를 호출해 종료 상태와 재시도 횟수를 확인하는 검사를 넣고 접수 시 지정한다.

```shell
agent-office --db ../audit/tasks.sqlite3 improve --release ../office-releases submit proposal.json --image TARGETED_IMAGE_ID
```

검사 이미지는 후보 밖에서 준비하고 `/usr/bin/timeout`을 포함한다. 엔트리포인트는 표준입력에서 소스 tar를 읽고 검사 이름과 결과를 출력하며 모든 필수 검사를 통과했을 때만 0으로 종료해야 한다. 이미지 ID는 접수 뒤 바꿀 수 없다. 제공되는 검사 소스는 [validation](../validation)에 있다.

## 4. 근거와 결과를 검토해 결정

`review.html`은 생성 당시의 검토 기록이다. 인용한 실행이 제안 이유를 뒷받침하는지, 변경 내용이 기대 효과와 일치하는지, 검사 출력이 해당 동작을 검증했는지 읽는다. 최신 기록은 `review CANDIDATE_ID`, 후보 목록은 `list`로 확인한다.

```shell
agent-office improve --release ../office-releases approve CANDIDATE_ID --sha REVIEWED_SHA
```

운영자 터미널에 표시된 후보와 확인 문구를 읽고 입력한다. 승인하면 같은 트랜잭션에서 결정 이력을 남기고 안정 커밋을 교체한다. 거절하려면 같은 명령의 `approve`를 `reject`로 바꾼다. 실패한 후보, 다른 SHA, 이미 다른 후보가 승격돼 기준이 바뀐 경우에는 승인을 거부한다. 두 후보가 같은 기준에서 출발했다면 남은 후보는 새 안정 커밋에서 다시 제안하고 검증한다.

## 5. 승인된 소스 사용

```shell
agent-office improve --release ../office-releases checkout-stable ../approved-office
```

지정한 새 디렉터리에 현재 안정 커밋의 독립 Git 체크아웃이 생긴다. 다음 개선 주기의 기준이나 별도 배포 절차의 입력으로 사용할 수 있다. 승격의 단위는 **릴리스 기록이 가리키는 소스 커밋**이다. 서비스 설치와 재시작은 운영자의 배포 절차에서 수행한다.

## 작업실 화면 연결

1단계에서 릴리스 저장소를 초기화한 뒤 다음 두 명령을 **각각의 계정**에서 실행한다. 예시는 작업 호스트 기준 경로다. 로컬·원격 모두 같은 방법을 사용한다.

**작업실 계정**에서 평소 사용하던 저널과 개선 대상인 Office 소스 체크아웃을 지정한다. 조회 파일은 운영자가 처음 게시할 때 생성된다.

```shell
agent-office --db ../office-data/tasks.sqlite3 studio --improvement-source ./approved-office --release-view ../published/office-view.sqlite3
```

원격 호스트는 위 명령에 `--accept-remote`를 추가한다. 로컬 작업실에서 기존 `--remote` 설정으로 연결한 뒤 **개선안**에서 해당 호스트를 선택한다. 생성에는 그 호스트의 저널·엔진·소스가 쓰인다. 소스 체크아웃에는 게시된 안정 커밋이 있어야 한다. 새 버전을 승격하면 다음 제안 전에 승인된 소스를 작업 호스트에 전달한다.

**운영자 계정**에서 같은 호스트 저널을 읽고, 릴리스 저장소와 별도의 디렉터리에 작업실 조회 파일을 게시한다. 이 디렉터리는 운영자가 관리하며 Studio와 작업자에게는 읽기 접근만 제공한다. 작업자 계정은 릴리스 저장소와 Docker 소켓에 접근할 수 없어야 한다.

```shell
agent-office --db ../office-data/tasks.sqlite3 improve --release ../office-releases studio-sync --view ../published/office-view.sqlite3 --watch
```

`--watch`를 실행하면 생성이 끝난 제안을 인수해 원본 생성 기록을 대조하고 기본 검사 이미지로 후보를 검증한다. 한 번만 게시하려면 `--watch`를 생략한다. Studio는 이 프로세스가 게시한 조회 파일만 열기 때문에 운영자 계정이나 Docker 권한이 필요하지 않다. 서로 다른 기기에서 계정을 운영하면 일관된 저널 백업을 운영자에게, 게시된 조회 DB의 일관된 사본을 작업 호스트에 전달한다. 사본의 갱신 주기가 화면의 최신성을 결정한다.

**화면 사용 순서**

1. Studio 또는 오피스 보기에서 **개선안**을 열고 호스트를 선택한다. 종료된 실행 1~8건과 엔진을 고른 뒤 **개선안 생성**을 누르면 해당 CLI가 호출된다.
2. **생성 내역**에서 근거·기대 효과·제안 패치를 읽는다. 연결이 끊기면 같은 호스트와 요청 ID에서 접수 결과를 다시 확인한다. 생성 도중 작업실 프로세스가 종료된 경우에는 중단 상태가 남고 새 생성을 직접 선택할 수 있다.
3. **검토 후보**에서 후보 SHA, 검증과 결정 상태를 보고 상세에서 실제 변경과 검사 출력을 읽는다. 생성이 끝나도 아직 운영자가 접수하지 않았으면 제안 패치까지만 볼 수 있다. 화면은 마지막 게시 시각을 표시하며, 긴 diff·출력·이력은 표시한 범위를 안내한다. 전체 결과는 운영자의 `improve review CANDIDATE_ID`로 확인한다.
4. 운영자가 터미널에서 `approve` 또는 `reject`로 후보 SHA를 확인한다. 다음 게시 뒤 화면에서 결정과 그 시각을 확인한다. 실패하거나 중단된 검사를 다시 실행할 때에는 운영자 터미널의 `verify`를 사용한다.

[작업실 연동 구현 계획](superpowers/plans/2026-11-19-studio-improvements.md)
