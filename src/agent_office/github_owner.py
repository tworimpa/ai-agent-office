"""Pin automatic intake to one journal even when a user has two independent Studios."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3


def verify_owner(journal, client, repo_id, *, claim=False):
    pinned = getattr(client.config, "owner_journal", None)
    if pinned is not None and pinned != journal:
        raise ValueError("이 GitHub 연결의 자동 작업은 다른 Studio 저널에 지정되어 있습니다.")
    path = os.environ.get("AGENT_OFFICE_GITHUB_OWNER_DB")
    if path:
        db_path = Path(path)
        if not db_path.is_absolute():
            raise ValueError("GitHub 자동 작업 소유권 DB에는 절대 경로가 필요합니다.")
    else:
        base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_STATE_HOME")
        db_path = (Path(base) if base else Path.home()/".local"/"state") / "AI-Agent-Office" / "github-owners.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ident = (client.web_url.rstrip("/").casefold(), repo_id)
    with closing(sqlite3.connect(db_path, timeout=5)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS owners(instance TEXT, repo INTEGER, journal TEXT NOT NULL, PRIMARY KEY(instance,repo))")
        if claim:
            db.execute("INSERT OR IGNORE INTO owners VALUES(?,?,?)", (*ident, journal))
        owner = db.execute("SELECT journal FROM owners WHERE instance=? AND repo=?", ident).fetchone()
        if owner != (journal,):
            raise ValueError("저장소를 담당하는 Studio 저널이 다릅니다. 원래 작업실에 연결하세요.")
