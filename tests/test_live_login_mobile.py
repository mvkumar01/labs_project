"""Mobile login: a phone that restores the login page from its cache must not post a token whose cookie is gone."""
from __future__ import annotations

import pytest
import storage.live_db as live_db


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    live_db.init_live_db()
    from app import app
    return app.test_client()


@pytest.mark.parametrize("path", ["/live/login", "/live/register"])
def test_auth_pages_are_not_cached_and_reload_from_bfcache(client, path):
    r = client.get(path)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert "e.persisted" in r.get_data(as_text=True)
    cookie = r.headers["Set-Cookie"]
    assert "Expires=" in cookie and "HttpOnly" in cookie       # the token's cookie outlives a browser restart


def test_other_live_pages_keep_their_caching(client):
    assert "Cache-Control" not in client.get("/live/").headers                   # redirect to login, untouched


def test_a_fresh_page_passes_and_a_stale_token_still_fails(client):
    page = client.get("/live/login").get_data(as_text=True)
    token = page.split('name="csrf_token" value="')[1].split('"')[0]
    ok = client.post("/live/login", data={"csrf_token": token, "username": "nobody", "passcode": "x"})
    assert ok.status_code == 401 and "Incorrect username" in ok.get_data(as_text=True)
    stale = client.post("/live/login", data={"csrf_token": "old-page-token", "username": "nobody", "passcode": "x"})
    assert stale.status_code == 400 and "Session expired" in stale.get_data(as_text=True)
