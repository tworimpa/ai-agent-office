use super::{local_setup, studio::*};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    net::TcpListener,
    thread,
};
use url::Url;

const ID: &str = "0123456789abcdef0123456789abcdef";
const HOSTS: &str = r#"{"hosts":[{"id":"local","name":"이 컴퓨터"}]}"#;
const IDENTITY: &str = r#"{"journal_id":"0123456789abcdef0123456789abcdef"}"#;

fn listen(
    replies: Vec<(&'static str, u16, &'static str, String)>,
) -> (String, thread::JoinHandle<()>) {
    let server = TcpListener::bind("127.0.0.1:0").unwrap();
    let origin = format!("http://{}", server.local_addr().unwrap());
    let handle = thread::spawn(move || {
        for (path, status, mime, body) in replies {
            let (mut socket, _) = server.accept().unwrap();
            let mut request = BufReader::new(&socket);
            let mut line = String::new();
            request.read_line(&mut line).unwrap();
            assert_eq!(line.split_whitespace().nth(1), Some(path));
            loop {
                line.clear();
                if request.read_line(&mut line).unwrap() == 0 || line == "\r\n" {
                    break;
                }
            }
            let _ = write!(socket, "HTTP/1.1 {status} Response\r\nContent-Type: {mime}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
        }
    });
    (origin, handle)
}

#[test]
fn only_loopback_http_and_studio_pages() {
    let base = origin("http://localhost:8766/").unwrap();
    assert_eq!(base, "http://127.0.0.1:8766");
    for input in [
        "https://127.0.0.1",
        "http://localhost.evil",
        "http://127.0.0.1@evil",
        "http://user@localhost",
        "http://localhost/?x=1",
        "http://localhost/#section",
        "http://localhost/studio",
        "file:///tmp",
        "http://[::1]",
        "http://127.0.0.1:0",
    ] {
        assert!(origin(input).is_err(), "{input}");
    }
    let location = "/?host=local&workspace=abc&run=def#goal";
    assert_eq!(
        page(&Url::parse(&(base.clone() + location)).unwrap(), &base).as_deref(),
        Some(location)
    );
    for input in [
        "http://127.0.0.1:8766/api/hosts",
        "http://127.0.0.1:8767/",
        "http://u@127.0.0.1:8766/",
        "tauri://localhost/index.html",
    ] {
        assert!(page(&Url::parse(input).unwrap(), &base).is_none());
    }
    // Tauri omits index.html and the trailing slash for the initial Linux URL.
    assert!(local_setup(&Url::parse("tauri://localhost").unwrap()));
    assert!(local_setup(
        &Url::parse("tauri://localhost/index.html").unwrap()
    ));
    assert!(local_setup(&Url::parse("http://tauri.localhost/").unwrap()));
    for input in [
        "http://127.0.0.1:8766/",
        "http://tauri.localhost:8766/",
        "http://tauri.localhost/setup.js",
    ] {
        assert!(!local_setup(&Url::parse(input).unwrap()));
    }
}

#[test]
fn only_exact_catalog_aliases_from_the_attached_studio_open_externally() {
    let origin = "http://127.0.0.1:8766";
    let path = format!("/bot-catalog/links/example-1/{ID}");
    let alias = format!("{origin}{path}");
    let url = Url::parse(&alias).unwrap();
    assert_eq!(catalog_link(&url, origin), Some(alias.as_str()));
    assert_eq!(page(&url, origin), None);
    let longest = alias.replace("example-1", &"a".repeat(64));
    assert_eq!(
        catalog_link(&Url::parse(&longest).unwrap(), origin),
        Some(longest.as_str())
    );
    for invalid in [
        format!("https://127.0.0.1:8766{path}"),
        format!("http://127.0.0.1:8767{path}"),
        format!("http://localhost:8766{path}"),
        format!("http://example.org{path}"),
        format!("http://user:pw@127.0.0.1:8766{path}"),
        format!("{origin}/api/hosts"),
        format!("{origin}/prefix{path}"),
        format!("{alias}?"),
        format!("{alias}?workspace=local"),
        format!("{alias}#"),
        format!("{alias}#run"),
        format!("{alias}/"),
        alias[..alias.len() - 1].into(),
        format!("{alias}f"),
        alias.replace("example-1", "Example-1"),
        alias.replace("example-1", "%65xample-1"),
        alias.replace("example-1", "example--1"),
        alias.replace("example-1", "example-"),
        alias.replace("example-1", &"a".repeat(65)),
        alias.replace("abcdef", "ABCDEF"),
    ] {
        assert_eq!(
            catalog_link(&Url::parse(&invalid).unwrap(), origin),
            None,
            "{invalid}"
        );
    }
}

#[test]
fn identity_probe_requires_both_local_host_and_matching_id() {
    for (expected, ok) in [
        (None, true),
        (Some(ID), true),
        (Some("11111111111111111111111111111111"), false),
    ] {
        let (address, server) = listen(vec![
            ("/api/hosts", 200, "application/json", HOSTS.into()),
            (
                "/api/studio-identity",
                200,
                "application/json",
                IDENTITY.into(),
            ),
        ]);
        let result = probe(&client().unwrap(), &address, expected);
        assert_eq!(result.is_ok(), ok);
        if ok {
            let result = result.unwrap();
            assert_eq!(result.connection.journal_id, ID);
            assert_eq!(result.access, Access::ReadOnly);
        } else {
            assert_eq!(result.unwrap_err(), MISMATCH);
        }
        server.join().unwrap();
    }
}

#[test]
fn only_explicit_guard_support_enables_writes_and_capability_changes_need_confirmation() {
    for (flag, expected) in [
        ("true", Access::JournalBound),
        ("false", Access::ReadOnly),
        ("\"true\"", Access::ReadOnly),
    ] {
        let identity = format!(r#"{{"journal_id":"{ID}","journal_guard":{flag}}}"#);
        let (address, server) = listen(vec![
            ("/api/hosts", 200, "application/json", HOSTS.into()),
            ("/api/studio-identity", 200, "application/json", identity),
        ]);
        let live = probe(&client().unwrap(), &address, Some(ID)).unwrap();
        assert_eq!(live.access, expected);
        server.join().unwrap();
    }
    for previous in [None, Some(Access::ReadOnly), Some(Access::JournalBound)] {
        assert!(Access::ReadOnly.requires_confirmation(previous, false));
    }
    assert!(Access::ReadOnly.requires_confirmation(Some(Access::JournalBound), true));
    assert!(Access::JournalBound.requires_confirmation(Some(Access::ReadOnly), true));
    assert!(Access::JournalBound.requires_confirmation(Some(Access::ReadOnly), false));
    assert!(!Access::ReadOnly.requires_confirmation(Some(Access::ReadOnly), true));
    assert!(!Access::JournalBound.requires_confirmation(None, false));
}

#[test]
fn fallback_only_on_missing_identity_endpoint() {
    for (health, ok) in [(IDENTITY, true), ("{}", false)] {
        let (address, server) = listen(vec![
            ("/api/hosts", 200, "application/json", HOSTS.into()),
            ("/api/studio-identity", 404, "text/plain", "".into()),
            (
                "/api/runtime-health",
                200,
                "application/json",
                health.into(),
            ),
        ]);
        assert_eq!(probe(&client().unwrap(), &address, Some(ID)).is_ok(), ok);
        server.join().unwrap();
    }
    let (address, server) = listen(vec![
        ("/api/hosts", 200, "application/json", HOSTS.into()),
        (
            "/api/studio-identity",
            200,
            "application/json",
            r#"{"journal_id":"?"}"#.into(),
        ),
    ]);
    assert!(probe(&client().unwrap(), &address, None).is_err());
    server.join().unwrap();
}

#[test]
fn malformed_wrong_type_large_and_redirected_hosts_fail_closed() {
    for (code, mime, body) in [
        (200, "text/html", HOSTS.into()),
        (302, "application/json", HOSTS.into()),
        (200, "application/json", " ".repeat(256 * 1024 + 1)),
        (
            200,
            "application/json",
            r#"{"hosts":[{"id":"elsewhere","name":"x"}]}"#.into(),
        ),
    ] {
        let (address, server) = listen(vec![("/api/hosts", code, mime, body)]);
        assert!(probe(&client().unwrap(), &address, None).is_err());
        server.join().unwrap();
    }
}

#[test]
fn profile_retains_selected_route_and_rejects_invalid_replacements() {
    let root = tempfile::tempdir().unwrap();
    assert!(read(root.path()).unwrap().is_none());
    let mut connection = Connection {
        origin: "http://127.0.0.1:8766".into(),
        journal_id: ID.into(),
        page: "/".into(),
    };
    save(root.path(), &connection).unwrap();
    connection.page = "/?host=local&workspace=abc&run=def#goal".into();
    save(root.path(), &connection).unwrap();
    let good = Some(connection.clone());
    assert_eq!(read(root.path()).unwrap(), good);
    connection.page = "/api/hosts".into();
    assert!(save(root.path(), &connection).is_err());
    assert_eq!(read(root.path()).unwrap(), good);
    fs::write(
        root.path().join("connection.json"),
        br#"{"origin":"http://127.0.0.1"}"#,
    )
    .unwrap();
    assert!(read(root.path()).is_err());
}
