# 다른 Studio 호스트에서 목표 검증하기

목표의 각 검증 단계에서 실행 호스트를 선택할 수 있습니다. `local`은 목표를 소유한 Studio입니다. 다른 호스트를 선택하려면 목표의 Studio에 등록된 SSH 별칭 또는 저널 ID를 입력하세요. 접수할 때 별칭, 포트, 저널 ID를 고정합니다.

수신 호스트에서는 일반 사용자 세션에서 다음과 같이 Studio를 시작하고, 표시되는 저널 ID를 확인합니다. 두 호스트의 `--db` 경로는 각각 자신의 로컬 파일입니다.

```shell
agent-office --db peer.sqlite3 studio --port 8766 --accept-remote
```

목표 호스트에서 SSH 별칭과 수신 Studio를 연결합니다. 아래의 `PEER_JOURNAL_ID`는 수신 호스트가 출력한 값으로 바꿉니다.

```shell
agent-office --db goals.sqlite3 studio --port 8767 --remote linux-ci 8766 PEER_JOURNAL_ID
```

수신 Studio의 작업공간 첫 폴더에 검증용 Git 저장소 루트를 등록하세요. 기준 커밋과 검증할 커밋은 운영자가 검토하여 해당 저장소에 미리 준비합니다. 기준 검증은 두 SHA가 같습니다. 후보 검증은 후보와 같은 파일 내용·경로·실행 모드를 담은 후속 커밋을 준비합니다. Git의 텍스트 설정으로 Windows에서 CRLF로 체크아웃한 파일은 커밋의 LF 표현과 비교합니다. 바이너리와 `-text` 파일은 원래 바이트까지 일치해야 합니다.

예를 들어 목표의 전체 검증을 `linux-ci`, `python -m pytest -q`, 300초로 설정했다면 수신 호스트의 별도 터미널에서 다음 명령을 실행합니다. 작업공간 ID는 해당 작업공간 주소의 `workspace` 값입니다. `BASE_SHA`와 `CANDIDATE_SHA`는 전체 커밋 SHA입니다.

```shell
agent-office --db peer.sqlite3 peer-check --workspace-id WORKSPACE_ID --base BASE_SHA --commit CANDIDATE_SHA --stage full --timeout 300 --wait-seconds 28800 -- python -m pytest -q
```

이 명령은 소스 식별자와 대기 만료시각을 출력합니다. 기준과 후보를 둘 다 원격에서 검증하려면 각 커밋마다 한 번씩 대기시킵니다. 관련 테스트 파일을 지정한 빠른 검증은 `--stage quick`과 실제 검증 인자 배열을 사용합니다. 원격에서 동적으로 정하는 `--lf` 대신 미리 검토할 수 있는 테스트 파일을 직접 지정하면 일치 여부를 알 수 있습니다.

요청이 도착하면 수신 호스트의 대기 중인 명령이 검토한 커밋을 임시 폴더에 펼치고 한 번 검증합니다. SSH 채널에는 요청 ID, 기준 SHA, 소스와 검증 정의의 해시가 전달됩니다. 실행 결과에는 실제 호스트, 작업 경로, 명령, 소요시간, 커밋, 테스트별 결과가 남습니다. 검증 중 연결이 끊겼을 때 같은 요청 ID를 조회하면 이미 기록된 결과를 확인할 수 있습니다. 수신 호스트에 일치하는 커밋과 검증 대기가 없으면 해당 단계는 미검증입니다. 대기 중인 명령은 Ctrl+C로 종료할 수 있습니다.
