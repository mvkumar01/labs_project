"""SENSEX Proposer price-action variant: Renko bar exit, one loss per day, paper book."""
from __future__ import annotations

import py_compile
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from live import live_executor as ex
from live import live_runner as lr
from live import live_service as svc
from live import proposer_runner as pr
from live.engine import proposer_bar_exit as bx
from live.engine import proposer_engine as pe
import storage.live_db as live_db
from storage.live_db import init_live_db

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 6, 9, 22)


def _p(x5="strong_bull", conf=0.6, regime="neutral", asof=T0):
    return {"regime": regime, "x5": x5, "x5_conf": conf, "x5_asof": asof.isoformat(), "drift_state": "flat"}


# ═══════════════════════════════════════════════════════════════ detectors ══
def test_renko_needs_one_brick_to_continue_and_two_to_reverse():
    closes = [100, 149, 150, 201, 151, 101, 100, 99]
    bricks = bx.renko_bricks(closes, 50)
    # 150 -> up brick; 201 -> second up brick (level 200); a reversal needs 200 - 100 = 100
    assert bricks == [(2, 1), (3, 1), (6, -1)]


def test_renko_exit_fires_only_on_an_adverse_brick_formed_after_entry():
    # 10 Aug 2026: last brick before the entry was DOWN at 78,334; a put was bought in the 10:13 bar.
    closes = [78384, 78334, 78397, 78426, 78438]
    assert bx.renko_bricks(closes[:2], 50) == [(1, -1)]
    assert not bx.fires("renko-50", closes[:4], 3, "PE")          # 78,426 < 78,434: no up brick yet
    assert bx.fires("renko-50", closes, 3, "PE")                  # 78,438 prints the up brick
    assert not bx.fires("renko-50", closes, 3, "CE")              # an up brick is with a call
    assert not bx.fires("renko-50", closes, 5, "PE")              # entry bar not complete yet
    assert not bx.fires("renko-50-2", closes, 3, "PE")            # needs two adverse bricks


def test_consecutive_adverse_closes():
    closes = [100, 101, 100, 99, 98, 97]
    assert bx.fires("consec-4", closes, 2, "CE")
    assert not bx.fires("consec-4", closes, 3, "CE")              # only three post-entry bars
    assert not bx.fires("consec-4", closes, 2, "PE")
    assert not bx.fires("", closes, 2, "CE")
    with pytest.raises(ValueError):
        bx.fires("ichimoku-9", closes, 2, "CE")


# ══════════════════════════════════════════════════════════════════ engine ══
def test_variant_parameters_and_the_default_is_unchanged():
    assert pe.params_for("proposer_dt25") == pe.ProposerParams()
    assert pe.ProposerParams().bar_exit == "" and pe.ProposerParams().max_losses_per_day == 0
    px = pe.params_for(pe.STRATEGY_VERSION_PX)
    assert px.bar_exit == "renko-50" and px.max_losses_per_day == 1
    assert px.loss_floor_pct == pe.ProposerParams().loss_floor_pct
    assert ex.is_proposer_strategy(pe.STRATEGY_VERSION_PX)


def test_engine_exits_on_the_bar_signal_and_stops_after_a_loss():
    e = pe.ProposerEngine(pe.params_for(pe.STRATEGY_VERSION_PX))
    assert e.evaluate(T0, _p(), pe.Position(), option_ltp=None, spot=74000).action == "ENTER"
    e.mark_entry(_p())
    pos = pe.Position("CE", 400.0, 74000.0, 20)
    held = e.evaluate(T0 + timedelta(seconds=30), _p(), pos, option_ltp=398.0, spot=73990,
                      closes=[74010, 74000, 73990], entry_idx=1)
    assert held.action == "HOLD"
    out = e.evaluate(T0 + timedelta(minutes=3), _p(), pos, option_ltp=380.0, spot=73940,
                     closes=[74060, 74010, 73960, 73900], entry_idx=1)
    assert (out.action, out.reason) == ("EXIT", "bar_exit")
    # the base engine holds the same bars
    base = pe.ProposerEngine()
    base.evaluate(T0, _p(), pe.Position(), option_ltp=None, spot=74000)
    base.mark_entry(_p())
    assert base.evaluate(T0 + timedelta(minutes=3), _p(), pos, option_ltp=380.0, spot=73940,
                         closes=[74060, 74010, 73960, 73900], entry_idx=1).action == "HOLD"
    # flat again, a fresh gated print after the cooldown - but one trade has lost today
    e.evaluate(T0 + timedelta(minutes=3), _p(), pe.Position(), option_ltp=None, spot=73940)
    later = T0 + timedelta(minutes=20)
    e.set_book(day_realized=-400.0, book_net=-400.0, day_losses=1)
    blocked = e.evaluate(later, _p(asof=later), pe.Position(), option_ltp=None, spot=73900)
    assert (blocked.action, blocked.reason) == ("HOLD", "day_loss_limit")
    e.set_book(day_realized=-400.0, book_net=-400.0, day_losses=0)
    again = later + timedelta(minutes=5)
    assert e.evaluate(again, _p(asof=again), pe.Position(), option_ltp=None, spot=73900).action == "ENTER"


# ══════════════════════════════════════════════════════════════════ runner ══
def test_session_bars_add_polled_minutes_the_collector_has_not_written():
    day = datetime(2026, 10, 6)
    collector = [(day.replace(hour=9, minute=15), 100.0), (day.replace(hour=9, minute=16), 101.0)]
    sb = pr.SessionBars(bars_fn=lambda d: list(collector))
    sb.note(day.replace(hour=9, minute=16, second=50), 101.2)     # collector already has 09:16
    sb.note(day.replace(hour=9, minute=17, second=58), 102.5)
    sb.note(day.replace(hour=9, minute=18, second=3), 103.0)      # the minute still forming
    now = day.replace(hour=9, minute=18, second=4)
    assert sb.completed(now) == collector + [(day.replace(hour=9, minute=17), 102.5)]
    assert pr.entry_bar_index(sb.completed(now), "2026-10-06T03:47:20+00:00") == 2      # 09:17 IST
    assert pr.entry_bar_index(sb.completed(now), "2026-10-06T03:48:01+00:00") == 3      # still forming
    assert pr.entry_bar_index(sb.completed(now), None) is None


USER, CONN = "user-1", "user-1:zerodha"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://static.test:1234")
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    init_live_db()
    pr.ensure_schema()
    pr.ensure_regime_schema()
    svc.upsert_connection(USER, CONN, broker="zerodha", account_label="T", account_ref="zerodha:T",
                          status="connected")
    for k, v in (("lots", "1"), ("daily_loss_cap", "50000"), ("decision_engine", "proposer"),
                 ("strategy_version", pe.STRATEGY_VERSION_PX), ("kill_switch", "0")):
        svc.set_config(USER, CONN, k, v)
    monkeypatch.setattr(pr, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(lr, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(lr, "market_session_available", lambda _now: True)
    return tmp_path


class Market:
    def __init__(self):
        self.px, self.sp = 400.0, 74226.0

    def spot(self):
        return self.sp

    def quote(self, symbol):
        return {"ltp": self.px, "bid": self.px - 0.5, "ask": self.px + 0.5}


class Feed:
    def __init__(self, snap):
        self.snap = snap

    def snapshot(self):
        return dict(self.snap)


CHAIN = pd.DataFrame([{"timestamp": "x", "tradingsymbol": "SENSEX26O0874000CE", "strike": 74000,
                       "option_type": "CE", "expiry": "26O08", "oi": 1, "spot": 74226}])


def test_dry_run_exits_on_the_renko_brick_and_takes_no_second_trade(db, monkeypatch):
    ex.set_mode(USER, CONN, ex.Mode.DRY_RUN)
    monkeypatch.setattr(pr, "option_chain", lambda day: CHAIN)
    now = datetime.now(pr.IST).replace(tzinfo=None, hour=10, minute=0, second=10, microsecond=0)
    feed, market = Feed(_p("strong_bull", 0.6, "neutral", now.replace(second=0))), Market()
    bars = [(now.replace(minute=m, second=0), 74226.0) for m in range(55, 60)]       # 09:55..09:59 flat
    bars = [(t - timedelta(hours=1), c) for t, c in bars]
    box = {"bars": list(bars)}
    ctx = {"task_id": "prop-task", "adapters": {}, "reconciled": set(), "conns": {},
           "bars": pr.SessionBars(bars_fn=lambda d: list(box["bars"]))}

    class Adapter:
        def __init__(self, **_kw):
            pass

        def connect(self):
            pass

        def is_connected(self):
            return True

        def account_ref(self):
            return "zerodha:T"

        def use_segment(self, *_a):
            pass

        def broker_symbol(self, s):
            return s

    def cycle(at):
        return pr.process_connection(USER, CONN, ctx=ctx, feed=feed, market=market, now=at,
                                     adapter_factory=Adapter)

    assert cycle(now) is True
    st = svc.get_trade_state(USER, CONN)
    assert st["position"] == "OPEN" and st["side"] == "CALL"
    assert ctx["conns"][CONN].engine.params.bar_exit == "renko-50"
    with live_db.get_live_conn() as c:      # the runner stamps the wall clock: pin the entry to this test's minute
        c.execute("UPDATE live_trade_state SET entry_time=? WHERE conn_id=?",
                  (now.replace(tzinfo=pr.IST).astimezone(pr.timezone.utc).isoformat(), CONN))
    # two minutes later SENSEX has closed 120 points lower: a down brick (2 x 50 from the flat level)
    monkeypatch.setattr(pr.SessionBars, "REFRESH_S", 0.0)
    box["bars"] += [(now.replace(second=0), 74170.0), (now.replace(minute=1, second=0), 74100.0)]
    market.px, market.sp = 385.0, 74100.0
    assert cycle(now.replace(minute=2, second=5)) is False
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    with live_db.get_live_conn() as c:
        row = c.execute("SELECT reason, gross_pnl, strategy FROM live_trades").fetchone()
    assert row["reason"] == "bar_exit" and row["gross_pnl"] < 0 and row["strategy"] == pe.STRATEGY_VERSION_PX
    # a fresh strong print well after the cooldown: the day already has its loss
    later = now.replace(minute=20, second=10)
    feed.snap = _p("strong_bull", 0.7, "neutral", later.replace(second=0))
    assert cycle(later) is False
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"


def test_switching_the_preset_rebuilds_the_engine(db, monkeypatch):
    ex.set_mode(USER, CONN, ex.Mode.DRY_RUN)
    monkeypatch.setattr(pr, "option_chain", lambda day: pd.DataFrame())
    now = datetime.now(pr.IST).replace(tzinfo=None, hour=10, minute=0, second=10, microsecond=0)
    ctx = {"task_id": "t", "adapters": {}, "reconciled": set(), "conns": {}}

    class Adapter:
        def __init__(self, **_kw):
            pass

        def connect(self):
            pass

        def is_connected(self):
            return True

        def account_ref(self):
            return "zerodha:T"

    args = dict(ctx=ctx, feed=Feed({}), market=Market(), now=now, adapter_factory=Adapter)
    pr.process_connection(USER, CONN, **args)
    assert ctx["conns"][CONN].engine.params.max_losses_per_day == 1
    svc.set_config(USER, CONN, "strategy_version", "proposer_dt25")
    pr.process_connection(USER, CONN, **args)
    assert ctx["conns"][CONN].engine.params == pe.ProposerParams()


# ══════════════════════════════════════════════════════════════ paper book ══
def _synthetic_day(day: str):
    """A session that drifts up 12 points a minute from 09:30 with a 1-point wobble."""
    start = datetime.fromisoformat(day + "T09:15:00")
    spot, bars, rows = 74000.0, [], []
    for i in range(376):
        t = start + timedelta(minutes=i)
        o = spot
        spot += (12.0 if 15 <= i < 120 else 0.0) + (1.0 if i % 2 else -1.0)
        bars.append((t, o, max(o, spot) + 0.5, min(o, spot) - 0.5, spot))
        for strike in range(73000, 76100, 100):
            for typ in ("CE", "PE"):
                intrinsic = max(o - strike, 0) if typ == "CE" else max(strike - o, 0)
                ltp = intrinsic + 250.0
                rows.append({"timestamp": t, "underlying": "SENSEX", "strike": strike, "option_type": typ,
                             "tradingsymbol": f"SENSEX26O01{strike}{typ}", "expiry": "26O01", "ltp": ltp,
                             "bid": ltp - 0.5, "ask": ltp + 0.5, "oi": 1000, "volume": 10, "spot": o})
    return bars, pd.DataFrame(rows)


@pytest.fixture
def paper(tmp_path, monkeypatch):
    import sqlite3
    from labs.engine import proposer_px_tracker as tr
    data = {d: _synthetic_day(d) for d in ("2026-09-29", "2026-09-30")}
    monkeypatch.setattr(tr, "spot_ohlc", lambda day: data[day][0] if day in data else [])

    def frame(symbol, day, **_kw):
        if day not in data:
            raise FileNotFoundError(day)
        return data[day][1]

    monkeypatch.setattr(tr, "load_options_frame", frame)
    conn = sqlite3.connect(tmp_path / "labs.db")
    yield tr, conn
    conn.close()


def test_paper_book_replays_a_session_with_the_live_engine(paper):
    tr, conn = paper
    r = tr.simulate_day("2026-09-29")
    assert r["status"] == "closed" and r["n_trades"] >= 1
    first = r["trades"][0]
    assert first["side"] == "CE" and first["exit_ts"] and first["gross_rs"] > 0   # a rising tape: a call that wins
    assert first["exit_rule"] in ("daily_target", "spot_target")
    assert r["gross_rs"] == pytest.approx(sum(t["gross_rs"] for t in r["trades"]), abs=0.05)
    assert r["net_rs"] == pytest.approx(r["gross_rs"] - r["charges_rs"], abs=0.05)
    # idempotent, and persisted rows match
    out = tr.run_day("2026-09-29", connection=conn)
    again = tr.run_day("2026-09-29", connection=conn)
    assert out == again and out["gross_rs"] == r["gross_rs"]
    assert conn.execute("SELECT COUNT(*) FROM proposer_px_trades").fetchone()[0] == r["n_trades"]
    assert conn.execute("SELECT status, bar_exit, strategy_version FROM proposer_px_daily").fetchone() == (
        "closed", "renko-50", "proposer_dt25_px")


def test_paper_book_carries_the_book_and_replays_only_completed_minutes(paper):
    tr, conn = paper
    day1 = tr.run_day("2026-09-29", connection=conn)
    tr.run_day("2026-09-30", connection=conn)
    before = conn.execute("SELECT book_gross_before FROM proposer_px_daily WHERE trade_date='2026-09-30'").fetchone()[0]
    assert before == pytest.approx(day1["gross_rs"], abs=0.05)
    # mid-session: nothing after the last completed minute is used, and the day stays 'live'
    now = datetime(2026, 9, 30, 9, 40, 30, tzinfo=tr.IST)
    live = tr.simulate_day("2026-09-30", book_before=0.0, now=now)
    assert live["status"] == "live" and live["through_ts"] == "2026-09-30T09:39"
    assert all((t["exit_ts"] or t["entry_ts"]) <= "2026-09-30T09:40:00" for t in live["trades"])
    with pytest.raises(tr.ProposerPxInputError):
        tr.simulate_day("2026-10-01")                                          # no data: never invented
    with pytest.raises(tr.ProposerPxInputError):
        tr.simulate_day("2026-10-03")                                          # a Saturday


def test_dashboard_stats_and_backfill_order(paper, monkeypatch):
    tr, conn = paper
    from labs.engine import proposer_px_backfill as bf
    from labs.engine import proposer_px_view as view
    monkeypatch.setattr(bf, "sessions_with_quotes", lambda s, e: ["2026-09-29", "2026-09-30", "2026-10-01"])

    class Shared:
        """get_conn() hands back the test connection; the backfill's close() must not end it."""
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    monkeypatch.setattr(bf, "get_conn", lambda: Shared())
    monkeypatch.setattr(tr, "get_conn", lambda: Shared())
    out = bf.run_backfill(start_date="2026-09-29", end_date="2026-10-01", limit=5)
    assert [d["date"] for d in out["done"]] == ["2026-09-29", "2026-09-30"]
    assert [d["date"] for d in out["unavailable"]] == ["2026-10-01"] and out["remaining"] == 0
    rows, trades, stats = view.tab_data(conn)
    assert [r["trade_date"] for r in rows] == ["2026-10-01", "2026-09-30", "2026-09-29"]
    assert stats["days"] == 2 and stats["trades"] == len(trades) and stats["lots"] == 100
    assert stats["net_total"] == pytest.approx(sum(r["net_rs"] for r in rows), abs=0.05)
    assert stats["months"][0]["month"] == "2026-09" and stats["months"][0]["days"] == 2
    assert bf.run_backfill(start_date="2026-09-29", end_date="2026-10-01")["done"] == []      # nothing pending
    rebuilt = bf.run_backfill(start_date="2026-09-29", end_date="2026-10-01", rebuild=True, limit=1)
    assert [d["date"] for d in rebuilt["done"]] == ["2026-09-29"] and rebuilt["remaining"] == 2


# ═══════════════════════════════════════════════════════════════ UI wiring ══
def test_ui_patch_applies_to_the_committed_files_and_is_idempotent(tmp_path):
    for rel in ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    script = ROOT / "scripts" / "patch_proposer_px_ui.py"
    first = subprocess.run([sys.executable, str(script), str(tmp_path)], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([sys.executable, str(script), str(tmp_path)], capture_output=True, text=True)
    assert second.returncode == 0 and "0 edits applied" in second.stdout
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"proposer_px": "Sensex Proposer + Renko"') == 1
    assert routes.count('@labs_bp.route("/api/proposer_px/backfill"') == 1
    py_compile.compile(str(tmp_path / "labs/ui/routes.py"), doraise=True)
    py_compile.compile(str(tmp_path / "pa_paper_tracker_loop.py"), doraise=True)
    import jinja2
    jinja2.Environment().parse((tmp_path / "templates/live_strategy.html").read_text(encoding="utf-8"))


def test_paper_book_reads_the_gap_at_the_close_of_the_0915_bar(paper, monkeypatch):
    tr, conn = paper
    bars, _frame = _synthetic_day("2026-09-30")
    prev = _synthetic_day("2026-09-29")[0][-1][4]
    t, _o, _h, _l, c = bars[0]
    bars[0] = (t, prev, max(prev, c), min(prev, c), c)       # opens on the previous close, trades far from it
    monkeypatch.setattr(tr, "spot_ohlc", lambda day: bars if day == "2026-09-30" else _synthetic_day(day)[0])
    r = tr.simulate_day("2026-09-30")
    gap = (c / prev - 1) * 100
    assert gap < -0.3 and r["gap_pct"] == pytest.approx(gap, abs=0.001) and r["regime"] == "bearish"
