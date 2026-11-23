# Engine profiles and verification

## 작업실에서 보는 근거

선택한 호스트의 작업실은 `codex`, `antigravity`(실행 명령 `agy`), `claude`, `gemini`, `cline`을 표시한다. 새 작업의 역할 배정은 그 호스트에서 저장하고, 같은 실행 ID로 접수를 재확인할 때도 같은 배정을 사용한다. 이전 버전에서 접수한 기본 조합도 계속 열 수 있다.

| 표시 | 확인한 범위 |
| --- | --- |
| 설치·버전·CLI 옵션 | 해당 호스트에서 고정된 버전·도움말 명령으로 확인한 메타데이터 |
| 읽기 통과 | 임시 저장소의 임의 값을 읽어 정확히 반환하고 파일을 보존한 예제 |
| 수정 통과 | 지정 파일을 참조 값과 일치하게 수정하고 다른 파일을 보존한 예제 |
| 리뷰 통과 | 서로 다른 두 값을 실제로 읽고 차이를 설명하며 변경 요청 판정을 반환한 예제 |
| 미검증 | 현재 실행 환경과 기능에 대응하는 예제 결과가 없음 |

웹 화면의 조회는 유료 모델을 실행하지 않는다. 운영자가 해당 호스트에서 명시적으로 `verify-engine`을 실행하면 그 결과를 저널에 기록한다. `read`, `edit`, `review`는 각각 독립된 점검이다. 엔진 내부 대화 재개, 실시간 질문·승인, 호스트 밖의 파일 접근 격리는 이 예제의 검증 범위에서 제외한다. [추가 엔진 프로토콜](engine-protocols.md)

## Initial profile

Owner selection: Codex + Antigravity. Default Developer=codex, Reviewer=antigravity. Roles can be swapped using --developer/--reviewer. Legacy Claude adapter and stored task records remain supported when explicitly selected.

```text
agent-office engines
agent-office doctor --require codex antigravity
agent-office --db ../candidate/tasks.sqlite3 run --repo . --task "Propose one small improvement without modifying files"
```

The first two commands inspect metadata only. The run command calls the provider and consumes its plan/API usage. Its default is planning-only; it does not execute tests or a reviewer. Editing still requires --allow-edits in an appropriately isolated disposable worktree; provider permission modes are not interchangeable OS sandbox guarantees.

## Antigravity transport

The adapter calls official `agy`, not the IDE executable. It sends a single stdin NDJSON user message and expects one final result. Both plan/accept-edits modes retain --sandbox and --disable-slash-commands. No skip-permissions flag, persistent settings edit, new login or credentials injection is used.

The [official headless protocol](https://antigravity.google/docs/cli/headless/) defines status and response in the terminal result. The adapter fails closed for non-SUCCESS, malformed/missing/multiple results, empty/nonstring response, error fields or reported tool errors even if process exit is0. INTERRUPTED/timeout require recovery review. This deliberately rejects some recoverable tool-error runs instead of automatically accepting an incomplete review. It does not parse provider responses as human permission approval.

## Status snapshot

The observer collects known fixed version/help commands and Codex login status once at startup. GET /api/engines only serves that cached snapshot; opening or refreshing a page never performs model calls or repeats CLI probes. Timestamp/platform and 'startup diagnostic' labels make freshness explicit. Restart the observer to recollect, or run engines in a terminal for a fresh independent report.

No credential file or environment inventory is read. Only extracted semantic versions and authentication enums are exposed, not raw output, account details, executable paths or tokens. Antigravity help provides no standalone authentication-status command in the inspected CLI, so its login state remains unknown. IDE login and an executable being installed are not evidence of CLI authentication or successful file/tool execution.

Vendor executable resolution uses explicit absolute PATH entries and excludes the current directory, blank and relative entries. A same-name launcher placed in the viewed repository is not implicitly selected. The status snapshot is fetched independently of database metadata so it remains visible if the DB becomes unavailable after server startup.

The initial Windows probe found Codex0.163.0 with authenticated metadata and Antigravity1.4.2 with advertised headless flags; a later probe found agy1.5.2. Subsequent owner-authorized real checks passed Windows Antigravity reading and a WSL Codex0.147.0 + isolated Antigravity1.5.2 edit/test/review fixture. Windows Codex file-tool execution still failed. The installed WSL agy1.1.13 was not the tested 1.5.2 binary, so default WSL PATH readiness is not established. See [real acceptance evidence and limits](ao-002b-live-acceptance.md). These results do not automatically change cached engine-card metadata.

Real acceptance must be separately authorized and use a bounded fixture with file/result checks. `-Live`/`--live` verification scripts are opt-in provider communication checks; inspect the returned task response and use independent evidence for task success. They are not run by CI or by engine-card polling.


## 작업 중 진행 기록과 엔진 관리

작업실에는 실행별로 마지막 공급자 활동과 시각이 표시된다. 로컬 페이지의 /api/runs/<id>/stream은 SSE의 Last-Event-ID 이후 보존된 기록을 재전송한다. 각 실행은 최근 128개 기록만 보존한다. SSH 작업실에서는 실행 상세를 갱신할 때 마지막 확인 기록을 읽는다. 조용한 엔진에서는 다음 확인 가능한 출력이 올 때까지 마지막 시각을 유지한다. 실패한 CLI와 감독 프로세스는 표준 오류의 끝 16 KiB에서 확인한 모델·인증·네트워크·가져오기 오류 등의 분류를 표시한다. 프롬프트, 터미널 원문과 도구 입력은 이 기록에 저장하지 않는다.

로컬 Studio 엔진 진단에서 **공개 버전 확인**을 누르면 npm의 공식 패키지 데이터 또는 Google CLI 변경 내역을 읽는다. 설치된 정식 버전에 새 버전이 있으면 운영자가 업데이트할 수 있다. 같은 작업실에 활성 실행이 있거나 같은 사용자로 실행 중인 Office 공급자 작업이 있으면 요청을 거절한다. npm 엔진은 활성 실행 파일과 전역 패키지 경로를 대조한 뒤 표시된 버전을 지정한다. Claude와 Antigravity는 설치된 CLI의 공식 update 명령을 사용한다. 업데이트가 끝나면 설치 버전을 다시 조회하고, 목표 버전과 일치해야 완료로 표시한다. 외부 터미널에서 직접 시작한 CLI는 Office 실행 잠금에 참여하지 않는다. [Antigravity 업데이트](https://antigravity.google/docs/cli/commands/changelog) · [Gemini CLI 설치](https://geminicli.com/docs/get-started/installation/) · [Cline CLI](https://cline.bot/cli)
