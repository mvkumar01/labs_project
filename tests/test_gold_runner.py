"""Gold CCI short real-time runner, phase 0 (dry run): decisions made minute by minute must arrive
at the back test's signals using nothing from the future, and the stop must move as the rule says."""
from __future__ import annotations

import inspect
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from labs.engine import gold_cci_tracker as gt
from live import crudem_runner as cr
from live import gold_runner as gr
from live import mcx_dry_loop as loop

SIGNAL, TRADED = "GOLD26OCTFUT", "GOLDM26OCTFUT"


class Feed:
    """Serves the seeded GOLD candles as one listed contract, and the same prices as GOLDM."""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame.set_index("ts", drop=False)
        self.now: datetime | None = None
        self.fixed: float | None = None

    def contracts(self):
        return [{"tradingsymbol": SIGNAL, "instrument_token": 111, "expiry": "2026-10-05"}]

    def traded(self, today: date):
        return {"tradingsymbol": TRADED, "instrument_token": 222, "expiry": "2026-10-05"}

    def candles(self, token, frm, to):
        f = self.frame[(self.frame.ts >= frm) & (self.frame.ts <= to)]
        return f[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

    def ltp(self, symbol):
        """The forming minute's bar, walked open -> low -> high -> close through the minute."""
        if self.fixed is not None:
            return self.fixed
        minute = pd.Timestamp(self.now).floor("min")
        if minute not in self.frame.index:
            return None
        bar = self.frame.loc[minute]
        return float([bar.open, bar.low, bar.high, bar.close][min(3, self.now.second // 15)])


@pytest.fixture(scope="module")
def frame():
    f = pd.read_parquet(gt.SEED_FILE).astype({c: "float64" for c in ("open", "high", "low", "close", "volume")})
    return f[f.ts >= "2026-07-25"].reset_index(drop=True)             # the runner's own 60-day window


@pytest.fixture
def env(tmp_path, monkeypatch, frame):
    monkeypatch.setattr(gr, "START", "2026-09-01")
    monkeypatch.setattr(gr, "notify_telegram", lambda *_a, **_k: None)
    conn = sqlite3.connect(tmp_path / "live.db")
    gr.ensure_schema(conn)
    feed = Feed(frame)

    def history(start: date, end: date):
        past = frame[frame.ts.dt.date < end]                            # what the paper loop cached before today
        return {SIGNAL: past.reset_index(drop=True)}, {SIGNAL: "2026-10-05"}

    def drive(runner, start: str, end: str):
        t, stop = datetime.fromisoformat(start), datetime.fromisoformat(end)
        while t <= stop:
            feed.now = t
            runner.step(t, conn)
            t += timedelta(seconds=15)

    yield conn, feed, history, drive
    conn.close()


def test_phase_0_has_no_broker_path():
    assert gr.DRY_RUN is True
    for module in (gr, loop):
        src = inspect.getsource(module)
        for word in ("place_order", "placeOrder", "exit_all", "SmartApi", "live.brokers", "send_order"):
            assert word not in src


def test_dry_runner_takes_the_back_tests_signal_and_its_stop(env):
    conn, feed, history, drive = env
    runner = gr.Runner(feed=feed, series=cr.Series(feed, history=history))
    # the back test: signal 10:14 on 18 Sep, entry 10:15 at 153250, stopped out in the 10:29 bar
    drive(runner, "2026-09-18T10:12:05", "2026-09-18T10:30:00")
    t = conn.execute("SELECT direction, symbol, signal_symbol, qty, signal_ts, entry_ts, exit_ts, entry_price, exit_price, "
                     "stop_price, target_price, stop_dist, exit_reason, gross_rs, charges_rs, net_rs FROM live_gold_trades").fetchall()
    assert len(t) == 1
    (direction, symbol, signal_symbol, qty, signal_ts, entry_ts, exit_ts, entry, exit_, stop, target, dist, reason,
     gross, cost, net) = t[0]
    assert (direction, symbol, signal_symbol, qty) == ("short", TRADED, SIGNAL, 10)
    assert signal_ts[:16] == "2026-09-18 10:14" and entry_ts[:16] == "2026-09-18T10:15" and exit_ts[:16] == "2026-09-18T10:29"
    assert entry == feed.frame.loc[pd.Timestamp("2026-09-18 10:15")].open           # decided 5 s in: that price
    assert dist == pytest.approx(entry * 0.0025) and stop == 153634.0 and target == 152100.0   # on the tick, outward
    assert reason == "stop" and exit_ >= stop and gross == pytest.approx(-(exit_ - entry) * 10)
    assert net == pytest.approx(gross - cost, abs=0.01) and 200 < cost < 400         # one GOLDM lot round trip
    orders = conn.execute("SELECT kind, side, delay_s, signal_bar_close, status, dry_run FROM live_gold_orders ORDER BY id").fetchall()
    assert [(o[0], o[1]) for o in orders] == [("entry", "SELL"), ("exit", "BUY")]
    assert orders[0][2] == 5.0 and orders[0][3] == feed.frame.loc[pd.Timestamp("2026-09-18 10:14")].close
    assert all(o[4] == "DRY_FILLED" and o[5] == 1 for o in orders)
    assert conn.execute("SELECT outcome FROM live_gold_decisions").fetchall() == [("taken",)]
    assert gr.load_position(conn) is None


def _held(conn, entry=150000.0):
    pos = {"ref": "held", "side": -1, "symbol": TRADED, "signal_symbol": SIGNAL, "qty": 10, "signal_ts": "x",
           "entry_ts": "2026-09-18T12:00:05", "minute": "2026-09-18T12:00:00", "entry_price": entry, "stop_dist": 375.0,
           "stop": 150375.0, "stop0": 150375.0, "target": 148875.0, "best": entry, "moved": False}
    conn.execute("INSERT INTO live_gold_trades (book, trade_ref, trade_date, direction, symbol, signal_symbol, qty, signal_ts, "
                 "entry_ts, entry_price, stop_price, target_price, stop_dist, dry_run) VALUES "
                 "('dry','held','2026-09-18','short',?,?,10,'x','2026-09-18T12:00:05',?,150375,148875,375,1)", (TRADED, SIGNAL, entry))
    gr.save_position(conn, pos)


def test_stop_moves_to_entry_when_the_minute_rolls_after_one_stop_of_gain(env):
    conn, feed, history, drive = env
    runner = gr.Runner(feed=feed, series=cr.Series(feed, history=history))
    _held(conn)
    at = lambda s: datetime.fromisoformat("2026-09-18T" + s)          # noqa: E731
    feed.fixed = 149620.0                                             # 380 in favour, inside the entry minute
    pos = runner._manage(gr.load_position(conn), at("12:00:30"), conn)
    assert pos["best"] == 149620.0 and pos["moved"] is False and pos["stop"] == 150375.0
    feed.fixed = 150100.0                                             # same minute, back above the entry: still open
    assert runner._manage(gr.load_position(conn), at("12:00:50"), conn)["moved"] is False
    feed.fixed = 149900.0                                             # the minute rolls: the stop goes to the entry
    pos = runner._manage(gr.load_position(conn), at("12:01:02"), conn)
    assert pos["moved"] is True and pos["stop"] == 150000.0
    assert conn.execute("SELECT stop_moved_at FROM live_gold_trades WHERE trade_ref='held'").fetchone()[0][:16] == "2026-09-18T12:01"
    feed.fixed = 150010.0                                             # back through the entry: out, flagged as such
    assert runner._manage(gr.load_position(conn), at("12:01:20"), conn) is None
    assert conn.execute("SELECT exit_reason, exit_price FROM live_gold_trades WHERE trade_ref='held'").fetchone() == ("stop at entry", 150010.0)


def test_target_session_close_and_a_position_left_from_an_earlier_session(env):
    conn, feed, history, drive = env
    runner = gr.Runner(feed=feed, series=cr.Series(feed, history=history))
    _held(conn)
    feed.fixed = 148870.0
    assert runner._manage(gr.load_position(conn), datetime.fromisoformat("2026-09-18T12:05:10"), conn) is None
    assert conn.execute("SELECT exit_reason, gross_rs FROM live_gold_trades WHERE trade_ref='held'").fetchone() == ("target", 11300.0)
    conn.execute("DELETE FROM live_gold_trades")
    _held(conn)
    feed.fixed = 150100.0
    assert runner._manage(gr.load_position(conn), datetime.fromisoformat("2026-09-18T23:29:01"), conn) is None
    assert conn.execute("SELECT exit_reason FROM live_gold_trades WHERE trade_ref='held'").fetchone()[0] == "eod"
    conn.execute("DELETE FROM live_gold_trades")
    _held(conn)                                                        # entered on the 18th, runner back on the 21st
    feed.fixed = None
    feed.now = datetime(2026, 9, 21, 9, 30, 5)
    assert runner.step(feed.now, conn) is None
    assert conn.execute("SELECT exit_reason FROM live_gold_trades WHERE trade_ref='held'").fetchone()[0] == "eod_missed"


def test_both_runners_share_one_loop_and_one_failing_does_not_stop_the_other(tmp_path, monkeypatch):
    from storage import live_db
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    seen = []

    class Good:
        def step(self, now, conn):
            seen.append(now)

    class Bad:
        def step(self, now, conn):
            raise RuntimeError("feed down")

    loop.run(max_cycles=1, clock=lambda: datetime(2026, 10, 10, 12, 0), runners=[("bad", Bad()), ("good", Good())])
    assert seen == [datetime(2026, 10, 10, 12, 0)]
