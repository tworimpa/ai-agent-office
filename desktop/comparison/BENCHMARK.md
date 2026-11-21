# 설치형 데스크톱 성능 측정

같은 실행 호스트의 Electron 설치판과 Rust 설치판을 하나의 임시 Studio에 연결한다. 빈 저널의 첫 동작과 목표 10개가 있는 작업실의 브리핑을 각각 측정한다. 데이터가 있는 작업실에는 이전 목표 8개와 검토할 후보 1개를 미리 채운다. 열 번째 목표는 Studio 실행기와 모의 Codex를 실제로 시작해 질문 응답을 기다린다. 후보의 패치는 측정용 데이터다.

한 쌍의 두 앱이 같은 저널·후보·질문·작업 프로세스를 사용한다. 각 쌍의 첫 실행에는 연결 정보만 있는 새 프로필을 만들고, 다시 열 때에는 앱이 닫히면서 저장한 프로필을 사용한다. OS 파일 캐시는 유지한다. 매 회차 첫 앱을 바꾸고, 두 앱의 첫 실행을 마친 후 같은 순서로 다시 연다. 기본값은 OS마다 앱별 빈 작업실 10회와 목표 작업실 10회다.

## 실행

측정용 Python 3.11 이상에는 `psutil`을 설치한다. Linux에서는 `python-xlib`도 필요하다. 별도의 Studio용 Python에는 이 저장소의 개발 의존성을 설치한다. Git과 GUI가 있는 일반 사용자 세션에서 다음과 같이 실행한다. Linux의 X11 세션에서는 같은 디스플레이를 두 앱에 사용하고 가상 디스플레이를 쓰면 서버 종류·가속 상태를 결과에 기재한다.

```shell
python desktop/comparison/benchmark.py --electron ELECTRON_EXE --rust RUST_EXE --studio-python STUDIO_PYTHON --output NEW_RESULTS_DIR
python desktop/comparison/bench_summary.py NEW_RESULTS_DIR
```

`--repeats`와 `--idle`은 실행 점검에서만 줄인다. 기본 유휴 측정 시간은 9초다. 출력 폴더에는 실행 순서와 OS·실행 파일 SHA-256, 설치본의 화면에서 받은 결과, 시점별 PID와 메모리·CPU 카운터, 정상 종료 후 잔여 프로세스 및 실행별 표준 오류가 남는다. 출력 폴더에 빈 `STOP` 파일을 만들면 진행 중인 표본을 마친 후 수집을 중단하고 완료한 표본 파일을 보존한다. 요약의 `n`으로 각 조합의 완료 수를 확인한다.

## 관측 기준

- 시작 시간은 네이티브 실행 직전부터 Studio가 브라우저의 준비 신호를 수신할 때까지다. 창이 표시되면 두 앱 모두 같은 OS 창 크기 변경 요청을 받는다. 페이지가 두 프레임에서 1280×900 CSS 픽셀을 재확인해야 측정을 계속한다. 요청부터 크기 확인까지 걸린 시간은 `sizingMs`에 별도로 기록하며 전체 시작 시간에도 포함한다. 빈 작업실에서는 활성화된 등록 버튼을 확인한다. 목표 작업실에서는 브리핑에 목표가 10개 있고 올바른 질문과 후보 버튼을 모두 선택할 수 있어야 한다. 준비 조건을 만족한 후 두 번의 애니메이션 프레임을 거친다. 프레임과 로컬 HTTP 왕복의 일부가 들어가며, 실제 화면의 광학적 표시 시각은 측정하지 않는다.
- 빈 화면에서는 등록 동작부터 폴더 입력란 표시까지 잰다. 브리핑에서는 후보를 선택하고 변경 비교를 눌러 해당 패치 본문이 표시될 때까지, 질문을 선택해 원래 실행의 입력란에 포커스가 돌아올 때까지 각각 잰다. 다시 연 창은 앱이 저장한 주소로 바로 원래 실행을 불러와야 한다. 측정 코드는 임시 Studio가 제공하는 페이지에만 부착한다.
- 유휴 구간에는 제품의 기본 폴링을 유지한다. 시작·중간·종료 시점의 메모리를 읽고 세 값의 중앙값을 사용한다. CPU는 경과 시간 대비 사용자·커널 CPU 시간으로, 논리 코어 하나의 완전 점유가 100%다. PID가 바뀌면 CSV의 `CPUChurn`에 표시한다. 사라진 프로세스의 마지막 구간이 빠질 수 있다.
- Windows는 프로세스별 private commit, Linux는 `smaps_rollup`의 PSS를 사용한다. 각 데스크톱의 자손 프로세스를 추적하고 종료 후에도 이전에 발견한 자손을 확인한다. Studio, 작업 실행기, 모의 CLI, 테스트 프로세스는 별도 열이다. 질문 대기 중에 테스트는 이미 끝났다. `officeMiB`는 창과 Studio, `totalMiB`는 작업 실행기·CLI까지 포함한다. 프로세스 메모리에 접근할 수 없는 표본에서는 해당 합계를 비운다. 전역 디스플레이 서버·데스크톱 컴포지터·시스템 전체 파일 캐시는 포함하지 않는다.
- Windows에서는 해당 프로세스의 표시된 앱 창에 `WM_CLOSE`를 보내고 Linux에서는 해당 X11 창에 `WM_DELETE_WINDOW`를 보낸다. 정상 종료 코드를 확인한 뒤 닫기 요청의 3초 후 또는 종료 직후 중 더 늦은 시점에 잔여 메모리를 읽는다. 목표 작업실에서는 질문을 가진 CLI PID와 이를 감독하는 실행기가 계속 살아 있어야 한다.
- `trials.csv`는 개별 표본, `summary.json`은 OS별 중앙값·최솟값·최댓값과 같은 회차의 차이를 제공한다. 개선율은 회차별 `100 × (1 − Rust / Electron)`로 계산한다. 분모가 0인 회차는 해당 비율 계산에서 제외한다.

## 설치 용량

`bench_sizes.py`는 설치 파일의 바이트 수와 SHA-256, 설치된 일반 파일의 논리적 크기를 센다. Windows는 `--installed-dir`, Linux는 `--installed-deb`로 패키지에 소속된 파일을 지정한다. `--studio-runtime`에는 동일한 로컬 작업을 수행하는 데 필요한 Python 환경 디렉터리를 지정하고 런타임을 포함하는 설치판에는 `--includes-studio`를 붙인다.

```shell
python desktop/comparison/bench_sizes.py --package ELECTRON_INSTALLER --installed-dir ELECTRON_DIR --studio-runtime PYTHON_RUNTIME --includes-studio --output electron-size.json
python desktop/comparison/bench_sizes.py --package RUST_INSTALLER --installed-dir RUST_DIR --studio-runtime PYTHON_RUNTIME --shared-dir WEBVIEW2_VERSION_DIR --output rust-size.json
```

Linux의 WebKitGTK와 JavaScriptCore는 각각 `--shared-deb libwebkit2gtk-4.1-0 --shared-deb libjavascriptcoregtk-4.1-0`로 집계한다. 시스템에 이미 있는 렌더러를 전용 설치 크기에 더하면 다른 앱과 공유하는 용량을 중복 계산한다. 빈 시스템에 설치할 때는 배포판·기설치 패키지에 따라 추가 용량이 달라진다. Electron은 Python을 묶어 배포하므로 비교할 때 Rust의 셸 설치 용량과 동일 Studio를 포함한 용량을 함께 읽어야 한다.

설치 전 호스트에서 `/usr/bin/python3 bench_sizes.py --package APP.deb --estimate-apt-only --output apt-plan.json`을 실행하면 `apt-get -s` 기본 설정과 `--no-install-recommends` 각각의 신규·업그레이드 의존성, 예상 다운로드 크기 및 패키지 메타데이터의 추가 설치 용량을 얻는다. 이 추정치는 공유 라이브러리 실측치와 중복될 수 있다.

## 원자료 확인

`bench_export.py`로 동일한 호스트에서 수집한 두 설치판의 결과를 내보낼 수 있다.

```shell
python desktop/comparison/bench_export.py NEW_RESULTS_DIR NEW_PUBLIC_DIR --packages electron-size.json rust-size.json --display "Windows desktop"
```

공유본에는 실행별 PID·메모리·CPU 카운터와 시각이 든 `raw.jsonl.gz`, 표본별 `trials.csv`, 설치판·렌더러 의존성·실행 환경을 정리한 `environment.json`이 있다. 원본 stderr는 SHA-256과 진단별 행 수로 대조할 수 있다. 원자료를 압축 해제하면 한 줄당 한 번의 실행을 얻는다. 공유본을 복사한 디렉터리에 `bench_summary.py`를 실행해 CSV와 분포 요약을 다시 계산할 수 있다.
