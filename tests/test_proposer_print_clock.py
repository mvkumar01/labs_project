"""Per-connection print clock: ours (09:22, every 300 s) or Pramanaa's (forming candle, 09:21:45, 310.5 s)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from labs.engine import proposer_px_tracker as tr
from live import live_service as svc
from live import proposer_runner as pr
from storage import live_db
from storage.live_db import init_live_db

USER, CONN = "u1", "u1:zerodha"
DAY = datetime(2026, 10, 5)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    monkeypatch.delenv("PROPOSER_REGIME_SOURCE", raising=False)
    init_live_db()
    pr.ensure_schema()
    pr.ensure_regime_schema()
    svc.upsert_connection(USER, CONN, broker="zerodha", account_label="T", account_ref="zerodha:T",
                          status="connected")


def _bars(n_today: int):
    base = DAY.replace(hour=9, minute=15)
    today = [(base + timedelta(minutes=i), 74000.0 + 3 * i) for i in range(n_today)]
    prior = [(datetime(2026, 10, 1, 14, 0) + timedelta(minutes=i), 74000.0) for i in range(80)]
    return lambda day: today if day == base.date() else (prior if day.isoformat() == "2026-10-01" else [])


def _feed(clock, n_today=6, spot=74020.0):
    return pr.PredictorFeed(bars_fn=_bars(n_today), chain_fn=lambda d: pd.DataFrame(), open_fn=lambda d: None,
                            clock=clock, spot_fn=lambda: spot)


def test_pramanaa_clock_counts_the_forming_candle_and_prints_first_at_092145(db):
    feed = _feed(pr.PRINT_CLOCK_PRAMANAA, n_today=6)          # completed 09:15..09:20
    assert feed.step(DAY.replace(hour=9, minute=21, second=30)) is None      # before 09:21:45
    p = feed.step(DAY.replace(hour=9, minute=21, second=46))
    assert p is not None and p["x5_asof"] == "2026-10-05T09:21:46"
    ours = _feed(pr.PRINT_CLOCK_OURS, n_today=6)
    assert ours.step(DAY.replace(hour=9, minute=21, second=46)) is None      # ours needs 7 completed bars
    # stored in its own table, ours untouched
    assert [r["asof"] for r in pr.prints_today("2026-10-05", table="live_proposer_prints_pclock")] == [p["x5_asof"]]
    assert pr.prints_today("2026-10-05") == []


def test_pramanaa_clock_every_310_5_s_from_when_due(db):
    feed = _feed(pr.PRINT_CLOCK_PRAMANAA, n_today=12)
    first = DAY.replace(hour=9, minute=21, second=46)
    assert feed.step(first)
    assert feed.next_due == first + timedelta(seconds=310.5)
    assert feed.step(first + timedelta(seconds=310)) is None
    second = first + timedelta(seconds=311.4)                 # the cycle came 0.9 s late
    assert feed.step(second)
    assert feed.next_due == first + timedelta(seconds=621)    # counted from the due time, not the print
    # a restart keeps the phase
    again = _feed(pr.PRINT_CLOCK_PRAMANAA, n_today=12)
    again._new_day(DAY.date())
    assert again.next_due == datetime.fromisoformat(feed.latest["x5_asof"]) + timedelta(seconds=310.5)


def test_pramanaa_clock_waits_for_a_live_spot(db):
    feed = _feed(pr.PRINT_CLOCK_PRAMANAA, n_today=6, spot=None)
    assert feed.step(DAY.replace(hour=9, minute=21, second=50)) is None


def test_connection_reads_its_own_clock(db):
    assert pr.print_clock_for(USER, CONN) == "ours"
    svc.set_config(USER, CONN, "proposer_print_clock", "pramanaa")
    assert pr.print_clock_for(USER, CONN) == "pramanaa"
    svc.set_config(USER, CONN, "proposer_print_clock", "nonsense")
    assert pr.print_clock_for(USER, CONN) == "ours"


def test_paper_prints_forming_first_at_092145_then_310_5_s():
    base = DAY.replace(hour=9, minute=15)
    today = [(base + timedelta(minutes=i), 74000.0 + 3 * i) for i in range(60)]
    prior = [(datetime(2026, 10, 1, 14, 0) + timedelta(minutes=i), 74000.0) for i in range(80)]
    through = base + timedelta(minutes=40)
    plain, _ = tr._prints("2026-10-05", prior, today, 74000.0, {}, through)
    twin, _ = tr._prints("2026-10-05", prior, today, 74000.0, {}, through, gap_s=tr.PRAMANAA_PRINT_GAP_S,
                         forming=True)
    assert (plain[0]["t"].strftime("%H:%M"), plain[0]["sec"]) == ("09:22", 25.0)
    assert (twin[0]["t"].strftime("%H:%M"), twin[0]["sec"]) == ("09:21", 45.0)
    assert [p["t"].strftime("%H:%M:%S")[:5] for p in twin[:4]] == ["09:21", "09:26", "09:32", "09:37"]
    assert tr.NIFTY_V5_PCLOCK.key in tr.NIFTY_BOOKS and tr.NIFTY_V5_PCLOCK.forming
    assert tr.PRAMANAA_PRINT_GAP_S == pr.PRAMANAA_PRINT_GAP_S


from live import live_executor as ex  # noqa: E402
from tests.test_configure_strategy import CONN_ID, CSRF_TOKEN, USER_ID, _set_strategy  # noqa: E402
from tests.test_configure_strategy import client  # noqa: E402,F401  (fixture)


def _post_clock(client, clock):                                               # noqa: F811
    return client.post("/live/configure", data={
        "csrf_token": CSRF_TOKEN, "lots": "1", "daily_loss_cap": "3000", "strategy": "nifty_scalper_v1",
        "day_target": "1.5", "print_clock": clock})


def test_configure_print_clock_needs_flat_and_a_current_runner(client):     # noqa: F811
    _set_strategy("proposer", "proposer_nifty_scalper_v1")
    svc.set_config(USER_ID, CONN_ID, "proposer_day_target_pct", "1.5")
    page = client.get("/live/configure").get_data(as_text=True)
    assert 'name="print_clock"' in page and 'value="ours" selected' in page
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", "proposer-dt25-live-v14")
    assert "updated Proposer runner" in _post_clock(client, "pramanaa").get_data(as_text=True)
    assert not svc.get_config(USER_ID, CONN_ID, "proposer_print_clock")
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", ex.PROPOSER_DECISION_ABI)
    assert _post_clock(client, "pramanaa").status_code == 302
    assert svc.get_config(USER_ID, CONN_ID, "proposer_print_clock") == "pramanaa"
    assert 'value="pramanaa" selected' in client.get("/live/configure").get_data(as_text=True)
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state["position"] = "OPEN"
    svc.save_trade_state(USER_ID, CONN_ID, state)
    assert "open position" in _post_clock(client, "ours").get_data(as_text=True)
    assert svc.get_config(USER_ID, CONN_ID, "proposer_print_clock") == "pramanaa"
