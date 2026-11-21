use reqwest::{blocking::Client, header::CONTENT_TYPE, redirect::Policy, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    fs::File,
    io::{ErrorKind, Read, Write},
    path::Path,
    time::Duration,
};
use tempfile::NamedTempFile;
use url::Url;

const MAX_JSON: u64 = 256 * 1024;
const INVALID_ADDRESS: &str = "이 컴퓨터의 Studio 주소를 입력하세요. 예: http://127.0.0.1:8766";
const OFFLINE: &str = "Studio에 연결할 수 없습니다. 주소와 작업실이 실행 중인지 확인하세요.";
const INVALID_STUDIO: &str = "이 주소에서 AI Agent Office 작업실과 저널 ID를 확인할 수 없습니다.";
pub const MISMATCH: &str = "저장한 저널과 다른 작업실입니다. 주소와 저널 ID를 다시 확인하세요.";

pub fn origin(address: &str) -> Result<String, String> {
    let mut url = Url::parse(address).map_err(|_| INVALID_ADDRESS)?;
    if address.len() > 192
        || url.scheme() != "http"
        || !matches!(url.host_str(), Some("localhost" | "127.0.0.1"))
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
        || url.port() == Some(0)
    {
        return Err(INVALID_ADDRESS.into());
    }
    url.set_host(Some("127.0.0.1"))
        .map_err(|_| INVALID_ADDRESS)?;
    Ok(url.origin().ascii_serialization())
}

fn valid_journal(id: &str) -> bool {
    id.len() == 32
        && id
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub fn page(url: &Url, origin: &str) -> Option<String> {
    if url.origin().ascii_serialization() != origin
        || !url.username().is_empty()
        || url.password().is_some()
        || url.as_str().len() > 4096
        || !matches!(
            url.path(),
            "/" | "/studio" | "/office" | "/versions" | "/improvements"
        )
    {
        return None;
    }
    Some(url[url::Position::BeforePath..].to_owned())
}

pub fn client() -> Result<Client, reqwest::Error> {
    Client::builder()
        .timeout(Duration::from_millis(2500))
        .no_proxy()
        .redirect(Policy::none())
        .build()
}

fn api(client: &Client, origin: &str, path: &str, optional: bool) -> Result<Option<Value>, String> {
    let response = client
        .get(format!("{origin}{path}"))
        .send()
        .map_err(|_| OFFLINE)?;
    if optional && response.status() == StatusCode::NOT_FOUND {
        return Ok(None);
    }
    if !response.status().is_success()
        || response
            .headers()
            .get(CONTENT_TYPE)
            .and_then(|h| h.to_str().ok())
            .and_then(|h| h.split(';').next())
            .is_none_or(|mime| !mime.trim().eq_ignore_ascii_case("application/json"))
    {
        return Err(INVALID_STUDIO.into());
    }
    let mut data = Vec::new();
    response
        .take(MAX_JSON + 1)
        .read_to_end(&mut data)
        .map_err(|_| OFFLINE)?;
    if data.len() as u64 > MAX_JSON {
        return Err(INVALID_STUDIO.into());
    }
    serde_json::from_slice(&data)
        .map(Some)
        .map_err(|_| INVALID_STUDIO.into())
}

pub fn probe(client: &Client, address: &str, expected: Option<&str>) -> Result<Connection, String> {
    let origin = origin(address)?;
    let hosts = api(client, &origin, "/api/hosts", false)?.ok_or(INVALID_STUDIO)?;
    if !hosts["hosts"].as_array().is_some_and(|rows| {
        rows.iter()
            .any(|row| row["id"] == "local" && row["name"].is_string())
    }) {
        return Err(INVALID_STUDIO.into());
    }
    let identity = match api(client, &origin, "/api/studio-identity", true)? {
        Some(value) => value,
        None => api(client, &origin, "/api/runtime-health", true)?.ok_or(INVALID_STUDIO)?,
    };
    let journal = identity["journal_id"]
        .as_str()
        .filter(|s| valid_journal(s))
        .ok_or(INVALID_STUDIO)?;
    if expected.is_some_and(|id| journal != id) {
        return Err(MISMATCH.into());
    }
    Ok(Connection {
        origin,
        journal_id: journal.to_owned(),
        page: "/".into(),
    })
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Connection {
    pub origin: String,
    pub journal_id: String,
    pub page: String,
}

impl Connection {
    pub fn url(&self) -> Result<Url, String> {
        let url =
            Url::parse(&format!("{}{}", self.origin, self.page)).map_err(|_| INVALID_STUDIO)?;
        if origin(&self.origin)? != self.origin
            || !valid_journal(&self.journal_id)
            || page(&url, &self.origin).as_ref() != Some(&self.page)
        {
            return Err(INVALID_STUDIO.into());
        }
        Ok(url)
    }
}

pub fn read(directory: &Path) -> Result<Option<Connection>, String> {
    let path = directory.join("connection.json");
    let error = || {
        format!(
            "저장된 연결 설정을 읽을 수 없습니다. {} 파일을 확인하세요.",
            path.display()
        )
    };
    let file = match File::open(&path) {
        Ok(file) => file,
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(error()),
    };
    if file.metadata().map_err(|_| error())?.len() > 8192 {
        return Err(error());
    }
    let saved: Connection = serde_json::from_reader(file.take(8193)).map_err(|_| error())?;
    saved.url().map_err(|_| error())?;
    Ok(Some(saved))
}

pub fn save(directory: &Path, connection: &Connection) -> Result<(), String> {
    connection.url()?;
    let path = directory.join("connection.json");
    let error = || {
        format!(
            "연결 설정을 저장할 수 없습니다. {} 파일을 확인하세요.",
            path.display()
        )
    };
    let mut file = NamedTempFile::new_in(directory).map_err(|_| error())?;
    serde_json::to_writer(&mut file, connection).map_err(|_| error())?;
    file.flush()
        .and_then(|_| file.as_file().sync_all())
        .map_err(|_| error())?;
    file.persist(&path).map_err(|_| error())?;
    Ok(())
}
