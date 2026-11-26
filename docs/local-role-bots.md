# 로컬 역할 봇과 공유 패키지 설계

Agent Office 사용자는 자기 PC에 이름과 업무 지침을 가진 봇을 만들고, 같은 Workspace의 조정자·개발자·리뷰어 자리에 배정할 수 있다. 예를 들어 Windows와 Linux를 지원하는 프로젝트라면 개발 봇에는 경로 처리 지침을, 리뷰 봇에는 두 운영체제의 회귀 사례를 확인하는 지침을 줄 수 있다. Studio에서 평소처럼 의뢰하면 다음 작업부터 이 팀이 담당한다. [구현 및 후속 설계 이슈 #46](https://github.com/tworimpa/ai-agent-office/issues/46)

## 내 PC에서 첫 작업 맡기기

**Studio에서 만들기.** 로컬 실행 호스트를 선택하고 등록된 Workspace를 연다. **작업실 설정 → 역할 봇**에서 봇 ID·이름·역할·업무 지침을 입력해 **봇 만들기**를 누른다. 해당 역할의 선택 목록에서 방금 만든 봇을 골라 **역할에 배정**을 누른다. 조정자·개발자·리뷰어에 원하는 봇이 보이면 조정자에게 새 의뢰를 보낸다. 아래 CLI와 Studio는 같은 등록·배정을 읽는다.

**CLI에서 만들기.** Windows와 Linux에서 같은 팀을 구성하는 예다.

1. Agent Office가 설치된 환경에서 아래 명령을 실행한다. 표시된 주소를 브라우저로 열고, 처음이라면 **작업공간 등록**에서 작업할 로컬 Git 저장소를 등록한다. CLI와 Studio는 같은 호스트에서 같은 저널 파일을 사용해야 한다. 아래 두 터미널은 같은 디렉터리에서 연다.

   ```shell
   agent-office --db ../office-data/tasks.sqlite3 studio
   ```

2. 두 번째 터미널에서 Workspace ID를 찾는다. 아래 예시의 `WORKSPACE_ID`를 출력에 나온 값으로 바꾼다. 명령은 Windows PowerShell과 Linux 셸에서 동일하다.

   ```shell
   agent-office --db ../office-data/tasks.sqlite3 bots workspaces
   ```

3. 역할에 맞는 봇을 만들고 배정한다. 봇은 여러 개 만들어 두고 Workspace에서 역할당 한 명을 선택할 수 있다.

   ```shell
   agent-office --db ../office-data/tasks.sqlite3 bots create --workspace-id WORKSPACE_ID --id release-lead --role coordinator --name "릴리스 조정자" --instructions "담당자에게 원 요청, 두 운영체제에서 확인할 항목, 완료 조건을 함께 인계한다."
   agent-office --db ../office-data/tasks.sqlite3 bots create --workspace-id WORKSPACE_ID --id path-dev --role developer --name "경로 처리 개발자" --instructions "Windows와 Linux에서 재현되는지 확인하고 파일 경로에 공백이 있는 경우를 포함한다."
   agent-office --db ../office-data/tasks.sqlite3 bots create --workspace-id WORKSPACE_ID --id portability-review --role reviewer --name "호환성 리뷰어" --instructions "두 운영체제의 경로 처리 회귀를 검토하고 테스트 근거와 미확인 항목을 구분한다."
   agent-office --db ../office-data/tasks.sqlite3 bots assign --workspace-id WORKSPACE_ID --role coordinator --id release-lead
   agent-office --db ../office-data/tasks.sqlite3 bots assign --workspace-id WORKSPACE_ID --role developer --id path-dev
   agent-office --db ../office-data/tasks.sqlite3 bots assign --workspace-id WORKSPACE_ID --role reviewer --id portability-review
   agent-office --db ../office-data/tasks.sqlite3 bots list --workspace-id WORKSPACE_ID
   agent-office --db ../office-data/tasks.sqlite3 bots show --workspace-id WORKSPACE_ID --id portability-review
   ```

4. 같은 Studio Workspace의 **조정자**에게 “공백이 있는 파일명을 Windows와 Linux에서 보존하도록 수정하고 테스트해 줘”라고 의뢰한다. **파일 수정 허용 → 계획을 보고 시작**을 선택하면 조정자가 제안한 담당자, 선행 작업과 완료 기준이 열린다. 내용을 확인하거나 수정한 뒤 승인한다. 개발·테스트·독립 리뷰가 끝나면 같은 실행에서 계획의 완료 기준별 검증 결과와 변경 후보를 확인한다. [Workspace 사용 안내](../README.md#조정자에게-작업-맡기기)

지침을 바꿀 때는 새 ID로 봇을 만든 뒤 같은 역할에 배정한다. 기본 배정으로 되돌리려면 해당 역할의 `office-coordinator`, `office-developer`, `office-reviewer`를 배정한다. 이미 접수된 작업은 접수 당시의 정의를 계속 사용한다.

봇 생성과 배정은 작업을 실행하는 PC의 로컬 Studio에서 관리한다. SSH로 작업을 조회하면 양쪽 Studio가 지원할 때 실행에 고정된 봇 ID와 해시도 전달한다. 이전 버전 Studio에서는 기존 형식으로 담당자 이름을 확인할 수 있다.

## 봇의 로컬 계약

| 개념 | 소유자와 수명 | 예 |
| --- | --- | --- |
| 역할 | Office가 정의하는 책임과 결과. Workspace마다 각 역할에 봇 하나를 배정한다. | `coordinator`, `developer`, `reviewer` |
| 봇 정의 | Workspace에 등록한 고유 ID, 표시 이름, 역할, 업무 지침. 같은 역할에 여러 후보를 둘 수 있다. | `portability-review`, 호환성 리뷰어 |
| 엔진과 스킬 | Workspace의 엔진·스킬 선택과 실행 시 허용 범위를 적용한다. 같은 봇을 쓸 때도 엔진을 바꿀 수 있다. | 기본 개발 Codex, 기본 리뷰 Antigravity(`agy`), 선택 가능한 Claude Code 등 |
| 실행 | 호스트가 작업별로 배정과 정의를 고정하고 기존 실행 저널에 결과를 연결한다. | 요청, 담당 봇 ID·이름·역할·정의 해시, 담당 엔진, 시도별 결과 |

**조정자**는 ‘계획을 보고 시작’으로 접수한 의뢰에서 자기 봇 지침, 원 요청, 배정된 담당자, 선택한 테스트와 기준 커밋의 파일 발췌를 받아 직접 계획을 작성한다. 이번 실행의 개발자 엔진·모델 설정을 사용하는 별도 읽기 전용 세션이며, 호출과 의견은 조정자 역할로 기록된다. 사용자가 승인한 판과 제품 선택 답변이 담당자에게 전달된다. 추가 지시로 계획을 갱신하면 새 판도 확인한 뒤 재개한다. 빠른 의뢰에는 기존 인계 지침을 적용한다. LangGraph가 실행 순서와 재시도를 관리하며, 도구 권한 요청은 기존 승인 절차를 따른다.

**개발자**는 원 요청, 조정자의 인계와 개발자 지침으로 작업한다. 읽기 전용 의뢰에는 조사 결과를, 수정이 허용된 의뢰에는 별도 worktree의 변경 후보를 제출한다. **리뷰어**는 배정된 리뷰 엔진에서 조정자의 인계와 리뷰어 지침으로 검토한다. 호스트가 실행한 테스트 결과와 리뷰 판정은 독립적으로 기록한다. 사람에게 필요한 제품 결정과 권한 승인은 Inbox에서 처리한다. [엔진](engines.md) · [실행 검증](verification.md) · [승인 결정](decisions.md)

로컬 정의의 스키마 버전은 `1`이다. `id`는 영문 소문자·숫자와 단어 사이의 하이픈으로 최대 64자, `name`은 한 줄 40자, `instructions`는 비어 있지 않은 UTF-8 텍스트 8,192바이트 이하다. 호스트는 `schema_version`, `id`, `name`, `role`, `instructions`, `builtin`으로 정의의 SHA-256 해시(`digest`)를 계산한다. 배정 영수증에는 세 역할의 ID·이름·역할·해시·내장 여부가 들어가며 실행에 사용할 전체 지침은 별도로 고정된다.

기존 Workspace는 내장 봇으로 시작한다. 개발자·리뷰어의 기존 Workspace 엔진·스킬 설정, 작업별 재지정, 검증 흐름을 계속 사용한다. 봇 관리는 로컬 Studio 또는 해당 호스트의 CLI에서 수행하고, 배정된 팀에는 Workspace Studio에서 의뢰한다. 독립 `agent-office run --repo …`는 지정된 저장소와 자체 엔진 설정으로 실행한다.

## Orca에서 채택한 관리 방식

아래 소스는 Orca 커밋 `47980a4`를 기준으로 확인했다. Orca에서는 템플릿이나 직접 작성한 봇을 대화·멘션으로 호출하거나 커넥터·일정으로 시작하고, 개별 워커의 worktree와 원래 대화로 돌아갈 수 있다. Office에서 이를 적용할 순서는 로컬 배정, 설치·갱신, 외부 트리거다. [Orca 봇 가이드](https://orca.dev/docs/bots)

| Orca 소스에서 확인한 동작 | Agent Office 설계 |
| --- | --- |
| 봇 홈은 사용자의 목적(`SOUL.md`)·기억(`MEMORY.md`)과 엔진용으로 생성하는 `AGENTS.md`·`CLAUDE.md`를 각각 관리한다. [봇 홈](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/main/bots/bot-home.ts#L154) | 1차에는 작성자가 업무 지침을 쓰고 Office가 적절한 엔진 호출에 주입한다. 개인 메모를 추가할 때는 별도 호스트 상태로 저장한다. |
| 엔진을 바꾸면 새 Home Chat을 열고 예전 대화는 기록에 남긴다. [엔진 전환](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/renderer/src/components/bots/bot-home-chat-confirm.tsx#L45) | 봇 ID는 담당자의 신원이고 공급자 세션은 해당 엔진의 실행 기록이다. 다른 엔진으로 새 작업을 시작할 때는 업무 지침과 필요한 증거를 전달한다. |
| 스킬 패키지 업로드 경로를 재사용하고 별도 봇 공유 링크로 버전을 가리킨다. 링크가 갱신돼도 기존 사본은 유지된다. [공유 클라이언트](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/main/bots/sharing/bot-share-client.ts#L142) · [사본의 업데이트](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/renderer/src/components/bots/sharing/BotShareFooter.tsx#L63) | 후속 공유 기능에서 카탈로그·공유 링크는 배포본을 찾는 용도로 쓰고, 설치 버전과 실행 중인 작업은 커밋·내용 해시로 고정한다. |
| 가져온 일정은 일시 중지 상태로 만들고, 적용 실패 시 부분 생성된 봇의 삭제를 시도한다. [패키지 적용](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/main/bots/bot-package-apply.ts#L47) | 향후 설치를 완료한 뒤 사용자가 이 PC의 연결과 일정을 선택한다. 중간 실패 시 이전 배정과 실행은 계속 사용할 수 있어야 한다. |

## Git 저장소에서 공유하는 다음 단계

작성자는 패키지 하나를 Git 저장소에 올린다. 사용자는 저장소 URL·하위 경로·버전을 선택해 자기 PC에 설치하고 원하는 Workspace와 역할에 연결한다. 카탈로그 없이도 설치할 수 있도록 설계하며 CODE-Skills는 패키지 검색·버전 알림·포함 스킬 탐색에 활용할 수 있다. 이 절차는 후속 구현 범위다.

| 제안하는 공개 패키지 경로 | 작성 내용 |
| --- | --- |
| `plugin.json` | Agent Plugins 1.0.0 매니페스트의 이름·설명·버전과 `extensions`의 Office 네임스페이스 |
| `io.github.tworimpa.agentoffice/bot.json` | Office 확장의 규격 버전·역할·표시 이름·업무 지침 위치·입출력·필요 기능. 후속 버전에 보고서·변경 후보·독립 리뷰 같은 결과 계약을 정의한다. |
| `io.github.tworimpa.agentoffice/instructions.md` | 엔진 간 공유할 UTF-8 업무 지침과 완료 기준 |
| `skills/<name>/SKILL.md` | 선택적으로 묶는 재사용 절차. 현재 Office에서 쓰는 역할 스킬은 버전을 고정한 텍스트 지침이다. |

Agent Plugins의 공개 규격은 스킬·MCP 같은 공통 구성요소와 클라이언트 소유 확장 자리를 정의한다. 위 네임스페이스와 역할·배정의 의미는 **Agent Office가 제안하는 확장**이다. Claude Code의 Markdown 하위 에이전트, Codex의 TOML 하위 에이전트는 각각 고유한 모델·도구 선택 방법을 가진다. Office는 어댑터가 엔진 차이를 처리하므로 봇 작성자가 두 정의를 중복 작성할 필요가 없다. Microsoft의 APM 예제는 한 저장소에 디자인 리뷰 담당 에이전트와 스타일 검사 스킬을 함께 배포하는 사례다. [Agent Plugins 규격](https://agent-plugins.org/specification) · [확장 규칙](https://agent-plugins.org/plugin-authors/client-extensions) · [Agent Skills](https://agentskills.io/specification) · [Claude Code 하위 에이전트](https://code.claude.com/docs/en/sub-agents) · [Codex 하위 에이전트](https://developers.openai.com/codex/subagents/) · [APM 예제 프로젝트](https://github.com/microsoft/apm-sample-package)

| 단계 | 완료를 판단할 동작 |
| --- | --- |
| 1. 로컬 생성·배정·실행 | 같은 PC에서 세 봇을 만들고 Studio에 의뢰한다. 한 역할의 배정을 바꾼 뒤 기존 실행과 다음 실행의 정의가 구분되고, 개발·리뷰 호출에 해당 지침이 적용된다. 모의 엔진은 이 경로를 시험한다. 인증된 실제 CLI 수행 여부는 엔진과 OS별로 별도 기록한다. |
| 2. 작성·검증·설치 | JSON Schema·예제·템플릿으로 제3자가 패키지를 만든다. 깨끗한 PC에서 URL로 설치하고 커밋과 내용 해시를 확인한다. 필수 기능을 제공하지 않는 엔진은 실행 전에 원인을 표시한다. 같은 업무를 Windows·Linux의 Codex·Claude Code에서 실제로 검증한다. |
| 3. 공유·갱신·관리 | 설치자가 업데이트 내용을 보고 적용하거나 직전 버전을 선택한다. 개인 메모·권한·과거 실행은 보존한다. Studio에서 설치된 사본, Workspace 배정, 진행 중인 워커를 각각 조회·관리한다. Orca 가져오기, 카탈로그와 자동화 연결은 이 단계에서 검토한다. |
