#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod studio;

#[cfg(test)]
mod studio_tests;

use reqwest::blocking::Client;
use serde::Serialize;
use std::{
    fs::{self, File},
    path::PathBuf,
    sync::{Arc, Mutex, RwLock},
    thread,
    time::Duration,
};
use studio::Connection;
use tauri::{
    menu::{Menu, SubmenuBuilder},
    webview::{NewWindowResponse, PermissionResponse},
    AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent,
};
use url::Url;

const TITLE: &str = "AI Agent Office (Rust 비교판)";

#[derive(Default, Clone, PartialEq, Serialize)]
struct Screen {
    saved: Option<Connection>,
    candidate: Option<Connection>,
    message: String,
    unreadable: bool,
}

struct Session {
    directory: PathBuf,
    _profile_lock: File,
    client: Client,
    screen: Screen,
    connected: bool,
    paused: bool,
    // Navigation callbacks run on the native message loop and must not wait
    // for a Session lock held by a thread dispatching a WebView call.
    allowed_origin: Arc<RwLock<Option<String>>>,
}

fn local_setup(url: &Url) -> bool {
    let local = match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" => url.host_str() == Some("tauri.localhost"),
        _ => false,
    };
    local
        && matches!(url.path(), "/" | "/index.html")
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
}

impl Session {
    fn load(&mut self) {
        self.paused = false;
        match studio::read(&self.directory) {
            Ok(saved) => {
                self.screen = Screen {
                    saved,
                    ..Default::default()
                };
            }
            Err(message) => {
                self.screen = Screen {
                    unreadable: true,
                    message,
                    ..Default::default()
                };
            }
        }
    }

    fn select(&mut self, address: &str) -> Result<(), String> {
        self.paused = true;
        self.screen.candidate = None;
        if self.screen.unreadable {
            return Err(self.screen.message.clone());
        }
        let candidate = studio::probe(&self.client, address, None)?;
        let replacing = self
            .screen
            .saved
            .as_ref()
            .is_some_and(|s| s.journal_id != candidate.journal_id);
        self.screen.message = if replacing {
            "저장된 작업실과 저널이 다릅니다. 아래 저널로 등록을 변경할지 확인하세요."
        } else {
            "주소와 저널 ID를 확인한 뒤 연결하세요."
        }
        .into();
        self.screen.candidate = Some(candidate);
        Ok(())
    }

    fn confirm(&mut self, app: &AppHandle) -> Result<(), String> {
        let mut candidate = self
            .screen
            .candidate
            .take()
            .ok_or("먼저 주소를 확인하세요.")?;
        studio::probe(&self.client, &candidate.origin, Some(&candidate.journal_id))?;
        if let Some(saved) = &self.screen.saved {
            if saved.journal_id == candidate.journal_id {
                candidate.page.clone_from(&saved.page);
            }
        }
        studio::save(&self.directory, &candidate)?;
        self.screen.saved = Some(candidate.clone());
        self.open(app, &candidate)
    }

    fn open(&mut self, app: &AppHandle, connection: &Connection) -> Result<(), String> {
        let window = app
            .get_webview_window("main")
            .ok_or("작업실 창을 찾을 수 없습니다.")?;
        let mut url = connection.url()?;
        let fragment = url::form_urlencoded::Serializer::new(String::new())
            .append_pair("office-desktop-journal", &connection.journal_id)
            .append_pair("anchor", url.fragment().unwrap_or(""))
            .finish();
        url.set_fragment(Some(&fragment));
        *self.allowed_origin.write().unwrap() = Some(connection.origin.clone());
        if let Err(error) = window.navigate(url) {
            *self.allowed_origin.write().unwrap() = None;
            return Err(format!("Studio 화면을 열 수 없습니다. {error}"));
        }
        self.connected = true;
        self.paused = false;
        self.screen.message.clear();
        Ok(())
    }

    fn remember(&mut self, app: &AppHandle) -> Result<(), String> {
        let Some(saved) = &self.screen.saved else {
            return Ok(());
        };
        let Some(window) = app.get_webview_window("main") else {
            return Ok(());
        };
        let Some(page) = window
            .url()
            .ok()
            .filter(|url| {
                !url.fragment()
                    .unwrap_or("")
                    .starts_with("office-desktop-journal=")
            })
            .and_then(|url| studio::page(&url, &saved.origin))
        else {
            return Ok(());
        };
        if page != saved.page {
            let mut next = saved.clone();
            next.page = page;
            studio::save(&self.directory, &next)?;
            self.screen.saved = Some(next);
        }
        Ok(())
    }

    fn disconnect(&mut self, app: &AppHandle) {
        self.connected = false;
        *self.allowed_origin.write().unwrap() = None;
        if let Some(window) = app.get_webview_window("main") {
            let setup_url = if cfg!(target_os = "windows") {
                "http://tauri.localhost/index.html"
            } else {
                "tauri://localhost/index.html"
            };
            if window.navigate(Url::parse(setup_url).unwrap()).is_err() {
                // Never leave an interactive Studio on screen after failing identity verification.
                app.exit(1);
            }
        }
    }

    fn refresh(&mut self, app: &AppHandle) -> Result<(), String> {
        if self.screen.unreadable || self.paused {
            return Ok(());
        }
        let Some(saved) = self.screen.saved.clone() else {
            return Ok(());
        };
        if let Err(error) = studio::probe(&self.client, &saved.origin, Some(&saved.journal_id)) {
            if self.connected {
                self.disconnect(app);
            }
            return Err(format!("저장된 작업실 {}\n{error}", saved.origin));
        }
        if self.connected {
            self.remember(app)
        } else {
            self.open(app, &saved)
        }
    }
}

// Window creation, URL reads and probes run off the native message loop. One
// lock serializes confirmation, reconnect and close against the same profile.
async fn setup_action<F>(app: AppHandle, window: WebviewWindow, action: F) -> Result<Screen, String>
where
    F: FnOnce(&mut Session, &AppHandle) -> Result<(), String> + Send + 'static,
{
    if window.label() != "main" || !window.url().ok().is_some_and(|url| local_setup(&url)) {
        return Err("연결 설정 화면에서 실행하세요.".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<Mutex<Session>>();
        let mut session = state.lock().map_err(|_| "연결 상태를 읽지 못했습니다.")?;
        if session.connected {
            return Err("연결 설정 화면에서 실행하세요.".into());
        }
        if let Err(error) = action(&mut session, &app) {
            session.screen.message = error;
        }
        Ok(session.screen.clone())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn snapshot(app: AppHandle, window: WebviewWindow) -> Result<Screen, String> {
    setup_action(app, window, |_, _| Ok(())).await
}
#[tauri::command]
async fn select(app: AppHandle, window: WebviewWindow, address: String) -> Result<Screen, String> {
    setup_action(app, window, move |session, _| session.select(&address)).await
}
#[tauri::command]
async fn confirm(app: AppHandle, window: WebviewWindow) -> Result<Screen, String> {
    setup_action(app, window, Session::confirm).await
}
#[tauri::command]
async fn retry(app: AppHandle, window: WebviewWindow) -> Result<Screen, String> {
    setup_action(app, window, |session, app| {
        session.load();
        session.refresh(app)
    })
    .await
}

fn monitor(app: AppHandle) {
    let state = app.state::<Mutex<Session>>();
    loop {
        {
            let Ok(mut session) = state.lock() else {
                return;
            };
            let previous = session.screen.clone();
            if let Err(error) = session.refresh(&app) {
                session.screen.message = error;
            }
            if !session.connected && session.screen != previous {
                if let Some(setup) = app.get_webview_window("main") {
                    let _ = setup.eval("window.dispatchEvent(new Event('office-state'))");
                }
            }
        }
        thread::sleep(Duration::from_secs(2));
    }
}

fn profile_directory(app: &tauri::App) -> Result<PathBuf, Box<dyn std::error::Error>> {
    let mut args = std::env::args_os().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--profile-dir" {
            let path = args
                .next()
                .map(PathBuf::from)
                .filter(|p| p.is_absolute())
                .ok_or("--profile-dir에는 절대 경로가 필요합니다.")?;
            return Ok(path);
        }
    }
    Ok(app.path().app_config_dir()?)
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![snapshot, select, confirm, retry])
        .setup(|app| {
            let directory = profile_directory(app)?;
            fs::create_dir_all(&directory)?;
            let profile_lock = File::options()
                .create(true)
                .truncate(false)
                .read(true)
                .write(true)
                .open(directory.join("profile.lock"))?;
            profile_lock.try_lock()?;
            let allowed_origin = Arc::new(RwLock::new(None));
            let mut session = Session {
                directory: directory.clone(),
                _profile_lock: profile_lock,
                client: studio::client()?,
                connected: false,
                paused: false,
                allowed_origin: allowed_origin.clone(),
                screen: Screen::default(),
            };
            session.load();
            app.manage(Mutex::new(session));
            let office_menu = SubmenuBuilder::new(app, "작업실")
                .text("connection-settings", "연결 설정")
                .build()?;
            let menu = Menu::with_items(app, &[&office_menu])?;
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title(TITLE)
                .menu(menu)
                .inner_size(1320., 940.)
                .min_inner_size(350., 500.)
                .data_directory(directory.join("webview"))
                .on_navigation(move |url| match &*allowed_origin.read().unwrap() {
                    Some(origin) => studio::page(url, origin).is_some(),
                    None => local_setup(url),
                })
                .initialization_script(include_str!("pin-journal.js"))
                .on_download(|_, _| false)
                .on_new_window(|_, _| NewWindowResponse::Deny)
                .on_permission_request(|_, _| PermissionResponse::Deny)
                .build()?;
            let handle = app.handle().clone();
            thread::spawn(move || monitor(handle));
            Ok(())
        })
        .on_menu_event(|app, event| {
            if event.id().as_ref() == "connection-settings" {
                let app = app.clone();
                thread::spawn(move || {
                    let state = app.state::<Mutex<Session>>();
                    let mut session = state.lock().unwrap();
                    if session.connected {
                        let result = session.remember(&app);
                        session.paused = true;
                        session.disconnect(&app);
                        session.screen.message = result.err().unwrap_or_default();
                    }
                });
            }
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let app = window.app_handle().clone();
                thread::spawn(move || {
                    if let Ok(mut session) = app.state::<Mutex<Session>>().lock() {
                        if session.connected {
                            if let Err(error) = session.remember(&app) {
                                session.disconnect(&app);
                                session.screen.message = error;
                                if let Some(setup) = app.get_webview_window("main") {
                                    let _ = setup
                                        .eval("window.dispatchEvent(new Event('office-state'))");
                                }
                                return;
                            }
                        }
                    }
                    app.exit(0);
                });
            }
        })
        .run(tauri::generate_context!())
        .expect("AI Agent Office (Rust 비교판) 시작 실패");
}
