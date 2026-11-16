"""Shared-records connection strings: passwords with symbols; the Supabase schema file stays in step."""
from pathlib import Path
from urllib.parse import unquote, urlsplit

from snapstl import config, sync


def test_password_with_symbols_is_encoded():
    raw = "postgresql://postgres:p@ss)w#rd/x?%zz@db.example.supabase.co:5432/postgres"
    url = config.encode_password(raw)
    u = urlsplit(url)
    assert (u.hostname, u.port, u.path) == ("db.example.supabase.co", 5432, "/postgres")
    assert unquote(u.password) == "p@ss)w#rd/x?%zz" and config.check_url(raw) == ""
    assert config.describe(raw) == "postgres @ db.example.supabase.co"
    # already encoded or plain: unchanged
    for ok in ("postgresql://postgres.abc:Simple123@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
               "postgresql://postgres:ab%23c%29d@h.example:5432/postgres"):
        assert config.encode_password(ok) == ok
    assert "YOUR-PASSWORD" in config.check_url("postgresql://postgres:[YOUR-PASSWORD]@h:5432/postgres")


def test_schema_file_matches_the_app():
    f = Path(__file__).resolve().parents[1] / "docs" / "supabase-schema.sql"
    assert f.read_text() == sync.schema_sql(), "run: uv run snapstl-db schema > docs/supabase-schema.sql"


def test_nothing_changes_without_the_shared_records(monkeypatch):
    """Every computer needs a live connection to Supabase: without it pages show, but no work can be done
    (only Settings, Sync and the helper), and the app says why."""
    from fastapi.testclient import TestClient

    from snapstl import server

    c = TestClient(server.app, base_url="http://127.0.0.1:8317", client=("127.0.0.1", 50000))
    monkeypatch.setattr(sync, "REQUIRED", True)
    r = c.post("/api/ingest", files={"file": ("r.csv", b"x")})
    assert r.status_code == 503 and "connect them in Settings" in r.json()["detail"]
    st = c.get("/api/state").json()["shared"]
    assert st == {"required": True, "online": False, "reason": sync.offline_reason()}
    assert c.post("/api/sync").status_code == 400  # allowed through: says what to do
    assert c.post("/api/shared-records", json={"url": "nonsense"}).status_code == 400
    assert c.get("/api/orders/facets?tab=ready").status_code == 200

    monkeypatch.setattr(sync, "enabled", lambda: True)
    monkeypatch.setattr(sync.STATUS, "last_ok", 0.0)
    monkeypatch.setattr(sync.STATUS, "error", "Can't reach the shared records: timeout")
    r = c.post("/api/batches", json={"order_ids": []})
    assert r.status_code == 503 and "timeout" in r.json()["detail"]
    monkeypatch.setattr(sync.STATUS, "last_ok", 1.0)
    monkeypatch.setattr(sync.STATUS, "error", "")
    assert c.post("/api/batches", json={"order_ids": []}).status_code != 503  # back: work goes on
