# Rust 비교판

Windows와 Ubuntu x64에서 기존 Electron 앱과 **AI Agent Office (Rust 비교판)**을 함께 설치할 수 있다. 두 앱은 같은 Studio에 접속해 같은 작업실 화면을 연다. 작업 실행과 저널은 그 Studio가 소유한다.

## 두 앱에서 같은 작업 열기

1. Electron의 **작업실 → 연결 설정 및 도구 상태**에서 지금 연결한 Studio의 주소와 저널 ID를 확인한다. 예를 들어 로컬 서비스의 주소는 `http://127.0.0.1:8766`일 수 있다. 이전 개인 설치판이 자동으로 시작한 Studio의 주소는 사용자 설정 디렉터리의 `local-studio/runtime.json`에 있는 `port`로 확인한다.
2. Rust 비교판을 열어 같은 주소를 입력한다. 표시된 주소와 저널 ID를 확인한 뒤 연결한다. 작업공간에서 작업 기록이나 담당자 초상화를 열면 기존 실행을 이어 볼 수 있다.
3. 창을 닫아도 Studio가 작업을 계속한다. Rust 비교판을 다시 열면 저장한 대상에 재접속한다. 연결을 잃으면 설정 화면에 원인이 표시된다. 같은 포트에 다른 저널이 나타나면 대상 저널을 확인한 뒤 명시적으로 선택한다.

Rust 비교판에는 Python 실행기나 제공자 계정 설정이 필요하지 않다. 작업실 시작·종료와 원격 호스트 등록은 기존 Studio 관리 화면을 사용한다. 기존 Studio를 통해 SSH 호스트를 선택할 수 있다. 전용 폴더 선택기를 제공하지 않는 연결에서는 실행 호스트의 프로젝트 절대 경로를 입력한다.

## 구현 및 빌드

`rust-desktop/`의 Tauri 2 창은 설정 페이지에서만 네이티브 연결 명령을 사용한다. 선택한 루프백 Studio의 `/api/hosts`와 `/api/studio-identity`로 대상을 확인한다. Studio 화면에서 발생한 API 요청에는 기대하는 저널 ID를 붙인다. 이 버전의 Studio는 다른 ID가 지정된 요청을 처리하기 전에 HTTP 409를 돌려준다. 이전 Studio에서 연결 단계의 저널 확인은 가능하지만 요청마다 검사하는 기능은 서버 업데이트가 필요하다.

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

기본 산출물 경로는 `rust-desktop/target/release/bundle/`이다. CI에서는 Electron 설치 파일을 같은 실행의 Rust 패키지 검증에 전달한다. 두 앱을 설치한 뒤 연결·저널 교체를 검사하고, 비교판을 제거한 다음 Electron을 열어 같은 저널과 작업공간을 다시 확인한다.
