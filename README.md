# AI Agent Office

Local-first orchestration of coding agent CLIs with a browser-based local studio, persistent task history and a separate read-only observer.

The selected local or SSH-connected host owns its folders, processes, requests and decision journal. The collaboration inbox supports operator answers and one-time decisions. Unattended delegation and self-updating are planned features.

## 로컬 작업실 시작

아래 명령을 설치된 환경에서 실행한 뒤 터미널에 표시되는 주소를 브라우저에서 여세요. Windows와 Linux에서 같은 명령을 사용합니다.

```shell
agent-office --db ../office-data/tasks.sqlite3 studio
```

1. **작업공간 등록**에서 이름과 폴더의 절대 경로를 한 줄에 하나씩 입력합니다.
2. 주 폴더와 함께 쓸 폴더를 선택합니다. 한 번의 실행에서는 같은 Git 체크아웃의 현재 커밋에 포함된 폴더들을 선택할 수 있습니다. 이 목록은 작업자에게 전달하는 작업 범위입니다. CLI와 테스트는 별도 worktree의 저장소 루트에서 실행되며 실제 파일 접근 권한은 CLI와 OS 설정에 따릅니다.
3. 요청을 입력하고 **작업 시작**을 누릅니다. 기본값은 계획 작성입니다. 파일 수정과 테스트·리뷰까지 맡길 때에는 **개발·검증 맡기기**를 선택합니다.
4. 실행 상세에서 진행 상태를 확인하고 **실행 중지**를 요청할 수 있습니다. 작업실 명령이 실행 중이면 브라우저를 닫아도 작업이 계속됩니다. 같은 주소로 돌아오면 변경 파일, diff, 테스트 결과와 리뷰 의견을 확인할 수 있습니다.

기본 주소는 `http://127.0.0.1:8766/`입니다. `studio --port 0`을 사용하면 빈 포트를 고릅니다. 실제 작업은 선택한 PC에 설치한 Codex와 Antigravity 계정을 사용하므로 신뢰하는 저장소에서 시작하세요. [제품 구상](docs/product-vision.md) · [구현 계획](docs/superpowers/plans/2026-11-19-local-studio.md)

## 원격 호스트에서 작업하기

두 컴퓨터에 이 버전의 Agent Office를 설치합니다. 로컬에서는 Windows 기본 OpenSSH 클라이언트 또는 Linux의 `/usr/bin/ssh`를 사용합니다. 사용할 SSH 별칭(예: `build-box`)은 이미 호스트 키를 확인했고, 해당 계정에 비대화형으로 접속할 수 있어야 합니다. SSH 연결에서 원격 루프백으로의 TCP 전달도 허용되어 있어야 합니다. Git과 작업에 사용할 CLI·테스트 환경은 원격 호스트에 준비합니다.

**1. 원격 호스트**에서 작업실을 시작하고 계속 실행해 둡니다. 출력되는 포트와 32자리 `Journal ID`를 확인합니다.

```shell
agent-office --db ../remote-office/tasks.sqlite3 studio --port 8766 --accept-remote
```

**2. 로컬 컴퓨터**에서 다음 예시의 SSH 별칭과 저널 ID를 실제 값으로 바꿔 실행합니다. `--remote`는 호스트마다 한 번씩 지정할 수 있습니다. 다시 시작할 때도 같은 원격 저널을 지정하면 기존 실행 주소가 유지됩니다.

```shell
agent-office --db ../local-office/tasks.sqlite3 studio --remote build-box 8766 0123456789abcdef0123456789abcdef
```

**3. 브라우저**에서 터미널에 표시된 로컬 주소를 열고 실행 호스트로 `build-box`를 선택합니다. 해당 호스트의 작업공간과 폴더가 표시됩니다. 처음이면 **작업공간 등록**에 원격 컴퓨터의 절대 경로를 한 줄씩 입력합니다. 주 폴더와 추가 폴더를 선택하고 작업을 시작하면 worktree, 개발자·리뷰어 프로세스, 테스트와 결과 기록은 모두 원격에 생깁니다.

네트워크가 끊기거나 로컬 작업실을 종료해도 **원격 작업실 프로세스가 살아 있으면 실행은 계속됩니다.** 연결이 복구되면 기존 상세 주소를 열어 같은 호스트·실행 ID의 상태와 결과를 확인합니다. 작업 접수 응답이 유실됐을 때에는 같은 ID로 접수 여부를 확인할 수 있습니다. 원격 작업실 자체를 종료한 경우에는 해당 호스트의 중지·복구 규칙이 적용됩니다.

양쪽 HTTP 서버는 루프백에서 동작합니다. 로컬 작업실은 OpenSSH의 표준입출력 전달을 이용해 지정한 호스트의 작업실에만 요청합니다. 원격 포트에 다른 저널이 열려 있으면 요청이 거부됩니다. 저널 ID는 저장소 식별자이며 접속 인증은 SSH가 담당합니다. [OpenSSH 매뉴얼](https://man.openbsd.org/ssh.1) · [원격 실행 구현 계획](docs/superpowers/plans/2026-11-19-remote-studio.md)

## 역할별 엔진 선택과 호스트별 검증

실행 호스트를 고른 뒤 **개발자 엔진**과 **리뷰어 엔진**을 선택합니다. Codex, Antigravity, Claude Code, Gemini CLI, Cline CLI를 각 역할에 배정할 수 있습니다. 기본값은 개발자 Codex, 리뷰어 Antigravity입니다. 계획 작성은 개발자만 실행하고, 개발·검증에서는 선택한 개발자가 작업한 뒤 저장소 테스트와 선택한 리뷰어의 검토가 이어집니다. 실행 상세에는 접수 당시의 배정이 남습니다.

**호스트별 엔진 진단**에서 설치 여부, 버전, CLI가 안내하는 비대화형 옵션과 인증 상태를 확인합니다. 읽기·수정·리뷰 검증에는 해당 호스트에서 별도 예제로 확인한 결과와 시각을 표시합니다. 새 엔진을 설치했거나 실행 환경이 달라지면 현재 환경의 근거를 확인하세요.

Gemini와 Cline은 ACP로 연결합니다. Cline의 작업공간 파일 읽기·수정 허가는 도구 규약을 확인한 **CLI 3.0.74**에 적용합니다. 설치한 버전이 다르면 해당 호스트의 진단에 표시됩니다. [연결 방식과 지원 범위](docs/engine-protocols.md)

운영자가 실검증을 승인한 경우에만 **실행할 호스트**에서 그 작업실과 같은 DB 경로로 아래처럼 실행합니다. 이 명령은 임시 Git 저장소에서 지정한 CLI를 호출하므로 계정 사용량이 발생할 수 있습니다. `gemini` 자리에 원하는 엔진 ID를, 마지막 인자에 확인할 기능(`read`, `edit`, `review`)을 넣습니다.

```shell
agent-office --db ../remote-office/tasks.sqlite3 verify-engine gemini read
```

원격 작업실에서는 두 컴퓨터에 이 버전을 설치합니다. 원격 호스트의 진단과 검증 내역은 해당 호스트의 저널에서 가져옵니다. [엔진 가이드](docs/engines.md) · [역할별 엔진 구현 계획](docs/superpowers/plans/2026-11-19-engine-expansion.md)

## 협업 요청함

새 작업에서 **협업 요청함**을 켜고 시작합니다. 작업자가 판단을 기다리면 실행 중인 호스트의 요청함에 작업·역할·엔진과 질문 또는 권한 요청의 원문이 표시됩니다. 원격 호스트는 브라우저 상단에서 해당 SSH 별칭을 선택합니다.

| 요청 | 사용자 동작 | 작업자가 받는 내용 |
| --- | --- | --- |
| 기술 질문 | 예를 들어 “기존 주문에도 적용할까요?”에 답변을 입력하고 전송 | 원래 대화에 이어지는 기술적 답변 |
| 권한 요청 | 제안한 파일 작업의 경로와 내용을 확인한 뒤 이번 한 건을 승인하거나 거절 | 제공자 프로토콜의 일회성 결정 |

실행 상세의 요청 이력에서 **결정 접수**와 **작업자에게 전달됨**을 확인할 수 있습니다. 연결이 끊기면 원래 호스트·실행 주소로 돌아옵니다. 이미 저장된 결정은 유지되고 같은 요청에 다른 결정을 보내면 충돌로 표시됩니다. 중지·시간 초과·작업자 종료 후에는 남은 요청이 만료됩니다. 결정 시각이 있고 전달 시각이 없는 만료 기록은 작업자가 결정을 받았는지 확인할 수 없는 경우입니다.

질문을 받은 동안에도 실행 제한 시간은 흐릅니다. 기본 작업자 호출은 900초이며 기술 질문은 호출당 최대 8회입니다. 요청·답변은 각각 UTF-8 8,192바이트까지 원문으로 기록합니다. 사전 위임과 부재중 자동 응답은 후속 범위입니다.

| 엔진 | 기술 질문 처리 | 사용자에게 전달할 수 있는 권한 요청 |
| --- | --- | --- |
| Codex | 작업실 요청함 모드에서 app-server의 같은 스레드로 후속 턴 전달 | 제공자가 요청한 작업 폴더 안의 파일 변경 한 건 |
| Cline CLI 3.0.74 | 실행 중인 같은 ACP 세션에서 후속 턴 진행 | 역할이 허용하는 `read_files` 및 `editor` 호출 한 건 |
| Gemini CLI | 실행 중인 같은 ACP 세션에서 후속 턴 진행 | 기존 호스트 정책 적용. 추가 대화형 권한 요청은 거절 |
| Claude Code · Antigravity | 완료된 질문 턴의 대화 UUID를 지정해 같은 작업 폴더에서 재개 | 현재 배치 연결은 기술 답변을 전달 |

요청함에는 제공자가 실제로 승인을 요구한 작업이 나타납니다. 예를 들어 Codex가 호스트 정책에 따라 작업 폴더 안의 편집을 이미 허용하면 그 편집은 바로 진행됩니다. 요청함은 신뢰하는 개인용 호스트와 기존 SSH 연결을 사용합니다. 운영체제·제공자의 접근 제한과 작업자의 배포·푸시·머지 제한은 계속 적용됩니다. [구현 계획](docs/superpowers/plans/2026-11-19-studio-inbox.md)

## Architecture

```text
User task -> Agent Office CLI
            -> Git worktree (isolated working directory)
            -> LangGraph
               -> Developer CLI (Codex / Antigravity / Claude Code / Gemini CLI / Cline CLI)
               -> Local test command (allow-edits mode only)
               -> Reviewer CLI (read-only)
               -> [rework on failure, with a maximum attempt limit]
            -> Report for human review
```

The orchestrator does not replace the internal agentic loops inside Claude Code or Codex. It treats each CLI as a worker.

## Requirements

- Python 3.11+ and Git installed locally.
- Codex and Antigravity CLI (`agy`) installed/authenticated for real runs with the initial engine profile. Legacy Claude remains available through explicit role overrides.
- Windows or Linux, with a local Git repository containing an initial commit.
- The CLI you select must be on `PATH`.

## Install

```bash
git clone https://github.com/tworimpa/ai-agent-office.git
cd ai-agent-office
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## Cross-platform verification (PR #1)

The CI workflow runs **Ubuntu + Windows**, Python **3.11 + 3.13**, without provider credentials. It verifies dependencies, unit tests, Git worktrees and native subprocess adapter communication.

- Windows: `powershell -ExecutionPolicy Bypass -File .\\scripts\\verify-windows.ps1`
- Linux: `bash scripts/verify-linux.sh`
- To include authenticated headless CLI checks: add `-Live` (Windows) or `--live` (Linux).
- Inspect available CLIs: `agent-office doctor`

Task and event queries, explicit interruption marking and candidate DB paths are described in the [storage guide](docs/storage.md). Use `--db` before the subcommand to keep development state separate from stable state. Inbox continuations belong to the original Studio run. A run marked for recovery requires inspection.

For read-only access to the journal, use `agent-office --db PATH serve --port 8765`. See the [observer guide](docs/observer.md) for selection, timeline updates and actual CLI acceptance limits.

See [verification guide](docs/verification.md). Real Codex/Antigravity checks require those CLIs installed and authenticated on the target PC; CI mocks do not validate live provider behavior.

## Try it

Default is **planning-only**, with edit permissions disabled.

```bash
agent-office run --repo . --task "Suggest the next milestone for Agent Office"
```

Opt in to edit the isolated Git worktree and run tests:

```bash
agent-office run --repo . --allow-edits \
  --task "Add one focused regression test and explain what changed"
```

By default development uses `codex` and review uses `antigravity` (`agy`). Override either role, for example with `--developer gemini --reviewer cline`. The other selectable engine is `claude`. Product workers do not merge/push/deploy their work. Codex's repository maintenance follows the owner's separate CI-gated workflow in AGENTS.md.

Use `agent-office engines` for fixed metadata-only diagnostics. The observer shows a startup snapshot for the two initial engines. Installation, authentication, advertised headless flags and actual task acceptance are distinct; metadata never marks a live task verified. See [engine guide](docs/engines.md).

**Important:** CLI sandbox modes are provider-specific; Claude's `acceptEdits` permission mode is **not a full OS/filesystem sandbox**. Prompts are not a security boundary. Only run trusted tasks on an appropriately isolated local account/environment, and do not expose sensitive credentials or production repositories. The default test command is `python -m pytest -q` (using the Agent Office Python interpreter). Changes to files outside the Git worktree can still be possible depending on CLI permissions and installed tools.

## Security / platform note

Tasks are transferred to CLI subprocesses through standard input rather than shell-interpolated command arguments. This helps with long prompts and Windows npm `.cmd` wrappers, but cannot eliminate every risk from third-party tools. On Windows, validate Codex's effective native sandbox before using `--allow-edits`. The worktree is not an operating-system sandbox.

## Roadmap

제품의 목표, 사용자 흐름과 단계별 완료 기준은 [AI Agent Office 제품 구상](docs/product-vision.md)에 정리했습니다.

- AO-000: LangGraph + Claude/Codex adapters + separate worktree + tests + human-reviewed result.
- AO-001: persistent task/run/event database, read-only queries and explicit recovery marking.
- AO-002: office observation UI, Gemini/Cline/Antigravity adapters and skills registry.
- AO-003: questions, approval inbox, session continuation, and delegated decisions within policy.
- AO-004: supervised self-improvement, regression benchmarks, candidate builds and safe promotion.

## Project rules

- Codex maintenance follows the owner's standing PR → latest-head CI → merge authorization in [AGENTS.md](AGENTS.md). Product workers still do not publish/merge their own work; deployment and security changes require separate authorization.
- Isolate development to a worktree; never edit the running Agent Office installation in place.
- Store per-task evidence including test results, git diffs and decision trail before promotion.
- Keep agent roles separate from the selected CLI engines.
- Fail closed on unrecognized reviewer verdicts or provider errors.

See [docs/architecture.md](docs/architecture.md) for the planned system and AO-000 scope.
