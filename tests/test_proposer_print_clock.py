"""Print clocks: Nifty Scalper V3 prints every 3 minutes (live and paper); Pramanaa's timing is a paper book only."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from labs.engine import proposer_px_tracker as tr
from live import live_executor as ex
from live import live_service as svc
from live import proposer_runner as pr
from live.engine import proposer_engine as pe
from storage import live_db
from storage.live_db import init_live_db
from tests.test_configure_strategy import CONN_ID, CSRF_TOKEN, USER_ID, _set_strategy, _strategy_pair
from tests.test_configure_strategy import client  # noqa: F401  (fixture)

DAY = datetime(2026, 10, 5)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    monkeypatch.delenv("PROPOSER_REGIME_SOURCE", raising=False)
    init_live_db()
    pr.ensure_schema()
    pr.ensure_regime_schema()


def _feed(clock, n_today=60):
    base = DAY.replace(hour=9, minute=15)
    today = [(base + timedelta(minutes=i), 74000.0 + 3 * i) for i in range(n_today)]
    prior = [(datetime(2026, 10, 1, 14, 0) + timedelta(minutes=i), 74000.0) for i in range(80)]
    bars = lambda day: today if day == base.date() else (prior if day.isoformat() == "2026-10-01" else [])  # noqa: E731
    return pr.PredictorFeed(bars_fn=bars, chain_fn=lambda d: pd.DataFrame(), open_fn=lambda d: None, clock=clock)


def test_v3_is_v1_on_a_3_minute_clock_on_nifty():
    v1, v3 = pe.STRATEGY_VERSION_NIFTY_SCALPER_V1, pe.STRATEGY_VERSION_NIFTY_SCALPER_V3
    assert pe.params_for(v3) == pe.params_for(v1)
    assert pe.params_for(v3, 0.02) == pe.params_for(v1, 0.02)              # the day target is editable
    assert v3 in pe.KNOWN_VERSIONS and v3 in pe.DAY_TARGET_VERSIONS and ex.is_proposer_strategy(v3)
    assert pr.instrument_for(v3) is pr.NIFTY_I
    assert pr.print_clock_for(v3) == pr.PRINT_CLOCK_3MIN
    assert pr.print_clock_for(v1) == pr.print_clock_for("proposer_dt35_v5") == pr.PRINT_CLOCK_5MIN


def test_3_minute_feed_prints_at_0922_then_every_180_s_in_its_own_table(db):
    feed, five = _feed(pr.PRINT_CLOCK_3MIN), _feed(pr.PRINT_CLOCK_5MIN)
    first = DAY.replace(hour=9, minute=22, second=7)
    assert feed.step(first - timedelta(minutes=1)) is None                # six completed bars at 09:21
    p = feed.step(first)
    assert p and p["x5_asof"] == "2026-10-05T09:22:07"
    assert feed.next_due == first + timedelta(seconds=180)
    assert feed.step(first + timedelta(seconds=179)) is None
    assert feed.step(first + timedelta(seconds=181))
    assert five.step(first) and five.next_due == first + timedelta(minutes=5)
    assert len(pr.prints_today("2026-10-05", table=pr.PRINTS_TABLE[pr.PRINT_CLOCK_3MIN])) == 2
    assert len(pr.prints_today("2026-10-05")) == 1


def test_paper_books_v3_every_3_min_and_pramanaa_timing():
    base = DAY.replace(hour=9, minute=15)
    today = [(base + timedelta(minutes=i), 74000.0 + 3 * i) for i in range(60)]
    prior = [(datetime(2026, 10, 1, 14, 0) + timedelta(minutes=i), 74000.0) for i in range(80)]
    through = base + timedelta(minutes=40)
    v3, _ = tr._prints("2026-10-05", prior, today, 74000.0, {}, through, gap_s=tr.NIFTY_V3.print_gap_s)
    pc, _ = tr._prints("2026-10-05", prior, today, 74000.0, {}, through, gap_s=tr.NIFTY_V3_PCLOCK.print_gap_s,
                       forming=tr.NIFTY_V3_PCLOCK.forming)
    assert [p["t"].strftime("%H:%M") for p in v3[:4]] == ["09:22", "09:25", "09:28", "09:31"]
    assert (pc[0]["t"].strftime("%H:%M"), pc[0]["sec"]) == ("09:21", 45.0)
    assert [p["t"].strftime("%H:%M") for p in pc[:4]] == ["09:21", "09:26", "09:32", "09:37"]
    for book in (tr.NIFTY_V3, tr.NIFTY_V3_PCLOCK):
        assert book.key in tr.NIFTY_BOOKS and book.key in tr.DELTA_STRIKE_BOOKS
        assert tr.params_on("2026-10-20", book) == tr.params_on("2026-10-20", tr.NIFTY_V5)


def _post(client, strategy, day_target="1.5"):                               # noqa: F811
    return client.post("/live/configure", data={"csrf_token": CSRF_TOKEN, "lots": "1", "daily_loss_cap": "3000",
                                                "strategy": strategy, "day_target": day_target})


def test_configure_offers_nifty_scalper_v3_with_a_day_target(client):       # noqa: F811
    _set_strategy("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V1)
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", ex.PROPOSER_DECISION_ABI)
    page = client.get("/live/configure").get_data(as_text=True)
    assert 'value="nifty_scalper_v3"' in page and 'name="print_clock"' not in page
    assert _post(client, "nifty_scalper_v3", "2.5").status_code == 302
    assert _strategy_pair() == ("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V3)
    assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "2.5"
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", "proposer-dt25-live-v14")
    _set_strategy("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V1)
    assert "restarted first" in _post(client, "nifty_scalper_v3").get_data(as_text=True)
    assert _strategy_pair() == ("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V1)
