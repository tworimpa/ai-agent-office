# AO-001 명령·출력과 수용 시나리오

상태: 후보 구현의 수용 계약. 아래 ID/출력은 형식 예시이며 실제 모델 실행 영수증이 아니다. [검증 결과](../../ao-001-results.md)를 참조한다.
상위 문서: [설계](2026-11-06-ao-001-design.md), [구현 계획](../plans/2026-11-06-ao-001.md).

## 사용자가 확인할 완성 모습

작업을 한 번 실행한 뒤 터미널을 닫아도 새 터미널에서 작업 ID, 원래 요청의 마스킹된 요약, 작업 경로, 현재 상태, 시도 횟수, 단계별 이벤트를 확인한다. 프로그램이 강제로 종료됐다면 명시적으로 확인해 `RECOVERY_REQUIRED`로 표시할 수 있다. 작업이 다시 실행되지는 않는다.

계획 전용 작업은 `PLAN_ONLY`, 테스트와 리뷰를 모두 통과한 편집 작업은 `READY_FOR_HUMAN_REVIEW`로 표시한다. 두 경우를 UI의 동일한 '개발 완료'로 합치지 않는다.

## 명령과 JSON 계약

예시는 후보 전용 DB `../ao-001-candidate-state/tasks.sqlite3`를 사용한다. 실제 모델을 호출하는 run 예시는 사용량 허용과 CLI 로그인이 선행돼야 한다. 개발/자동 테스트에서는 fake worker로 동일 계약을 검증한다. 후보 상태 디렉터리는 Git 저장소 밖에 두어 DB와 잠금 파일이 소스 변경에 섞이지 않게 한다.

```text
agent-office --db ../ao-001-candidate-state/tasks.sqlite3 run --repo . --developer codex --reviewer codex --max-attempts 2 --task "README를 읽고 개선점 하나를 제안해줘. 파일은 수정하지 마."
```

기존 run JSON의 workspace/branch/attempts/developer_output/test_output/review_output/status를 보존하고 task_id/run_id/state/outcome을 추가한다. 아래는 추가 필드와 핵심 결과만 발췌한 예시다.

```json
{
  "task_id": "11111111111111111111111111111111",
  "run_id": "22222222222222222222222222222222",
  "status": "PLAN_ONLY",
  "state": "PLAN_ONLY",
  "outcome": "PLAN_ONLY",
  "attempts": 1
}
```

```text
agent-office --db ../ao-001-candidate-state/tasks.sqlite3 tasks list --limit 50
agent-office --db ../ao-001-candidate-state/tasks.sqlite3 tasks show 11111111111111111111111111111111
agent-office --db ../ao-001-candidate-state/tasks.sqlite3 events 11111111111111111111111111111111 --after-seq 0 --limit 100
agent-office --db ../ao-001-candidate-state/tasks.sqlite3 recover --run-id 22222222222222222222222222222222
```

| 명령 | stdout JSON |
|---|---|
| tasks list | `{"tasks": [TaskRecord, ...], "limit": 50}` |
| tasks show | `{"task": TaskRecord}` |
| events | `{"task_id": "…", "run_id": "…", "events": [EventRecord, ...], "next_after_seq": 5, "has_more": false}` |
| recover | `{"run_id": "…", "state": "RECOVERY_REQUIRED", "outcome": "INTERRUPTED"}` |

TaskRecord와 EventRecord는 설계의 동명 테이블 필드를 JSON object로 직렬화한다. `payload_json`만 응답에서 JSON object인 `payload`로 변환한다. SQLite 내부 row/연결 객체는 노출하지 않는다. nullable 값은 JSON null, timestamps는 UTC 문자열이다.

events는 limit+1개를 읽어 has_more를 정하고 응답은 limit개까지 반환한다. next_after_seq는 마지막 반환 event.seq이며, 빈 목록이면 요청한 after-seq를 유지한다. 빈 tasks/events는 성공이며 ID가 없는 task/run, task-run 연결 불일치는 오류다. 추가 pagination은 seq cursor로만 수행한다.

조회 및 정상 recover는 exit 0이다. 실패한 작업의 정상 run report는 exit 1이다. 인자/DB/Git/잠금 등 명령 처리 오류는 exit 2이며 stdout은 비우고 stderr에 아래 JSON만 쓴다. Ctrl+C는 기록 가능 여부와 무관하게 exit 130이며 성공으로 보고하지 않는다.

```json
{"error": {"code": "RUN_BUSY", "message": "Run is owned by another coordinator."}}
```

오류 code는 INVALID_ARGUMENT/NOT_FOUND/DB_NOT_FOUND/SCHEMA_UNSUPPORTED/DB_BUSY/STORE_IO/RUN_BUSY/HOST_MISMATCH/GIT_FAILED다. message는 원본 subprocess stderr/환경/SQL 매개변수를 붙이지 않는 정해진 안내 문구다. 저장 장애 때문에 최종 report를 확정할 수 없는 경우 STORE_IO, exit 2다.

## 정상 실행 이벤트 순서

추가 실행이 없는 첫 시도에서 다음 순서를 고정한다. timestamp는 실행 시각이며 예시 seq에 빈 번호가 없다. stage 완료는 해당 stage의 active state를 유지하고, 최종 state/outcome은 RUN_FINISHED에서 함께 확정한다.

| seq | 편집·테스트·리뷰 정상 경로 | 계획 전용 경로 |
|---|---|---|
| 1 | RUN_CREATED | RUN_CREATED |
| 2 | WORKSPACE_READY | WORKSPACE_READY |
| 3 | STAGE_STARTED / developer | STAGE_STARTED / developer |
| 4 | STAGE_COMPLETED / developer | STAGE_COMPLETED / developer |
| 5 | STAGE_STARTED / test | RUN_FINISHED / PLAN_ONLY |
| 6 | STAGE_COMPLETED / test | — |
| 7 | STAGE_STARTED / review | — |
| 8 | STAGE_COMPLETED / review | — |
| 9 | RUN_FINISHED / READY_FOR_HUMAN_REVIEW | — |

attempts는 developer STAGE_STARTED를 처음 commit할 때만 증가한다. retry 결정이나 같은 operation_key 재전송은 증가시키지 않는다. 실패 phase는 STAGE_FAILED를 남기고 재시도 가능 시 RETRY_SCHEDULED → 다음 developer 시작으로 연결한다. timeout은 재시도 경로에 들어가지 않는다.

## 수용 매트릭스

각 시나리오는 실제 임시 SQLite 파일과 호출 횟수를 남기는 fake worker/test를 사용한다. 모델의 문장 평가는 통과 증거로 사용하지 않는다. D/T/R은 developer/test/reviewer 실행 횟수다.

| ID | 입력·상황 | 반드시 확인할 결과 | 구현 작업 |
|---|---|---|---|
| A01 | 계획 전용 run 후 새 CLI에서 조회 | PLAN_ONLY, attempts=1, D/T/R=1/0/0, events=5, ID/경로 보존 | 1/4/5 |
| A02 | 편집 작업의 테스트·리뷰 통과 | READY_FOR_HUMAN_REVIEW, D/T/R=1/1/1, events=9, main 미변경 | 4 |
| A03 | max_attempts=2, 테스트 연속 실패 | FAILED/TEST_FAILED, attempts=2, D/T/R=2/2/0 | 4 |
| A04 | 첫 리뷰 수정 요구, 둘째 승인 | attempts=2, D/T/R=2/2/2, 기존 task/run 안에서 순서 보존 | 4 |
| A05 | APPROVED와 CHANGES_REQUESTED 동시 응답 | WAITING_USER/REVIEW_UNDECIDED, READY 아님 | 0/4 |
| A06 | worker 124 또는 test TimeoutExpired, retry 여유 있음 | RECOVERY_REQUIRED/INTERRUPTED, 이후 단계·재시도 0회 | 4 |
| A07 | 최초 RUN_CREATED commit 실패 | STORE_IO, exit 2, Git/D/T/R 모두 0회, 성공 report 없음 | 1/4 |
| A08 | developer 실행 완료 후 결과 commit 실패 | D=1, T/R=0, STORE_IO/exit 2; DB의 마지막 상태는 active, 명시 recover 후 RECOVERY_REQUIRED | 4 |
| A09 | fake owner 실행 중 조회와 recover 시도 | 조회는 성공, recover는 RUN_BUSY/exit 2, owner state/event 수 불변 | 3/5 |
| A10 | coordinator 강제 종료 후 조회·recover 두 번 | 조회만으로 상태 변경 없음, 첫 recover만 event 추가, worker 실행 횟수 불변 | 3/4/5 |
| A11 | 같은 operation_key 동일/다른 내용 재전송 | 동일 내용은 seq/attempt/row 수 불변; state/outcome 등 하나라도 다른 내용은 거절 | 1/2 |
| A12 | 한글·공백 경로, 알려진 token 패턴, 큰 요청 | 요청 snapshot ≤8192 bytes/유효 UTF-8, 알려진 비밀 원문 없음, 원래 실행 입력은 그대로 | 1/2/4 |
| A13 | 없는 DB/더 높은 schema/잘못된 ID/다른 run 연결 | 명시 오류, 원본 DB/schema 불변, 외부 호출 0회 | 1/5 |
| A14 | candidate DB에 쓰고 stable DB 비교 | stable tasks/events/파일 내용 불변, doctor는 DB를 열지 않음 | 5 |

## 제출할 증거

- 각 수용 ID와 대응 테스트 이름/결과를 `docs/verification.md`에 매핑한다.
- Windows/Ubuntu × Python 3.11/3.13 CI의 commit SHA와 실행 URL을 기록한다.
- 로컬 Windows/WSL의 Python/CLI 버전, pytest 요약, candidate DB 조회 예제를 남긴다.
- fake subprocess, 실제 인증 모델 호출, 편집 모드 수용을 별도 열로 기록한다.
- 변경 diff/미해결 사항과 운영자 복구 절차를 함께 검토한다. DB나 로그 원문은 공개 PR에 첨부하지 않는다.
