# 로컬 역할 봇과 공유 패키지 설계

Agent Office 사용자는 자기 PC에 이름과 업무 지침을 가진 봇을 만들고, 같은 Workspace의 조정자·개발자·리뷰어 자리에 배정할 수 있다. 예를 들어 Windows와 Linux를 지원하는 프로젝트라면 개발 봇에는 경로 처리 지침을, 리뷰 봇에는 두 운영체제의 회귀 사례를 확인하는 지침을 줄 수 있다. Studio에서 평소처럼 의뢰하면 다음 작업부터 이 팀이 담당한다. [구현 및 후속 설계 이슈 #46](https://github.com/tworimpa/ai-agent-office/issues/46)

클라이언트 간 교환에는 [Bot Definition 1.0](bot-spec-v1.md)을 사용한다. [클라이언트와 실행 API](bot-client.md)에 현재 웹 화면과 HTTP 계약을 정리했다.

## 내 PC에서 첫 작업 맡기기

봇별로 열어 둔 탭, 보내기 전 명령과 파일 편집 초안은 현재 브라우저 탭의 세션에 보관한다. 같은 탭을 새로고침하면 복원된다. 다른 창에서 같은 파일을 먼저 저장하면 서버에 저장된 내용과 내 초안을 비교한 뒤 어느 쪽으로 계속 편집할지 선택한다. 전송한 명령과 어명의 실제 배정·결과는 작업실 저널에서 다시 볼 수 있다.

**Studio에서 만들기.** 로컬 Workspace의 **봇 회의실 → 봇 만들기**를 연다. 이름과 역할(예: UX 리서처), 업무 지침을 입력해 회의실에 추가한다. ID는 자동 생성되며 직접 지정할 수도 있다. 멤버를 선택해 명령을 보내면 해당 봇이 이 실행의 담당자가 된다. 파일 변경에는 **파일 수정 허용**을 선택하고 **실행 기록 열기**로 진행 상황과 결과를 확인한다.

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

**파일 편집.** 회의실에서 멤버를 선택하고 오른쪽 파일 탐색기에서 `SOUL.md`(업무 지침), `MEMORY.md`(메모), 추가한 폴더와 텍스트 파일을 연다. 가운데 탭에서 편집하고 저장한 내용은 다음 의뢰부터 사용된다. 파일은 Workspace 저널에 저장되며 실행 시점의 사본과 해시를 보존한다. 다른 창이 먼저 저장하면 충돌을 표시한다. 새 명령의 담당자를 선택해도 작업실의 기본 배정은 유지된다.

봇 생성과 배정은 작업을 실행하는 PC의 로컬 Studio에서 관리한다. SSH로 작업을 조회하면 양쪽 Studio가 지원할 때 실행에 고정된 봇 ID와 해시도 전달한다. 이전 버전 Studio에서는 기존 형식으로 담당자 이름을 확인할 수 있다.

## 봇의 로컬 계약

| 개념 | 소유자와 수명 | 예 |
| --- | --- | --- |
| 실행 자리 | Office가 실행하는 단계. 작업실 설정에서 각 자리에 봇 하나를 배정한다. | `coordinator`, `developer`, `reviewer` |
| 봇 정의 | 작성자가 정한 이름, 자유 입력 역할, 지침과 참고 파일. | 다온 · UX 리서처 |
| 엔진과 스킬 | Workspace의 엔진·스킬 선택과 실행 시 허용 범위를 적용한다. 같은 봇을 쓸 때도 엔진을 바꿀 수 있다. | 기본 개발 Codex, 기본 리뷰 Antigravity(`agy`), 선택 가능한 Claude Code 등 |
| 실행 | 호스트가 작업별로 배정과 정의를 고정하고 기존 실행 저널에 결과를 연결한다. | 요청, 담당 봇 ID·이름·역할·정의 해시, 담당 엔진, 시도별 결과 |

**조정자**는 ‘계획을 보고 시작’으로 접수한 의뢰에서 자기 봇 지침, 원 요청, 배정된 담당자, 선택한 테스트와 기준 커밋의 파일 발췌를 받아 직접 계획을 작성한다. 이번 실행의 개발자 엔진·모델 설정을 사용하는 별도 읽기 전용 세션이며, 호출과 의견은 조정자 역할로 기록된다. 사용자가 승인한 판과 제품 선택 답변이 담당자에게 전달된다. 추가 지시로 계획을 갱신하면 새 판도 확인한 뒤 재개한다. 빠른 의뢰에는 기존 인계 지침을 적용한다. LangGraph가 실행 순서와 재시도를 관리하며, 도구 권한 요청은 기존 승인 절차를 따른다.

**개발자**는 원 요청, 조정자의 인계와 개발자 지침으로 작업한다. 읽기 전용 의뢰에는 조사 결과를, 수정이 허용된 의뢰에는 별도 worktree의 변경 후보를 제출한다. **리뷰어**는 배정된 리뷰 엔진에서 조정자의 인계와 리뷰어 지침으로 검토한다. 호스트가 실행한 테스트 결과와 리뷰 판정은 독립적으로 기록한다. 사람에게 필요한 제품 결정과 권한 승인은 Inbox에서 처리한다. [엔진](engines.md) · [실행 검증](verification.md) · [승인 결정](decisions.md)

로컬 저장 버전 1·2의 정의와 실행 해시를 유지한다. 중첩 파일, 빈 폴더, 외형 확장은 호스트 저장 버전 3으로 보관한다. 클라이언트는 공개 교환 형식의 specVersion을 읽는다. 파일 수와 바이트 수, 버전 호환 제약은 [공개 명세](bot-spec-v1.md)에 정리했다.

기존 Workspace는 내장 봇으로 시작한다. 개발자·리뷰어의 기존 Workspace 엔진·스킬 설정, 작업별 재지정, 검증 흐름을 계속 사용한다. 봇 관리는 로컬 Studio 또는 해당 호스트의 CLI에서 수행하고, 배정된 팀에는 Workspace Studio에서 의뢰한다. 독립 `agent-office run --repo …`는 지정된 저장소와 자체 엔진 설정으로 실행한다.

## Orca에서 채택한 관리 방식

아래 소스는 Orca 커밋 `47980a4`를 기준으로 확인했다. Orca에서는 템플릿이나 직접 작성한 봇을 대화·멘션으로 호출하거나 커넥터·일정으로 시작하고, 개별 워커의 worktree와 원래 대화로 돌아갈 수 있다. Office에서 이를 적용할 순서는 로컬 배정, 설치·갱신, 외부 트리거다. [Orca 봇 가이드](https://orca.dev/docs/bots)

| Orca 소스에서 확인한 동작 | Agent Office 설계 |
| --- | --- |
| 봇 홈은 사용자의 목적(`SOUL.md`)·기억(`MEMORY.md`)과 엔진용으로 생성하는 `AGENTS.md`·`CLAUDE.md`를 각각 관리한다. [봇 홈](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/main/bots/bot-home.ts#L154) | 1차에는 작성자가 업무 지침을 쓰고 Office가 적절한 엔진 호출에 주입한다. 개인 메모를 추가할 때는 별도 호스트 상태로 저장한다. |
| 엔진을 바꾸면 새 Home Chat을 열고 예전 대화는 기록에 남긴다. [엔진 전환](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/renderer/src/components/bots/bot-home-chat-confirm.tsx#L45) | 봇 ID는 담당자의 신원이고 공급자 세션은 해당 엔진의 실행 기록이다. 다른 엔진으로 새 작업을 시작할 때는 업무 지침과 필요한 증거를 전달한다. |
| 스킬 패키지 업로드 경로를 재사용하고 별도 봇 공유 링크로 버전을 가리킨다. 링크가 갱신돼도 기존 사본은 유지된다. [공유 클라이언트](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/main/bots/sharing/bot-share-client.ts#L142) · [사본의 업데이트](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/renderer/src/components/bots/sharing/BotShareFooter.tsx#L63) | 후속 공유 기능에서 카탈로그·공유 링크는 배포본을 찾는 용도로 쓰고, 설치 버전과 실행 중인 작업은 커밋·내용 해시로 고정한다. |
| 가져온 일정은 일시 중지 상태로 만들고, 적용 실패 시 부분 생성된 봇의 삭제를 시도한다. [패키지 적용](https://github.com/stablyai/orca/blob/47980a48d1977c26dd8f7a075be95349255b88ef/src/main/bots/bot-package-apply.ts#L47) | 향후 설치를 완료한 뒤 사용자가 이 PC의 연결과 일정을 선택한다. 중간 실패 시 이전 배정과 실행은 계속 사용할 수 있어야 한다. |

## 공개 Git 저장소에서 공유하기

1. 작성자는 봇 회의실에서 봇을 선택하고 **공유 패키지 내보내기**를 누른다. 내려받은 ZIP을 풀어서 공개 Git 저장소의 루트나 원하는 하위 폴더에 두고 커밋한다. 공유할 버전에는 Git 태그를 붙이거나 전체 커밋 SHA를 알려 준다.
2. 받는 사람은 대상 Workspace의 봇 회의실에서 **Git에서 봇 설치**를 누른다. 공개 HTTPS 저장소 URL과 Git 태그 또는 전체 커밋 SHA를 입력한다. 패키지가 하위 폴더에 있으면 저장소 기준 경로도 입력한다. 브랜치 이름 대신 커밋을 공유할 수 있다.
3. **패키지 확인**에서 봇 이름, 역할, 실제 커밋, 정의 해시와 파일 목록을 확인하고 **설치**를 누른다. 새 봇을 열면 **Git 설치 출처**에서 저장소와 커밋을 다시 볼 수 있다. 역할에 배정하려면 기존 Workspace 설정을 사용한다.

공유 패키지는 `MEMORY.md`를 제외한다. 설치한 사람은 빈 메모로 시작한다. 작성자가 공유할 참고 자료는 `references/` 같은 별도 경로에 담을 수 있다. 설치 시에는 로컬 저널에 정의를 저장하며, 각 실행에는 시작 시점의 봇 정의와 해시가 남는다. 이미 설치한 같은 버전을 다시 적용하면 기존 봇을 돌려준다. 같은 ID에 다른 정의나 설치 출처를 적용하면 충돌을 표시한다.

| 패키지 경로 | 작성 내용 |
| --- | --- |
| `plugin.json` | Agent Plugins 1.0.0 매니페스트. `extensions.io.github.tworimpa.agentoffice`에 `{"packageVersion":"1.0"}`을 선언한다. |
| `io.github.tworimpa.agentoffice/bot.json` | [Bot Definition 1.0](bot-spec-v1.md). `files.SOUL.md`에 봇 지침을, `files`의 다른 상대 경로에 참고 자료와 텍스트 스킬을 담는다. |
| `skills/<name>/SKILL.md` | 선택적 텍스트 스킬. 가져올 때 같은 상대 경로에 추가한다. 한 경로의 스킬은 한 곳에서 정의한다. |

실행 호스트에 Git 2.39 이상이 필요하다. 표준 HTTPS 포트의 공개 저장소에 직접 접속해 지정한 Git 커밋을 확인한다. 저장소가 리디렉션을 반환하면 대상 URL을 직접 입력한다. Studio의 기존 역할별 엔진과 실행 권한을 사용하며, 패키지 설치 자체는 CLI 작업을 시작하지 않는다. [예제 패키지](../examples/bot-packages/inventory-check) · [Agent Plugins 확장 규칙](https://agent-plugins.org/plugin-authors/client-extensions)

## 공유 봇 업데이트와 롤백

1. 설치한 봇의 **Git 설치 출처 → 업데이트 확인**에서 같은 저장소의 태그 또는 전체 커밋 SHA를 입력한다. 현재와 후보의 커밋·정의 해시, 추가·수정·삭제할 파일과 유지할 로컬 편집을 확인한다.
2. 변경을 적용하면 같은 봇과 Workspace 배정을 사용한다. 로컬 편집과 배포본이 같은 파일을 서로 다르게 바꿨으면 해당 경로를 표시하고 적용을 중단한다. 개인 메모는 적용하는 시점의 내용을 유지한다.
3. **직전 버전으로 롤백**에서 로컬에 저장된 업데이트 직전 상태를 확인하고 복원한다. 그 뒤 추가한 로컬 편집에도 같은 충돌 규칙을 적용한다. 복원에는 Git 연결이 필요 없다. 복원 기회는 업데이트마다 한 번 생긴다.

기존 설치본의 출처에 커밋과 해시만 있으면 현재 정의를 대조한다. 로컬 편집 때문에 원본을 확인할 수 없는 경우 설치한 커밋에서 검증된 원본을 다시 읽어 비교한다. 변경에 성공하면 새 의뢰가 활성 정의를 사용한다. 접수된 의뢰에는 당시 정의와 해시가 남는다. [업데이트 설계와 검증](superpowers/plans/2026-11-27-bot-package-updates.md)

| 단계 | 완료를 판단할 동작 |
| --- | --- |
| 1. 로컬 생성·배정·실행 | 같은 PC에서 세 봇을 만들고 Studio에 의뢰한다. 한 역할의 배정을 바꾼 뒤 기존 실행과 다음 실행의 정의가 구분되고, 개발·리뷰 호출에 해당 지침이 적용된다. 모의 엔진은 이 경로를 시험한다. 인증된 실제 CLI 수행 여부는 엔진과 OS별로 별도 기록한다. |
| 2. 작성·검증·설치 | Studio에서 공유 패키지를 만들고 다른 Workspace에서 URL과 버전으로 설치한다. 설치 커밋과 정의 해시를 확인한다. Windows와 WSL Ubuntu의 Codex에서 같은 패키지로 읽기 전용 의뢰를 검증한다. |
| 3. 공유·갱신·관리 | 설치자가 업데이트 내용을 보고 적용하거나 직전 버전을 선택한다. 개인 메모·권한·과거 실행은 보존한다. Studio에서 설치된 사본, Workspace 배정, 진행 중인 워커를 각각 조회·관리한다. Orca 가져오기, 카탈로그와 자동화 연결은 이 단계에서 검토한다. |
