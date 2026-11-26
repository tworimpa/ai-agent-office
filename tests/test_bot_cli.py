import json

from agent_office import cli
from agent_office.store import TaskStore
from agent_office.studio_catalog import StudioCatalog


def test_registered_workspace_bot_commands(tmp_path, capsys):
    journal = tmp_path / "tasks.sqlite3"
    with TaskStore(journal):
        pass
    catalog = StudioCatalog(journal)
    workspace = catalog.create("Office", [str(tmp_path)])["id"]
    other = catalog.create("Second", [str(tmp_path)])["id"]

    def invoke(*arguments):
        status = cli.main(["--db", str(journal), "bots", *arguments])
        output = capsys.readouterr()
        return status, json.loads(output.out if status == 0 else output.err)

    assert invoke("workspaces") == (0, {"workspaces": [{"id": workspace, "name": "Office"},
                                                      {"id": other, "name": "Second"}]})
    scope = ["--workspace-id", workspace]
    parameters = ["--id", "local-review", "--role", "reviewer"]
    status, response = invoke("create", *scope, *parameters, "--name", "Review",
                              "--instructions", "Inspect the changed tests.")
    assert status == 0
    definition = response["bot"]
    assert definition["id"] == "local-review"
    assert invoke("show", *scope, "--id", "local-review") == (0, {"bot": definition})
    assert invoke("assign", *scope, *parameters) == (0, {"role": "reviewer", "bot": definition})

    status, listing = invoke("list", *scope)
    assert status == 0
    assert listing["bindings"] == {"coordinator": "office-coordinator", "developer": "office-developer",
                                   "reviewer": "local-review"}
    assert any(bot["id"] == "local-review" for bot in listing["bots"])
    assert invoke("list", "--workspace-id", other)[1]["bindings"]["reviewer"] == "office-reviewer"
    assert invoke("show", "--workspace-id", other, "--id", "local-review")[0] == 2
    assert invoke("assign", *scope, "--id", "local-review", "--role", "coordinator")[0] == 2
    assert invoke("assign", *scope, "--id", "office-reviewer", "--role", "reviewer")[0] == 0
    assert invoke("list", *scope)[1]["bindings"]["reviewer"] == "office-reviewer"
