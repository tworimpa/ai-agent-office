# 이슈 40 구현 및 검증 범위

기준 버전은 `3e2ae40`이다. 항목 번호는 [원 이슈](https://github.com/tworimpa/ai-agent-office/issues/40)를 따른다.

| 항목 | 사용자가 확인할 동작 | 검증 진입점 |
| --- | --- | --- |
| 1 · 사무실 폴링 | 숨은 탭은 갱신을 멈추고 다시 보이면 최신 상태를 읽는다. 실행 중에는 3초, 유휴 상태에서는 20초 간격이다. 진행 중인 GitHub 동기화 상태는 일반 캐시보다 먼저 갱신한다. | `tests/web/workspace-polling.test.mjs`, 설치판 GitHub 여정 |
| 2 · 작업 트리 정리 | Workspace 설정에서 용량과 보존 사유를 보고 실행 한 건을 선택한다. 실행·복구·검토·파일·브랜치 신원을 다시 확인하고 충돌 시 보존한다. | `tests/test_worktree_storage.py`, `tests/web/workspace-storage.test.mjs` |
| 3 · CLI 진단 | 실패 단계와 분류된 원인을 확인한다. stderr 수집량에는 상한이 있고 사용자 프롬프트·자격값을 포함할 수 있는 원문은 저널에 남기지 않는다. | `tests/test_engine_operations.py` |
| 4 · Cline 호환성 | 미검증 버전에는 호스트의 읽기·편집 검증을 안내한다. 다른 버전이나 다른 실행 파일의 통과 기록은 승계되지 않는다. | `tests/test_engine_operations.py`, `tests/test_acp_adapters.py` |
| 5 · SQLite 경합 | 화면 조회의 쓰기 잠금을 줄이고 실행 종료는 프로세스 알림으로 감지한다. 신규 독립 작업실에는 WAL을 적용하고 기존·관리 서비스 저널은 원래 모드를 유지한다. | `tests/test_room_bundle.py`, `tests/test_away_http.py` |
| 6 · 장시간 진행 | 확인한 활동과 시각을 담당자에 연결한다. 로컬 구독은 이벤트 ID로 재접속하고 원격은 갱신 시 같은 저널의 최근 활동을 읽는다. | `tests/test_engine_operations.py`, `tests/web/engine-operations.test.mjs` |
| 7 · 원격 요청 | 작업실·실행·목표·질문·지시를 묶어 한 번에 조회한다. 각 응답의 작업실·실행 신원과 크기 제한을 검사한다. 이전 호스트는 개별 조회를 사용한다. | `tests/test_room_bundle.py` |
| 8 · 작업자 외부 쓰기 | 코딩 CLI와 테스트 자식 프로세스에서 일반 Git 전송과 gh 호출을 제한한다. 운영자 환경으로는 별도로 승인된 작업을 수행할 수 있다. | `tests/test_worktree_storage.py`, [저장소 경계](storage.md) |
| 9 · 다언어 검증 | 목표 설정에서 pytest, npm, Go, Cargo를 선택한다. 각 호스트·단계의 종료 코드와 출력이 같은 목표에 남는다. 단일 명령 영수증은 테스트 케이스 개수와 구분한다. | `tests/test_project_checks.py`, `tests/web/workspace-goal.test.mjs`, [검증 레시피](overnight-goals.md) |
| 10 · 공급자 사용량 및 업데이트 | 공급자가 제공한 사용량·비용과 계정 한도를 구분한다. 엔진 진단에서 공개 버전을 확인하며 설치 변경은 유휴 상태의 운영자 터미널에서 검토한다. | `tests/test_engine_operations.py`, [엔진 안내](engines.md) |
| 11 · 화면 코드 | 공통 요청·폴링 및 좌석·요약·갱신 모듈을 분리했다. CI에서 DOM 동작과 공통 모듈 타입을 검사한다. | `npm run test:web`, `npm run check:web` |
| 12 · 부재중 답변 | 사전에 위임한 사실은 기준 커밋·행·인용으로 답해 모델 호출을 0회로 줄인다. 모호한 문서와 권한 요청은 사용자에게 남는다. 독립 재확인을 선택하면 기존 두 역할을 사용한다. | `tests/test_away_http.py`, `tests/test_away_expert.py` |
| 13 · 좌석과 동시성 | 작업실에 참여 중인 역할과 하위 작업을 표시한다. 호스트별 병렬 슬롯은 1~6개로 설정하고 기본값은 2개다. | `tests/test_team_runs.py`, `tests/web/workspace-polling.test.mjs` |
| 14 · 종료·문서·라이선스 | 시간 초과한 CLI 자식을 정리한다. 현재 구성과 책임 경계를 문서화하고 소유자가 선택한 Apache-2.0 원문을 배포물에 포함한다. | `tests/test_engine_operations.py`, [아키텍처](architecture.md), [라이선스](../LICENSE) |

## 측정 해석

Windows의 같은 임시 작업실에서 활성 실행의 조회를 15회 측정했다. 첫 탐색 이후 한 번의 갱신에 HTTP 요청은 5회에서 1회, SQLite 연결은 6회에서 3회, 쓰기 트랜잭션 시작은 2회에서 0회로 줄었다. DB 조회 중앙값은 15.4ms에서 7.7ms로 바뀌었다. 이 수치는 실제 원격 네트워크의 지연이나 SSH 인증 시간을 포함하지 않는다.

부재중 답변은 모의 CLI 호출 기록과 저널을 대조한다. 공급자 파서는 보고된 값만 사용하며 미보고·모호한 값은 미확인으로 표시한다. 외부 쓰기와 작업 트리 정리는 일회용 저장소에서 검사한다. Git/gh 환경 제한과 OS 샌드박스의 보호 범위는 다르다.
