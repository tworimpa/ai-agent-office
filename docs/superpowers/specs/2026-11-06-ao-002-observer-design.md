# AO-002A 로컬 오피스 관찰 화면

사용자가 승인한 작업 목록 → 상세 → 이벤트 타임라인을 구현한다. 실제 Codex 읽기 작업 1건의 CLI/DB 영수증을 먼저 확인한다. 구현 선택은 아래와 같이 정하며 원격 게시·실제 편집 실행은 포함하지 않는다.

## 접근과 범위

- Python stdlib HTTP 조회 서버 + 패키지에 포함된 HTML/CSS/ES modules를 사용한다. 기존 Python 설치만으로 Windows/Linux에서 실행하고 별도 Node 서버/프레임워크 의존성을 만들지 않는 선택이다.
- FastAPI/SPA 분리 구조는 추가 서비스·빌드가 필요해 첫 관찰 화면에는 사용하지 않는다. 외부 Sites hosting은 PC의 DB를 직접 조회하는 이 작업의 로컬 전용 요구와 맞지 않는다.
- `agent-office --db PATH serve --port 8765`; bind는 127.0.0.1로 고정하고 host 옵션은 제공하지 않는다. port 0은 자동 할당 테스트/충돌 회피에 허용한다.
- 조회 전용이다. worker 실행/복구/취소/DB 경로 변경/파일 읽기 endpoint는 만들지 않는다. DB가 없거나 유효하지 않으면 서버를 시작하지 않는다.

## HTTP 계약

`ObserverServer(db_path: Path, port: int)`는 ThreadingHTTPServer를 구성한다. 요청마다 TaskStore(readonly=True)를 새로 열고 종료한다. DB 초기화·migration·복구나 CLIAdapter 호출을 하지 않는다.

| 경로 | 응답 |
|---|---|
| GET / | 고정 index.html |
| GET /app.js, /state.js, /style.css | 고정 패키지 asset만 반환 |
| GET /api/meta | `{mode:"read-only", database:<파일명>, task_limit:200}` |
| GET /api/tasks | `{tasks:[TaskRecord...], limit:200}` 최근 200건 |
| GET /api/tasks/<32 hex id> | `{task:TaskRecord}` |
| GET /api/tasks/<id>/events?run_id=<id>&after_seq=0&limit=100 | AO-001 events envelope와 동일 |

task list는 updated_at 내림차순. events limit 기본100/최대500, cursor는 SQLite signed int 범위. 허용되지 않은/중복 query parameter는 400, 잘못된 ID는400, 없는 task/run은404, DB missing/잠김/schema/I/O는503, 알 수 없는 경로404다. 오류는 고정 code/message JSON으로 private raw exception을 노출하지 않는다. HEAD는 같은 headers와 빈 body, 쓰기 method는405다. 항상 no-store, nosniff, same-origin connect/script/style, frame-ancestors none, 외부 리소스 없는 CSP를 보낸다.

Host는 서버의 실제 포트에 대응하는 127.0.0.1 또는 localhost만 허용한다. API의 외부 Origin과 Sec-Fetch-Site:cross-site를 거절하고 CORS를 열지 않는다. 외부 페이지에서 사용자가 클릭한 첫 화면 navigation은 허용하되 framing은 금지한다. 고정 asset map으로 경로 탐색/임의 파일 다운로드를 막고, 요청 로그에 URL/DB 내용을 출력하지 않는다.

서버가 읽는 DB가 실행 중 다른 schema/파일로 교체되면 갱신 오류를 표시한다. 열려 있던 화면이 DB를 다시 생성하지 않는다. 이 서버는 개인 PC 관찰용이며 외부 배포용 서버가 아니다.

## 화면과 갱신

한국어, 밝은 배경의 차분한 작업 공간. 헤더에 읽기 전용/연결 상태/갱신 시각, 요약에 실행 중·확인 필요·실패·최근 작업 수를 표시한다. 아래는 작업 목록, 상세, 타임라인 3개 영역이다. 작은 화면에서는 DOM 순서대로 쌓고 상세로 이동하는 skip link를 둔다. 최근200건임을 명시하고 검색·상태 필터는 이 범위에만 적용한다.

task는 native button으로 선택하고 aria-pressed를 갱신한다. 목록 갱신 시 기존 button을 재사용해 focus가 사라지지 않게 한다. 선택은 URL의 task query로 보존한다. 화면을 다시 열어도 같은 작업을 조회한다. 저장된 task ID가 최근200건 밖에 있어도 상세 직접 조회를 시도한다.

선택 상세에는 request_snapshot, state/outcome, task/run ID, 역할별 engine, workspace/branch, attempts/max_attempts, 시각을 표시한다. 코드 문자열·경로·요청은 textContent로 넣고 HTML로 해석하지 않는다. PLAN_ONLY는 '계획만 작성', READY_FOR_HUMAN_REVIEW는 '검토 준비'로 명확히 구분한다. 기록만으로 설치/로그인/실제 실행 성공을 추정하지 않는다.

3초마다 조회하되 이전 요청 완료 후 다음 timer를 예약한다. 숨긴 탭은 polling을 멈추고 다시 표시할 때 갱신한다. 각 fetch는 10초 timeout과 AbortController를 사용한다. task 선택 변경 시 이전 fetch를 취소하고 generation token으로 오래된 응답을 폐기한다.

선택 task의 run이 바뀌면 cursor/events를 초기화한다. events는 seq로 중복 제거하며 1회 최대10페이지를 가져오고 has_more이면 다음 cycle에 이어간다. 화면에는 최근1000개까지만 보존하고 생략 안내를 표시한다. DB 쿼리 결과가 비어 있으면 cursor를 유지한다. 타임라인 전체를 aria-live로 읽지 않고 상태 변경·오류만 한 곳에서 알린다.

빈 목록, 필터 결과 없음, DB 장애/서버 끊김, 존재하지 않는 task를 구분한다. 오류 시 마지막 데이터와 마지막 성공 시각을 남기되 '현재 상태'로 오인하지 않게 오래된 데이터임을 표시한다. 재연결 성공 시 현재 DB 상태로 복원한다.

## 파일과 수용 기준

- `observer.py`: 고정 HTTP surface/DB projection/수명.
- `web/index.html`, `style.css`, `state.js`, `app.js`: 화면과 순수 event/cursor 상태 함수.
- CLI serve dispatch 및 setuptools package-data 추가. 포트/DB 인자 검증은 서버 bind 전에 수행한다.
- pytest에서 실제 로컬 HTTP GET/HEAD/405/Host/Origin/경로 탐색/민감 오류/읽기 부작용/DB 삭제·교체를 검증한다.
- Node 기본 test runner로 event dedup/run reset/cursor 검증을 수행한다. 외부 브라우저 자동화 허용 시 desktop/mobile/focus/새로고침/갱신을 실제 브라우저에서 확인한다. 사용할 수 없으면 그 제한을 보고하고 browser 검증 완료로 주장하지 않는다.
- wheel에 asset을 포함하고 editable checkout 밖에서도 serve 가능함을 검증한다. Windows/Linux 기존 Python matrix와 신설 테스트를 통과시킨다.
- 실제 모델 receipt는 fake fixture/데모와 분리한다. 원격 push/PR/merge/호스팅 게시를 자동으로 하지 않는다.
