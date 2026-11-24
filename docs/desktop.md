# AI Agent Office 설치형 앱

Windows x64 사용자별 설치판과 Ubuntu·Debian 계열 Linux x64 설치판을 제공한다. 설치판에는 사무실 창과 로컬 Studio 실행용 Python 환경이 포함된다. 작업을 실행할 호스트에는 Git과 선택한 에이전트 CLI를 준비한다.

## 설치와 첫 실행

[GitHub Releases](https://github.com/tworimpa/ai-agent-office/releases)의 Assets에서 운영체제에 맞는 설치 파일과 `SHA256SUMS.txt`를 받는다. [다운로드 검증 및 배포 절차](desktop-release.md)

- **Windows**에서는 `AI-Agent-Office-Setup-<버전>.exe`를 실행하고 시작 메뉴에서 **AI Agent Office**를 연다.
- **Linux**에서는 데스크톱 패키지 설치 도구로 `ai-agent-office-desktop_<버전>_amd64.deb`를 열거나, 패키지가 있는 폴더에서 `sudo apt install ./ai-agent-office-desktop_<버전>_amd64.deb`를 실행한다. 앱 목록에서 **AI Agent Office**를 연다. WSL에서는 WSLg가 실행 중인 Ubuntu에서 같은 앱을 열 수 있다.

**기존 작업실 연결**은 실행 중인 이 컴퓨터의 Studio 주소를 사용한다. 예를 들어 Windows의 `AgentOfficeStudio`가 포트 8766으로 동작 중이면 `http://127.0.0.1:8766`을 입력한다. 기존 저널과 작업공간이 연결된다.

구버전 Studio가 저널 ID를 제공하지 않는 경우 앱을 다시 열 때 주소를 확인하고 수동으로 연결한다.

**내 작업실 시작**을 누르면 앱이 이 컴퓨터의 로컬 작업실을 열어 준다. 첫 화면에서 프로젝트 폴더를 선택한 뒤 제안된 이름으로 작업공간을 등록할 수 있다. 폴더를 추가하거나 이름을 바꿔도 된다. 첫 작업은 조정자에게 맡기고, 접수 화면에서 **담당자와 대화**를 선택해 같은 실행을 따라간다.

기존 서비스나 SSH로 연결한 원격 작업실에서는 화면에 표시된 호스트의 프로젝트 경로를 입력한다. **기존 Studio 연결 및 SSH 설정**에서 SSH 별칭, Studio 포트, 원격 저널 ID를 한 줄씩 등록할 수 있다. 저장된 작업실에 다시 연결할 수 없을 때는 연결 화면에서 대상을 확인하고 **저장된 연결 다시 시도**를 누른다. [원격 작업실 안내](../README.md#원격-호스트에서-작업하기)

연결 화면에서 **도구 상태**를 확인할 수 있다. 설치가 필요한 Git·CLI나 로그인할 에이전트의 **공식 안내**를 열 수 있다. 제공자가 고정된 진단으로 인증 상태를 알려 주지 않으면 앱은 확인이 필요하다고 표시한다. Windows의 새 로컬 Studio는 Codex 프로세스에 MXC를 지정한다. 연결한 기존 Studio에는 해당 서비스의 실행·승인 설정이 적용된다.

## 작업 계속하기와 업데이트

앱 창을 닫거나 앱을 종료하면 로컬 Studio와 작업 프로세스는 계속 실행된다. 앱을 열면 마지막 작업 화면으로 돌아간다. 개별 실행은 사무실의 **실행 중지**로 중지한다. 데스크톱 앱이 시작한 실행기 자체를 종료하려면 운영체제 창의 **작업실 → 로컬 작업실 종료**를 선택한다. 이 동작은 해당 로컬 Studio에 연결된 작업에 영향을 준다. 재부팅이나 장애로 중단된 실행은 Studio의 기존 복구 규칙에 따라 표시된다.

새 설치판을 적용하기 전에 로컬 작업실에서 작업이 끝났는지 확인하고, 위 메뉴에서 로컬 작업실과 앱을 종료한다. Windows에서는 새 설치 파일을 실행한다. Linux에서는 새 `.deb`를 같은 방식으로 설치한다. 제거할 때 Windows의 **설치된 앱** 또는 Linux의 패키지 관리자에서 앱을 제거한다. 기본 제거와 수동 업그레이드는 사용자 데이터 경로와 에이전트 CLI의 기존 설정을 보존한다.

| 항목 | Windows | Linux |
|---|---|---|
| 데스크톱 연결 설정과 로컬 저널 | `%APPDATA%\AI Agent Office` | `${XDG_CONFIG_HOME:-~/.config}/AI Agent Office` |
| 새 로컬 Studio 시작 기록 | 위 경로의 `local-studio/startup.log` | 위 경로의 `local-studio/startup.log` |

## 빌드와 검증

각 OS에서 Node.js 26과 uv를 준비한 뒤 저장소 루트에서 실행한다.

```shell
npm ci --prefix desktop
npm test --prefix desktop
npm run package --prefix desktop
```

설치 파일과 `SHA256SUMS.txt`는 `desktop/dist`에 생성된다. 설치된 실행 파일을 대상으로 모의 Codex로 연결·수정·추가 지시·창 재연결·테스트·리뷰의 흐름을 검증할 수 있다.

CI에서는 각 운영체제의 설치 검사가 통과하면 파일명, 버전, 설치 파일 SHA-256, Actions 아티팩트 ID와 digest를 영수증에 기록한다. 같은 커밋을 다시 빌드해도 설치 파일의 바이트와 아티팩트 ID가 달라질 수 있다. 배포할 때는 해당 커밋의 `main` 빌드에서 검사를 통과한 작업의 영수증으로 파일을 선택한다.

```shell
node desktop/verify-install.mjs PATH_TO_INSTALLED_EXECUTABLE
```

검증은 임시 저널과 예제 Git 저장소를 사용한다. 설치된 실제 Codex로 같은 흐름을 검증하려면 해당 OS의 Codex 인증을 준비한 뒤 운영 범위가 승인된 환경에서 `--live`를 추가한다. Linux CI에서는 `xvfb-run`으로 GUI가 있는 환경을 제공한다.
