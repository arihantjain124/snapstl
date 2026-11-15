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
