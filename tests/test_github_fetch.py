"""A real Git smart-HTTPS transfer between disposable repositories; no external network."""
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from agent_office.github_connection import GitHubError
from agent_office.github_fetch import ensure_base


def git(root, *args, data=None):
    return subprocess.run(["git", *args], input=data, cwd=root, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout


@pytest.fixture
def transport(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "fixture")
    git(source, "config", "user.email", "fixture@example.com")
    (source/"readme").write_text("old")
    git(source, "add", ".")
    git(source, "commit", "-m", "old")
    checkout = tmp_path / "registered"
    git(tmp_path, "clone", str(source), str(checkout))
    git(checkout, "config", "http.extraHeader", "X-Untrusted-Checkout: fixture")
    (source/"readme").write_text("new")
    git(source, "commit", "-am", "new")
    sha = git(source, "rev-parse", "HEAD").decode().strip()
    script = Path(__file__).resolve().parents[1] / "desktop" / "fixtures" / "github_tls.py"
    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True)
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.serve_git(True)
        def do_POST(self):
            self.serve_git(False)
        def serve_git(self, advertise):
            route = urlsplit(self.path).path
            calls.append((self.command, route, self.headers.get("X-Untrusted-Checkout")))
            if route.endswith("/info/refs") and getattr(client, "redirect", False):
                self.send_response(302)
                self.send_header("Location", client.web_url + "/different/repo.git/info/refs")
                self.end_headers()
                return
            expected = "Basic " + base64.b64encode(b"x-access-token:temporary-test-token").decode()
            if self.headers.get("Authorization") != expected:
                self.send_error(403)
                return
            if route == "/owner/repo.git/info/refs" and advertise:
                content = b"001e# service=git-upload-pack\n0000" + git(source, "upload-pack", "--stateless-rpc",
                                                                          "--advertise-refs", ".")
                kind = "advertisement"
            elif route == "/owner/repo.git/git-upload-pack" and not advertise:
                req = self.rfile.read(int(self.headers["Content-Length"]))
                content = git(source, "upload-pack", "--stateless-rpc", ".", data=req)
                kind = "result"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-git-upload-pack-" + kind)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(tmp_path/"ca.pem", tmp_path/"tls.pem")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    @contextmanager
    def read_token(slug, expected_repo_id):
        assert slug == "owner/repo"
        yield "temporary-test-token"
    client = SimpleNamespace(git_read_token=read_token, branch_sha=lambda slug, branch: sha,
                             web_url=f"https://127.0.0.1:{server.server_port}",
                             config=SimpleNamespace(proxy="direct",ca_file=tmp_path/"ca.pem"))
    try:
        yield checkout, sha, client, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_missing_commit_hydrated_over_https_without_changing_the_checkout(transport):
    checkout, sha, client, calls = transport
    (checkout/"readme").write_text("dirty operator work")
    before = git(checkout, "show-ref")
    assert ensure_base(checkout, "owner/repo", "main", sha, client) == sha
    assert git(checkout, "cat-file", "-p", sha+":readme") == b"new"
    assert (checkout/"readme").read_text() == "dirty operator work"
    assert before == git(checkout, "show-ref")
    assert not (checkout/".git"/"FETCH_HEAD").exists()
    assert calls and all(not row[2] for row in calls)
    assert {p for _, p, _ in calls} == {"/owner/repo.git/info/refs", "/owner/repo.git/git-upload-pack"}
    calls.clear()
    assert ensure_base(checkout, "owner/repo", "main", sha, client) == sha
    assert not calls


def test_head_changed_so_objects_never_imported(transport):
    checkout, sha, client, _ = transport
    client.branch_sha = lambda *args: "b"*40
    with pytest.raises(GitHubError, match="fetch_head_changed"):
        ensure_base(checkout, "owner/repo", "main", sha, client)
    result = subprocess.run(["git", "cat-file", "-e", sha], cwd=checkout, capture_output=True)
    assert result.returncode != 0


def test_git_http_redirect_is_refused_without_contacting_destination(transport):
    checkout, sha, client, calls = transport
    client.redirect = True
    with pytest.raises(GitHubError, match="git_fetch_failed"):
        ensure_base(checkout, "owner/repo", "main", sha, client)
    assert calls and all(route == "/owner/repo.git/info/refs" for _,route,_ in calls)
