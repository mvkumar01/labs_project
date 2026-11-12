"""Asset grouping, personal Watch Lists and the shared Spotlight."""
import json
import sqlite3
from pathlib import Path

import jinja2
import pytest
from flask import Flask

from labs.services.overview_navigation import DEFAULT_SPOTLIGHT, asset_for, build_navigation, strategy_keys
from labs.services.display_format import planning_capital
from live.auth_gate import register_auth_gate
from storage import live_db


def card(key, instrument):
    return dict(key=key, label=key, instrument=instrument, status="Running", size="1 lot",
                today_net=1, total_net=100, position="Flat", open_net=0, win_pct=50,
                win_days=1, active_days=2, capital=10000, sessions=2, last_date="2026-10-29")


@pytest.mark.parametrize("key,instrument,group", [
    ("btc_rsi_roc", "BTCUSDT spot (Binance)", "crypto"),
    ("crypto_xs_trend", "Binance BTC/ETH perpetuals", "crypto"),
    ("gold_cci_v2", "GOLD futures", "commodities"),
    ("dry_gold", "GOLDM futures", "commodities"),
    ("crudem_combo", "CRUDEOILM futures", "commodities"),
    ("proposer_nifty_v5", "NIFTY options", "nifty"),
    ("proposer_v5_dt25", "SENSEX options", "sensex"),
    ("bn_alpha", "BANKNIFTY options", "banknifty"),
    ("bn_selective_guard", "Bank Nifty options", "banknifty"),
    ("nifty_omega", "NIFTY options", "nifty"),
    ("infy_tcs_pair", "INFY and TCS shares", "other"),
    ("risk_core", "NIFTY, BANK NIFTY, BTC, ETH, crude", "other"),
    ("expiry_strangle", "NIFTY + SENSEX options", "other"),
])
def test_asset_classification(key, instrument, group):
    assert asset_for(card(key, instrument)) == group


def test_every_strategy_in_exactly_one_group_and_invalid_asset_resets():
    cards = [card("gold_cci_v2", "GOLD futures"), card("proposer_nifty_v5", "NIFTY options")]
    nav = build_navigation(cards, "not-a-group")
    assert nav["asset"] is None
    assert [g["key"] for g in nav["groups"]] == ["crypto", "commodities", "nifty", "banknifty", "sensex"]
    assert sum(g["count"] for g in nav["groups"]) == len(cards)
    assert build_navigation(cards, "nifty")["cards"] == [cards[1]]


def render(cards, asset=None, spotlight=DEFAULT_SPOTLIGHT, admin=False):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader("templates"), autoescape=True)
    env.filters["planning_capital"] = planning_capital
    from urllib.parse import urlencode
    env.globals["url_for"] = lambda endpoint, **kw: "/labs/live?" + urlencode(kw)
    return env.get_template("_overview.html").render(overview_cards=cards, can_manage_spotlight=admin,
        overview_navigation=build_navigation(cards, asset, spotlight), date_from="2026-10-01", date_to="2026-10-29")


def test_overview_has_spotlight_first_groups_and_inert_strategy_library():
    html = render([card("gold_cci_v2", "GOLD futures"), card("crypto_xs", "Binance perpetuals")])
    assert html.index('id="spotlight-title"') < html.index('id="asset-title"')
    assert 'data-asset="crypto"' in html and 'asset=crypto' in html
    assert 'data-asset="banknifty"' in html and 'asset=banknifty' in html
    assert 'id="watch-list-title">Watch List' in html
    assert '<template data-card-template="gold_cci_v2">' in html
    assert 'date_from=2026-10-01&amp;date_to=2026-10-29' in html
    assert 'data-preferences-url=' in html


def test_group_page_only_shows_member_tiles_and_keeps_filtered_links():
    html = render([card("gold_cci_v2", "GOLD futures"), card("crypto_xs", "Binance perpetuals")], "commodities")
    assert 'data-strategy-key="gold_cci_v2"' in html
    assert 'data-strategy-key="crypto_xs"' not in html
    assert 'tab=gold_cci_v2' in html and 'date_from=2026-10-01' in html
    assert 'data-watch-toggle="gold_cci_v2"' in html


@pytest.fixture
def client(tmp_path, monkeypatch):
    from labs.services import overview_navigation as nav
    from labs.ui.live_routes import live_bp
    allowed = strategy_keys() | set(DEFAULT_SPOTLIGHT)          # the default list's books, committed or not
    monkeypatch.setattr(nav, "strategy_keys", lambda: allowed)
    from live.control_plane import init_schema
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    c = live_db.get_live_conn()
    live_db.init_live_db(c)
    init_schema(c)
    for uid in ("alice", "bob", "admin"):
        c.execute("INSERT INTO live_users VALUES(?,?,?,?)", (uid, uid, "unused", "now"))
    c.execute("INSERT INTO live_admin_roles VALUES (?)", ("admin",))
    c.commit(); c.close()
    app = Flask(__name__)
    app.secret_key = "test-only"
    app.config["SESSION_COOKIE_SECURE"] = False
    register_auth_gate(app)
    app.register_blueprint(live_bp)
    return app.test_client()


URL = "/live/api/overview-preferences"
SPOTLIGHT_URL = "/live/api/overview-spotlight"


def login(client, user):
    with client.session_transaction() as s:
        s.clear(); s["user_id"] = user
    response = client.get(URL, headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    return response.json["csrf_token"]


def test_guests_cannot_read_or_write_account_settings(client):
    assert client.get(URL).status_code == 401
    assert client.post(URL, json={"keys": ["gold_cci_v2"]}).status_code == 401


def test_account_settings_persist_and_do_not_cross_users(client):
    token = login(client, "alice")
    response = client.post(URL, json={"keys": ["gold_cci_v2", "proposer_nifty_v5", "gold_cci_v2"], "user_id": "bob"}, headers={"X-CSRF-Token": token})
    assert response.status_code == 200 and response.json["keys"] == ["gold_cci_v2", "proposer_nifty_v5"]
    assert client.get(URL).json["keys"] == response.json["keys"]
    assert login(client, "bob")
    assert client.get(URL).json["keys"] == []
    login(client, "alice")
    assert client.get(URL).json["keys"] == ["gold_cci_v2", "proposer_nifty_v5"]


def test_invalid_and_missing_csrf_do_not_overwrite_saved_settings(client):
    token = login(client, "alice")
    headers = {"X-CSRF-Token": token}
    assert client.post(URL, json={"keys": ["gold_cci_v2"]}, headers=headers).status_code == 200
    for value in ("gold_cci_v2", ["<script>bad</script>"], ["gold_cci_v2"] * 101, [42]):
        assert client.post(URL, json={"keys": value}, headers=headers).status_code == 400
    assert client.post(URL, json={"keys": []}).status_code == 400
    assert client.get(URL).json["keys"] == ["gold_cci_v2"]
    assert client.post(URL, json={"keys": []}, headers=headers).json["keys"] == []


def test_spotlight_contains_only_curated_tiles_and_the_editor_is_admin_only():
    assert DEFAULT_SPOTLIGHT == ("nifty_omega", "bn_selective_guard", "bn_selective", "proposer_nifty_scalper_v5")
    cards = [card("nifty_omega", "NIFTY options"), card("gold_cci_v2", "GOLD futures"),
             card("bn_selective_guard", "BANKNIFTY options"), card("proposer_nifty_scalper_v5", "NIFTY options")]
    assert build_navigation(cards, "banknifty")["cards"] == [cards[2]]
    assert [c["key"] for c in build_navigation(cards)["spotlight"]] == [
        "nifty_omega", "bn_selective_guard", "proposer_nifty_scalper_v5"]
    page = render(cards)
    spotlight = page.split('data-spotlight-grid>', 1)[1].split('</section>', 1)[0]
    assert 'data-strategy-key="nifty_omega"' in spotlight and 'data-strategy-key="gold_cci_v2"' not in spotlight
    assert 'data-watch-grid></div>' in page and 'data-spotlight-manage' not in page
    assert 'data-spotlight-manage' in render(cards, admin=True)
    assert 'labs.spotlight.v1' in Path("static/overview.js").read_text(encoding="utf-8")


def test_only_admins_can_change_shared_spotlight_without_changing_watch_lists(client):
    from labs.ui.overview_preferences import spotlight_settings
    assert client.get(SPOTLIGHT_URL).status_code == 401
    alice = login(client, "alice")
    headers = {"X-CSRF-Token": alice}
    assert client.get(SPOTLIGHT_URL).status_code == 403
    assert client.post(SPOTLIGHT_URL, json={"keys": []}, headers=headers).status_code == 403
    assert client.post(URL, json={"keys": ["nifty_omega"]}, headers=headers).json["keys"] == ["nifty_omega"]
    admin = login(client, "admin")
    assert client.get(SPOTLIGHT_URL).json["keys"] == list(DEFAULT_SPOTLIGHT)
    assert client.post(SPOTLIGHT_URL, json={"keys": []}).status_code == 400
    headers = {"X-CSRF-Token": admin}
    assert client.post(SPOTLIGHT_URL, json={"keys": ["unknown"]}, headers=headers).status_code == 400
    picks = ["proposer_nifty_scalper_v5", "bn_selective_guard"]
    assert client.post(SPOTLIGHT_URL, json={"keys": picks}, headers=headers).json["keys"] == picks
    assert client.get(URL).json["keys"] == []
    login(client, "alice")
    assert client.get(URL).json["keys"] == ["nifty_omega"]
    with client.application.test_request_context():
        assert spotlight_settings() == (picks, False)
