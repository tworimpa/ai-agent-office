# Rust 비교판

> **보관됨 (2026-11-27).** 비교 결과에 따라 두 운영체제 모두 Electron 설치판을 기본 앱으로 유지한다. Rust 비교판에는 새 개발을 하지 않으며 설치 대상으로 안내하지 않는다. 아래 내용은 비교 당시의 사용·검증 기록이다. 릴리스와 배포 정책이 요구하는 비교판 검사는 별도 결정 전까지 유지된다. [결정 기록 ADR-009](decisions.md)

Windows와 Ubuntu x64에서 기존 Electron 앱과 **AI Agent Office (Rust 비교판)**을 함께 설치할 수 있다. 두 앱은 같은 Studio에 접속해 같은 작업실 화면을 연다. 작업 실행과 저널은 그 Studio가 소유한다.

[두 OS의 성능 측정 및 설치 용량](../desktop/comparison/RESULTS.md)에서 설치판별 중앙값과 전체 표본을 확인할 수 있다.

## 두 앱에서 같은 작업 열기

Windows에서는 비교판의 `x64-setup.exe`를 실행한 뒤 시작 메뉴에서 **AI Agent Office (Rust 비교판)**을 연다. Ubuntu에서는 다운로드한 디렉터리에서 `sudo apt install ./ai-agent-office-rust_0.7.0_amd64.deb`를 실행한 뒤 앱 메뉴 또는 `ai-agent-office-rust`로 연다. Linux의 시스템 WebKitGTK 의존성은 apt가 함께 설치한다.

1. Electron의 **작업실 → 연결 설정 및 도구 상태**에서 지금 연결한 Studio의 주소와 저널 ID를 확인한다. 예를 들어 로컬 서비스의 주소는 `http://127.0.0.1:8766`일 수 있다. 이전 개인 설치판이 자동으로 시작한 Studio의 주소는 사용자 설정 디렉터리의 `local-studio/runtime.json`에 있는 `port`로 확인한다.
2. Rust 비교판을 열어 같은 주소를 입력한다. 표시된 주소와 저널 ID를 확인한 뒤 연결한다. 작업공간에서 작업 기록이나 담당자 초상화를 열면 기존 실행을 이어 볼 수 있다.
3. 창을 닫아도 Studio가 작업을 계속한다. Rust 비교판을 다시 열면 저장한 대상에 재접속한다. 연결을 잃으면 설정 화면에 원인이 표시된다. 같은 포트에 다른 저널이 나타나면 대상 저널을 확인한 뒤 명시적으로 선택한다.

Rust 비교판에는 Python 실행기나 제공자 계정 설정이 필요하지 않다. 작업실 시작·종료와 원격 호스트 등록은 기존 Studio 관리 화면을 사용한다. 기존 Studio를 통해 SSH 호스트를 선택할 수 있다. 전용 폴더 선택기를 제공하지 않는 연결에서는 실행 호스트의 프로젝트 절대 경로를 입력한다.

## 구현 및 빌드

`rust-desktop/`의 Tauri 2 창은 설정 페이지에서만 네이티브 연결 명령을 사용한다. 선택한 루프백 Studio의 `/api/hosts`와 `/api/studio-identity`로 대상을 확인한다. 이 버전의 Studio는 `journal_guard`를 알린다. 비교판은 API 주소와 헤더에 확인한 저널 ID를 붙이고, 서버는 다른 ID가 지정된 요청에 HTTP 409를 돌려준다. 포트를 구버전 Studio가 차지해도 구버전 서버가 이 API 주소를 거절한다.

저널 ID 확인만 지원하는 이전 Studio는 **읽기 전용**으로 표시한다. 사용자가 확인하면 작업 이력을 볼 수 있다. Rust판에서 의뢰·추가 지시·답변까지 하려면 **Electron 설치판 0.7.1에 포함된 Studio** 또는 같은 저널 확인 기능이 있는 독립 Studio를 실행해 두 앱을 그 주소에 연결한다. 비교판 설치가 실행 중인 서비스나 기존 Studio를 업데이트하지 않는다.

구버전의 읽기 전용 모드에서는 서버가 저널 ID를 조회마다 검증하지 않는다. 포트가 다른 저널로 교체되면 다음 연결 점검에서 설정 화면으로 돌아간다. 재접속할 때 읽기 전용 연결을 다시 확인한다.

비교판의 앱 식별자는 `dev.aiagentoffice.rust`, 실행 파일과 Debian 패키지명은 `ai-agent-office-rust`다. 설정은 별도의 사용자 디렉터리에 저장된다. `--profile-dir <절대 경로>`로 검증용 프로필을 지정할 수 있다. Windows에서는 시스템 WebView2, Linux에서는 WebKitGTK 4.1을 사용한다.

해당 OS의 [Tauri 빌드 전제 조건](https://v2.tauri.app/start/prerequisites/)과 Rust stable을 준비하고 `rust-desktop/`에서 실행한다.

```sh
cargo test --release --locked
node --test pin-journal.test.mjs
# Windows
npx --yes --package=@tauri-apps/cli@2.12.6 tauri build --bundles nsis -- --locked
# Ubuntu
npx --yes --package=@tauri-apps/cli@2.12.6 tauri build --bundles deb -- --locked
```

기본 산출물 경로는 `rust-desktop/target/release/bundle/`이다. CI에서는 Electron 설치 파일을 같은 실행의 Rust 패키지 검증에 전달한다. 두 앱에서 연결을 저장하고, 같은 포트의 저널 교체를 시험한다. 이후 한쪽 앱을 제거한 다음 다른 앱의 저장된 프로필이 원래 저널·작업공간을 복원하는지 양방향으로 확인한다. 측정 환경과 재현 명령은 [비교 벤치마크 안내](../desktop/comparison/BENCHMARK.md)를 참조한다.

## 실제 작업 인계

Windows WebView2와 Ubuntu 26.04의 WebKitGTK에서 각각 Electron → Rust와 Rust → Electron을 실행했다. 설정 복원 회귀를 재현하고 한 앱에서 작업 중인 창을 닫았다. 다른 앱에서 담당자에게 추가 지시를 보내고 원래 질문에 답했다. 네 실행 모두 지시와 질문이 하나씩 남았고 전체 pytest 2개가 통과했다. 두 창은 같은 검토 후보와 저널 이벤트를 열었다. Git·설치판·pytest는 실제로 실행했고 개발·리뷰에는 모의 CLI를 사용했다.

1366×768과 390×844에서 질문과 후보를 열고 포커스·패치·Markdown·Mermaid를 확인했다. Windows 한글 조합 검증은 Chromium 입력 엔진을 사용했고, Ubuntu에서는 전용 X11 세션에서 IBus 두벌식으로 `밝게`를 입력해 저장된 답변을 확인했다. [실행별 검증 기록](../desktop/comparison/results/handoff/verified.json) · [Windows 일반 창](../desktop/comparison/results/handoff/windows-webview2.png) · [Ubuntu 좁은 창](../desktop/comparison/results/handoff/ubuntu-webkitgtk.png)

Ubuntu의 WebKitGTK 2.52.6에서는 창을 정상적으로 닫을 때 앱 본체가 종료 코드 0을 반환해도 렌더러 자식이 간헐적으로 SIGABRT로 끝났다. 별도 재현에서는 Xvfb 2회 중 2회, WSLg XWayland 2회 중 1회였고, 최소 GTK/WebKit 앱에서도 2회 중 1회였다. 이 환경에서 물리 Linux 데스크톱의 발생률을 추정할 수 없다. 작업을 소유한 Studio와 저널은 창을 닫은 뒤에도 사용 가능했다.
