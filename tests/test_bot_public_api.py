"""The browser and standalone clients consume the same versioned representation."""
import json
from http.client import HTTPConnection

from test_studio_http import request, studio


def test_manifest_and_virtual_tree_work_between_workspaces(tmp_path):
    with studio(tmp_path / "journal.db", accept_remote=True) as server:
        first = server.catalog.create("first", [str(tmp_path)])["id"]
        second = server.catalog.create("second", [str(tmp_path)])["id"]
        spec = request(server, "/api/bot-spec")[1]
        assert request(server, spec["schema"])[1]["$schema"].endswith("2020-12/schema")
        package = request(server, spec["example"])[1]
        assert package["specVersion"] == spec["specVersion"]
        path = "/api/rooms/" + first + "/bots"
        member = path + "/" + package["id"]
        for _ in range(2):
            status, receipt = request(server, path + "/import", package)
            assert status == 200 and receipt["bot"] == package
        status, tree = request(server, member + "/assets")
        assert status == 200 and "notes/interviews" in tree["folders"]
        edit = {"filename": "hooks/review.json", "content": "{}", "create": True, "digest": tree["digest"]}
        assert request(server, member + "/assets", edit)[0] == 200
        assert request(server, member + "/assets", edit)[0] == 409
        digest = request(server, member + "/assets")[1]["digest"]
        extra = {"filename": ".notes.md", "content": "draft", "create": True, "digest": digest}
        status, accepted = request(server, member + "/assets", extra)
        assert status == 200
        assert request(server, member + "/assets", {"filename": ".notes.md", "content": "saved",
                                                    "digest": accepted["digest"]})[0] == 200
        portable = request(server, member + "/manifest")[1]
        assert portable["files"]["hooks/review.json"] == "{}" and portable["extensions"] == package["extensions"]
        assert request(server, "/api/rooms/" + second + "/bots/import", portable)[1]["bot"] == portable
        assert request(server, "/api/peer/" + server.journal_id + path[4:] + "/import", package)[0] == 404
        assert request(server, path + "/import", package, Origin="https://elsewhere.invalid")[0] == 403
        raw = json.dumps(package).replace('"specVersion":', '"specVersion":"bogus","specVersion":')
        conn = HTTPConnection(*server.server_address)
        try:
            conn.request("POST", path + "/import", raw,
                         {"Origin": f"http://127.0.0.1:{server.server_port}", "X-Agent-Office":"studio",
                          "Content-Type":"application/json"})
            assert conn.getresponse().status == 400
        finally:
            conn.close()


def test_client_entry_points_and_packaged_assets(tmp_path):
    with studio(tmp_path / "office.db") as server:
        assert b"workspace-bootstrap.js" in request(server, "/")[1]
        ws = server.catalog.create("registered", [str(tmp_path)])["id"]
        assert b"bot-client.js" in request(server, "/")[1]
        assert b"bot-client.js" in request(server, "/bots?workspace="+ws+"&bot=research")[1]
        status, module = request(server, "/bot-client-packages.js")
        assert status == 200 and b"mountBotPackages" in module
        assert b"workspace-bootstrap.js" in request(server, "/workspace?workspace="+ws)[1]
        assert b"workspace-bootstrap.js" in request(server, "/?workspace="+ws+"&run="+"b"*32)[1]
        assert request(server, "/bots?workspace=invalid")[0] == 400
        for path in ("figure-0.webp", "figure-11.webp", "study-seated-king.webp",
                     *(f"walk-{i}.webp" for i in range(12))):
            status, image = request(server, "/bot-art/" + path)
            assert status == 200 and image[8:12] == b"WEBP"
