"""CRUDEOILM combination real-time runner, phase 0 (dry run): decisions made minute by minute must
arrive at the trades the back test takes, using nothing from the future."""
from __future__ import annotations

import inspect
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from live import crudem_runner as cr
from live.engine import crudem_combo_engine as eng

FIX = Path(__file__).resolve().parent / "fixtures"
SYMBOL = "CRUDEOILM26OCTFUT"


class Feed:
    """Serves the fixture as one listed contract, as of a movable clock."""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame.set_index("ts", drop=False)
        self.now: datetime | None = None
        self.candle_calls = 0

    def contracts(self):
        return [{"tradingsymbol": SYMBOL, "instrument_token": 111, "expiry": "2026-10-19"}]

    def candles(self, token, frm, to):
        self.candle_calls += 1
        f = self.frame[(self.frame.ts >= frm) & (self.frame.ts <= to)]
        return f[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

    def ltp(self, symbol):
        """The forming minute's bar, walked open -> low -> high -> close through the minute."""
        minute = pd.Timestamp(self.now).floor("min")
        if minute not in self.frame.index:
            return None
        bar = self.frame.loc[minute]
        return float([bar.open, bar.low, bar.high, bar.close][min(3, self.now.second // 15)])


@pytest.fixture(scope="module")
def frame():
    f = pd.read_parquet(FIX / "crudem_continuous_1min_to_20261006.parquet")
    return f[f.ts >= "2026-09-10"].reset_index(drop=True)             # enough warm-up for one session


@pytest.fixture
def env(tmp_path, monkeypatch, frame):
    monkeypatch.setattr(cr, "START", "2026-10-01")                      # the fixture ends on 6 Oct
    monkeypatch.setattr(cr, "notify_telegram", lambda *_a, **_k: None)
    conn = sqlite3.connect(tmp_path / "live.db")
    cr.ensure_schema(conn)
    feed = Feed(frame)

    def history(start: date, end: date):
        past = frame[frame.ts.dt.date < end]                            # what the paper loop cached before today
        return {SYMBOL: past.reset_index(drop=True)}, {SYMBOL: "2026-10-19"}

    def drive(runner, start: str, end: str):
        t, stop = datetime.fromisoformat(start), datetime.fromisoformat(end)
        while t <= stop:
            feed.now = t
            runner.step(t, conn)
            t += timedelta(seconds=15)

    yield conn, feed, history, drive
    conn.close()


def test_phase_0_has_no_broker_path():
    src = inspect.getsource(cr)
    assert cr.DRY_RUN is True
    for word in ("place_order", "placeOrder", "exit_all", "SmartApi", "live.brokers", "send_order"):
        assert word not in src


def test_dry_runner_takes_the_back_tests_trades_for_a_session(env):
    conn, feed, history, drive = env
    runner = cr.Runner(feed=feed, series=cr.Series(feed, history=history))
    drive(runner, "2026-10-06T10:05:05", "2026-10-06T11:32:00")
    row = conn.execute("SELECT cid, direction, entry_ts, exit_ts, entry_price, exit_reason, stop_price, target_price "
                       "FROM live_crudem_trades").fetchall()
    # the back test: rule 2803 short, signal 10:09, entry 10:10, target hit in the 11:30 bar
    assert len(row) == 1 and row[0][:2] == (2803, "short")
    assert row[0][2][:16] == "2026-10-06T10:10" and row[0][3][:16] == "2026-10-06T11:30" and row[0][5] == "target"
    entry_bar = feed.frame.loc[pd.Timestamp("2026-10-06 10:10")]
    assert row[0][4] == entry_bar.open                                  # decided 5 s in: filled at that price
    assert row[0][6] == round(row[0][6]) and row[0][7] == round(row[0][7])    # levels on the Rs 1 tick
    orders = conn.execute("SELECT kind, side, reason, delay_s, bar_open, fill_price, status, dry_run "
                          "FROM live_crudem_orders ORDER BY id").fetchall()
    assert [(o[0], o[1]) for o in orders] == [("entry", "SELL"), ("exit", "BUY")]
    assert orders[0][3] == 5.0 and orders[0][4] == entry_bar.open and all(o[6] == "DRY_FILLED" and o[7] == 1 for o in orders)
    assert conn.execute("SELECT outcome FROM live_crudem_decisions WHERE cid=2803").fetchone()[0] == "taken"
    assert cr.load_position(conn) is None

    # the evening trade, with a restart in the middle: the position is read back from the database
    drive(runner, "2026-10-06T20:10:05", "2026-10-06T20:17:00")
    pos = cr.load_position(conn)
    assert pos and pos["cid"] == 593002 and pos["side"] == 1 and pos["entry_ts"][:16] == "2026-10-06T20:15"
    assert pos["stop"] == int(pos["entry_price"] * 0.99) and pos["target"] >= pos["entry_price"] * 1.03
    restarted = cr.Runner(feed=feed, series=cr.Series(feed, history=history))
    drive(restarted, "2026-10-06T23:27:05", "2026-10-06T23:29:50")
    last = conn.execute("SELECT cid, exit_ts, exit_reason, gross_rs, charges_rs, net_rs FROM live_crudem_trades "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    assert last[0] == 593002 and last[1][:16] == "2026-10-06T23:29" and last[2] == "eod"
    assert last[5] == pytest.approx(last[3] - last[4], abs=0.01) and 40 < last[4] < 80
    assert cr.load_position(conn) is None


def test_a_signal_while_a_position_is_held_is_logged_not_taken(env):
    conn, feed, history, drive = env
    runner = cr.Runner(feed=feed, series=cr.Series(feed, history=history))
    cr.save_position(conn, {"ref": "held", "cid": 506248, "side": 1, "symbol": SYMBOL, "qty": 10,
                            "signal_ts": "x", "entry_ts": "2026-10-06T09:30:00", "entry_minute": "2026-10-06T09:30:00",
                            "entry_price": 8000.0, "stop_dist": 4000.0, "stop": 4000.0, "target": 20000.0})
    drive(runner, "2026-10-06T10:08:05", "2026-10-06T10:11:00")
    assert conn.execute("SELECT outcome, detail FROM live_crudem_decisions WHERE cid=2803").fetchone() == (
        "position_held", "held by 506248")
    assert conn.execute("SELECT COUNT(*) FROM live_crudem_trades").fetchone()[0] == 0


def test_a_missing_candle_skips_the_minute_and_a_stale_position_is_closed(env, monkeypatch):
    conn, feed, history, drive = env
    runner = cr.Runner(feed=feed, series=cr.Series(feed, history=history))
    real = feed.candles
    monkeypatch.setattr(feed, "candles", lambda token, frm, to: real(token, frm, to)[lambda f: f.ts < "2026-10-06 10:09"])
    drive(runner, "2026-10-06T10:08:05", "2026-10-06T10:11:00")          # the 10:09 signal bar never arrives
    assert conn.execute("SELECT COUNT(*) FROM live_crudem_trades").fetchone()[0] == 0
    assert runner.decided == datetime(2026, 10, 6, 10, 10)             # that boundary was given up on, once
    # a position left over from an earlier session is closed, flagged, not carried
    cr.save_position(conn, {"ref": "old", "cid": 2803, "side": -1, "symbol": SYMBOL, "qty": 10, "signal_ts": "x",
                            "entry_ts": "2026-10-05T22:00:00", "entry_minute": "2026-10-05T22:00:00",
                            "entry_price": 8000.0, "stop_dist": 80.0, "stop": 8080.0, "target": 7920.0})
    conn.execute("INSERT INTO live_crudem_trades (book, trade_ref, trade_date, cid, direction, symbol, qty, signal_ts, "
                 "entry_ts, entry_price, stop_price, target_price, stop_dist, dry_run) VALUES "
                 "('dry','old','2026-10-05',2803,'short',?,10,'x','2026-10-05T22:00:00',8000,8080,7920,80,1)", (SYMBOL,))
    feed.now = datetime(2026, 10, 6, 10, 12, 5)
    assert runner.step(feed.now, conn) is None
    assert conn.execute("SELECT exit_reason FROM live_crudem_trades WHERE trade_ref='old'").fetchone()[0] == "eod_missed"


def test_charges_match_the_back_tests_cost_model():
    from labs.engine.charges import mcx_futures_round_trip_charges
    for buy, sell in ((8704.0, 8617.0), (7660.0, 7889.8), (9213.0, 9166.9)):
        assert cr.charges(buy, sell, 10) == pytest.approx(mcx_futures_round_trip_charges(buy, sell, 10)["raw_total"], abs=1e-9)
    assert eng.levels(eng.MEMBER_BY_CID[2803], 8000.0, 31.5) == (31.5, 8031.5, 7968.5)
    assert eng.levels(eng.MEMBER_BY_CID[593002], 8000.0) == (80.0, 7920.0, 8240.0)
    # on the tick, outward from the entry: touched exactly when the unrounded level is
    assert cr.tick_levels(+1, 7919.5, 8240.2) == (7919.0, 8241.0)
    assert cr.tick_levels(-1, 8031.5, 7968.5) == (8032.0, 7968.0)
    assert cr.tick_levels(+1, 7920.0, 8240.0) == (7920.0, 8240.0)
