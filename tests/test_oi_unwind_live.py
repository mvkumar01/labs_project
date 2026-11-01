"""OI Unwind on the live runner: one rule with the paper book, levels, the expiry and ATM strike, DRY entry and exits."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from labs.engine import oi_unwind_tracker as U
from labs.ui import live_routes as routes
from live import live_executor as ex
from live import live_runner as lr
from live import live_service as svc
from live import proposer_runner as pr
from live.engine import oi_unwind_engine as oi
from live.engine import proposer_engine as pe
from storage import live_db
from storage.live_db import init_live_db

USER, CONN = "u_oi", "c_oi"
OPEN = datetime(2026, 11, 3, 9, 15)
CPR = U.cpr_levels(25600.0, 25400.0, 25500.0)
NIFTY_V, SENSEX_V = pe.STRATEGY_VERSION_OI_UNWIND_NIFTY, pe.STRATEGY_VERSION_OI_UNWIND_SENSEX


def _marks(res=25500.0, unwinding=True, sup=(25300.0, False), n=40):
    return [(OPEN + timedelta(minutes=5 * i), sup, (res, unwinding)) for i in range(n)]


def _closes(values):
    return [(OPEN + timedelta(minutes=i), c) for i, c in enumerate(values)]


def _walls(marks=None, book=U.NIFTY, cpr=CPR):
    w = oi.WallFeed(book, background=False)
    w.cpr, w.marks = cpr, _marks() if marks is None else marks
    return w


LABEL = OPEN + timedelta(minutes=14)
BREAK = [25490.0] * 10 + [25505.0] * 5                     # the 09:25 candle closes above 25500


# ----------------------------------------------------------------- wiring ---
def test_versions_belong_to_this_runner_and_trade_their_own_index():
    assert NIFTY_V in pe.KNOWN_VERSIONS and SENSEX_V in pe.KNOWN_VERSIONS
    assert ex.is_proposer_strategy(NIFTY_V) and ex.is_proposer_strategy(SENSEX_V)
    assert pr.instrument_for(NIFTY_V) == pr.NIFTY_I and pr.instrument_for(SENSEX_V) == pr.SENSEX_I
    assert oi.book_for(NIFTY_V) is U.NIFTY and oi.book_for(SENSEX_V) is U.SENSEX and oi.book_for("v2.11") is None
    assert pr.NIFTY_I.lot_size == U.NIFTY.lot_size and pr.SENSEX_I.lot_size == U.SENSEX.lot_size
    assert NIFTY_V not in pe.NIFTY_SCALPER_VERSIONS            # no opening ratio, no scaled exits
    assert NIFTY_V not in pe.DAY_TARGET_VERSIONS and SENSEX_V not in pe.DAY_TARGET_VERSIONS


def test_presets_are_selectable_proposer_presets():
    assert routes.STRATEGY_PRESETS["oi_unwind_nifty"] == ("proposer", NIFTY_V)
    assert routes.STRATEGY_PRESETS["oi_unwind_sensex"] == ("proposer", SENSEX_V)
    assert "oi_unwind_nifty" in routes.STRATEGY_LABELS and "oi_unwind_sensex" in routes.STRATEGY_LABELS


# ------------------------------------------------------------ one rule ---
def test_live_signal_matches_the_paper_entry():
    closes = _closes(BREAK)
    bars = [(m, c, c + 1, c - 1, c) for m, c in closes]
    paper = U.simulate(bars, _marks(), CPR, U.NIFTY, session_done=False)
    signal, why = oi.decide(LABEL, closes, _walls())
    assert why == "ok" and len(paper) == 1
    assert (signal["side"], signal["bar"], signal["wall"], signal["target"], signal["stop"]) == (
        paper[0]["side"], paper[0]["entry_t"], paper[0]["wall"], paper[0]["target"], paper[0]["stop"])


def test_every_candle_of_a_session_decides_as_the_paper_book():
    closes = _closes(([25490.0] * 10 + [25505.0] * 5 + [25490.0] * 5 + [25506.0] * 5 + [25480.0] * 5) * 2)
    bars = [(m, c, c, c, c) for m, c in closes]
    marks = _marks(sup=(25485.0, True))
    w = _walls(marks)
    live = []
    for n in range(len(closes)):
        if closes[n][0].minute % 5 == 4:
            sig, _ = oi.decide(closes[n][0], closes[:n + 1], w)
            if sig:
                live.append((sig["side"], sig["bar"]))
    # paper hides a side's signals while its own trade is open, so it is a subset of the live signals
    paper = [(t["side"], t["entry_t"]) for t in U.simulate(bars, marks, CPR, U.NIFTY, session_done=False)]
    assert live and set(paper) <= set(live)


def test_a_put_mirrors_the_call():
    closes = _closes([25310.0] * 10 + [25295.0] * 5)
    signal, why = oi.decide(LABEL, closes, _walls(_marks(sup=(25300.0, True))))
    assert why == "ok" and signal["side"] == "PUT" and signal["stop"] == 25295.0 + 30.0
    assert signal["target"] < 25300.0 - 10.0


def test_no_signal_when_the_wall_is_not_unwinding():
    assert oi.decide(LABEL, _closes(BREAK), _walls(_marks(unwinding=False))) == (None, "ok")


def test_the_sixty_second_rule_reads_the_older_mark():
    """At the 09:24 label the 09:20 mark decides, not one stamped later."""
    marks = _marks(unwinding=False, n=2) + [(OPEN + timedelta(minutes=10), (25300.0, False), (25500.0, True))]
    assert oi.decide(LABEL - timedelta(minutes=5), _closes(BREAK[:10]), _walls(marks)) == (None, "ok")
    late = [(OPEN + timedelta(minutes=9), (25300.0, False), (25500.0, True))]
    closes = _closes([25490.0] * 5 + [25505.0] * 5)
    assert oi.decide(LABEL - timedelta(minutes=5), closes, _walls(late))[0] is None


def test_no_decision_on_missing_or_old_data():
    assert oi.decide(LABEL, _closes(BREAK), _walls(cpr=None))[1] == "cpr_unknown"
    assert oi.decide(LABEL, _closes(BREAK[:-1]), _walls())[1] == "bar_missing"
    assert oi.decide(LABEL, _closes(BREAK), _walls([]))[1] == "walls_stale"
    old = [(OPEN - timedelta(minutes=5), (25300.0, False), (25500.0, True))]
    assert oi.decide(LABEL, _closes(BREAK), _walls(old))[1] == "walls_stale"
    one_missed = [(OPEN + timedelta(minutes=5), (25300.0, False), (25500.0, True))]
    assert oi.decide(LABEL, _closes(BREAK), _walls(one_missed))[1] == "ok"


def test_candle_label_only_inside_the_window():
    assert oi.candle_label(datetime(2026, 11, 3, 9, 25, 2)) == datetime(2026, 11, 3, 9, 24)
    assert oi.candle_label(datetime(2026, 11, 3, 9, 25, 30)) is None
    assert oi.candle_label(datetime(2026, 11, 3, 9, 26, 2)) is None


def test_latest_marks_equal_the_full_day():
    rows = []
    for i in range(6):
        ts = (OPEN + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")
        rows += [(ts, 25500, "CE", "26N04", 500000 - 60000 * i, 25480), (ts, 25550, "CE", "26N04", 90000 + i, 25480),
                 (ts, 25450, "PE", "26N04", 400000 + 1000 * i, 25480), (ts, 25400, "PE", "26N04", 80000, 25480)]
        off = (OPEN + timedelta(minutes=5 * i + 1)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append((off, 25500, "CE", "26N04", 1, 25480))          # off-grid minutes are not marks
    frame = pd.DataFrame(rows, columns=oi.WALL_COLUMNS)
    assert oi.latest_marks(frame, U.NIFTY) == U.wall_marks(frame, U.NIFTY)[-2:]


# ------------------------------------------------------------- wall feed ---
def test_wall_feed_reads_once_a_mark_and_retries_a_late_one(monkeypatch):
    calls = []
    marks = [_marks()[1]]                                       # 09:20

    def read(day, book):
        calls.append(day)
        return marks
    w = oi.WallFeed(U.NIFTY, marks_fn=read, cpr_fn=lambda d, b: CPR, background=False)
    w.RETRY_S = 0.0
    w.step(datetime(2026, 11, 3, 9, 21, 5))
    w.step(datetime(2026, 11, 3, 9, 22, 5))
    assert len(calls) == 1 and w.cpr == CPR
    w.step(datetime(2026, 11, 3, 9, 26, 5))                     # the 09:25 mark is not there yet
    w.step(datetime(2026, 11, 3, 9, 26, 20))
    assert len(calls) == 3
    w.step(datetime(2026, 11, 3, 15, 0, 0))                     # no more entries, no more reads
    assert len(calls) == 3


def test_wall_feed_survives_a_failed_read():
    def read(day, book):
        raise FileNotFoundError(day)
    w = oi.WallFeed(U.NIFTY, marks_fn=read, cpr_fn=lambda d, b: CPR, background=False)
    w.step(datetime(2026, 11, 3, 9, 21, 5))
    assert w.marks == [] and w._busy is False


# ----------------------------------------------------------- levels, option ---
def test_exit_levels_on_both_sides_and_stop_first():
    call, put = {"stop": 100.0, "target": 150.0}, {"stop": 150.0, "target": 100.0}
    assert oi.exit_reason("CALL", 100.0, call) == "oi_stop" and oi.exit_reason("CALL", 150.0, call) == "oi_target"
    assert oi.exit_reason("PUT", 150.0, put) == "oi_stop" and oi.exit_reason("PUT", 100.0, put) == "oi_target"
    assert oi.exit_reason("CALL", 120.0, call) is None and oi.exit_reason("PUT", 120.0, put) is None


def test_levels_belong_to_the_symbol_held():
    raw = oi.dump_levels({"stop": 1.0, "target": 2.0, "wall": 1.5}, "X")
    assert oi.load_levels(raw, "X")["stop"] == 1.0
    assert oi.load_levels(raw, "Y") is None and oi.load_levels(None, "X") is None and oi.load_levels("{", "X") is None


def test_next_expiry_is_strictly_after_today(monkeypatch):
    dates = {"A": date(2026, 11, 3), "B": date(2026, 11, 10), "C": date(2026, 11, 17)}
    monkeypatch.setattr(oi, "expiry_date_of", lambda code, symbol: dates.get(code))
    assert oi.next_expiry_code(["C", "A", "B"], date(2026, 11, 3), "NIFTY") == "B"
    assert oi.next_expiry_code(["A"], date(2026, 11, 3), "NIFTY") is None


def test_atm_strike_needs_a_traded_price():
    chain = pd.DataFrame([{"tradingsymbol": "N25500CE", "strike": 25500, "option_type": "CE", "ltp": 100.0},
                          {"tradingsymbol": "N25500PE", "strike": 25500, "option_type": "PE", "ltp": 0.0}])
    assert oi.atm_symbol(chain, "CALL", 25512.0, U.NIFTY) == ("N25500CE", 25500, "CE")
    assert oi.atm_symbol(chain, "PUT", 25512.0, U.NIFTY) == (None, 25500, "PE")
    assert oi.atm_symbol(pd.DataFrame(), "CALL", 25512.0, U.NIFTY)[0] is None
    assert oi.atm_symbol(chain, "CALL", 74149.0, U.SENSEX)[1] == 74100


# ---------------------------------------------------------------- runner ---
@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://static.test:1234")
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    init_live_db()
    pr.ensure_schema()
    pr.ensure_regime_schema()
    svc.upsert_connection(USER, CONN, broker="zerodha", account_label="T", account_ref="zerodha:T",
                          status="connected")
    for k, v in (("lots", "1"), ("daily_loss_cap", "50000"), ("decision_engine", "proposer"),
                 ("strategy_version", SENSEX_V), ("kill_switch", "0")):
        svc.set_config(USER, CONN, k, v)
    ex.set_mode(USER, CONN, ex.Mode.DRY_RUN)
    monkeypatch.setattr(pr, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(lr, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(lr, "market_session_available", lambda _now: True)
    return tmp_path


class Market:
    def __init__(self):
        self.px, self.sp = 400.0, 74210.0
        self.quotes = []

    def spot(self):
        return self.sp

    def quote(self, symbol):
        self.quotes.append(symbol)
        return {"ltp": self.px, "bid": self.px - 0.5, "ask": self.px + 0.5}


class Feed:
    """No prints at all: OI Unwind must trade without the predictor."""

    def snapshot(self):
        return None


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


SENSEX_CPR = U.cpr_levels(74500.0, 73900.0, 74100.0)


def _ctx(now, closes, res=74200.0, sup=(73800.0, False), cpr=SENSEX_CPR):
    w = oi.WallFeed(U.SENSEX, marks_fn=lambda d, b: w.marks, cpr_fn=lambda d, b: cpr, background=False)
    w.day, w.cpr = now.date().isoformat(), cpr
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    w.marks = [(start + timedelta(minutes=5 * i), sup, (res, True)) for i in range(60)]
    bars = pr.SessionBars(bars_fn=lambda d: [(start + timedelta(minutes=i), c) for i, c in enumerate(closes)])
    return {"task_id": "prop-task", "adapters": {}, "reconciled": set(), "conns": {},
            "oi_walls": {U.SENSEX.key: w}, "oi_bars": {U.SENSEX.key: bars}}


def _today(hour, minute, second):
    return datetime.now(pr.IST).replace(tzinfo=None, hour=hour, minute=minute, second=second, microsecond=0)


def test_dry_entry_then_target_exit_without_any_print(db, monkeypatch):
    chain = pd.DataFrame([{"timestamp": "x", "tradingsymbol": "SENSEX26N0674200CE", "strike": 74200,
                           "option_type": "CE", "expiry": "26N06", "ltp": 400.0, "oi": 1, "spot": 74210}])
    seen = {}

    def fake_chain(day, profile=pr.SENSEX_I, next_expiry=False):
        seen.update(profile=profile, next_expiry=next_expiry)
        return chain
    monkeypatch.setattr(pr, "option_chain", fake_chain)
    market, now = Market(), _today(9, 25, 2)
    ctx = _ctx(now, [74190.0] * 5 + [74190.0] * 5)              # the bars up to 09:19 plus 09:20-09:24
    ctx["oi_bars"][U.SENSEX.key].bars_fn = lambda d: [(now.replace(minute=15 + i, second=0), 74190.0 if i < 9 else 74210.0)
                                                      for i in range(10)]

    def cycle(at):
        return pr.process_connection(USER, CONN, ctx=ctx, feed=Feed(), market=market, now=at,
                                     adapter_factory=Adapter)

    assert cycle(now) is True
    st = svc.get_trade_state(USER, CONN)
    assert st["position"] == "OPEN" and st["side"] == "CALL" and st["symbol"] == "SENSEX26N0674200CE"
    assert st["qty"] == 20 and st["entry_rule"] == oi.ENTRY_REASON
    assert seen == {"profile": pr.SENSEX_I, "next_expiry": True}
    levels = oi.load_levels(svc.get_config(USER, CONN, oi.LEVELS_KEY), st["symbol"])
    assert levels["stop"] == pytest.approx(74210.0 - 97.2) and levels["target"] > 74210.0

    assert cycle(now.replace(second=6)) is True                  # same candle, nothing new
    market.sp = levels["target"] + 1
    market.px = 500.0
    assert cycle(now.replace(minute=40, second=1)) is False
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    with live_db.get_live_conn() as c:
        row = c.execute("SELECT reason, gross_pnl, strategy FROM live_trades").fetchone()
    assert row["reason"] == "oi_target" and row["gross_pnl"] > 0 and row["strategy"] == SENSEX_V


def test_a_stop_exits_and_a_signal_is_skipped_while_a_trade_is_open(db, monkeypatch):
    called = []
    monkeypatch.setattr(pr, "option_chain", lambda *a, **k: called.append(a) or pd.DataFrame())
    now = _today(10, 0, 2)
    st = svc.get_trade_state(USER, CONN)
    st.update({"position": "OPEN", "side": "PUT", "symbol": "SENSEX26N0674200PE", "entry_price": 300.0, "qty": 20,
               "entry_time": datetime.now(pr.timezone.utc).isoformat(), "virtual": 1, "entry_rule": oi.ENTRY_REASON,
               "entry_spot": 74210.0})
    svc.save_trade_state(USER, CONN, st)
    svc.set_config(USER, CONN, oi.LEVELS_KEY, oi.dump_levels({"stop": 74307.2, "target": 74000.0, "wall": 74200.0},
                                                             st["symbol"]))
    market = Market()
    closes = [74190.0] * 40 + [74210.0] * 5                     # 09:55 candle breaks the wall: a CALL signal
    ctx = _ctx(now, closes)

    def cycle(at):
        return pr.process_connection(USER, CONN, ctx=ctx, feed=Feed(), market=market, now=at,
                                     adapter_factory=Adapter)

    assert cycle(now) is True and called == []                   # skipped, not reversed
    assert svc.get_trade_state(USER, CONN)["side"] == "PUT"
    market.sp = 74307.2
    assert cycle(now.replace(second=9)) is False
    with live_db.get_live_conn() as c:
        assert c.execute("SELECT reason FROM live_trades").fetchone()["reason"] == "oi_stop"


def test_a_position_without_levels_is_closed(db):
    now = _today(10, 7, 2)
    st = svc.get_trade_state(USER, CONN)
    st.update({"position": "OPEN", "side": "CALL", "symbol": "SENSEX26N0674200CE", "entry_price": 300.0, "qty": 20,
               "entry_time": datetime.now(pr.timezone.utc).isoformat(), "virtual": 1, "entry_rule": oi.ENTRY_REASON,
               "entry_spot": 74210.0})
    svc.save_trade_state(USER, CONN, st)
    ctx = _ctx(now, [74190.0] * 50)
    assert pr.process_connection(USER, CONN, ctx=ctx, feed=Feed(), market=Market(), now=now,
                                 adapter_factory=Adapter) is False
    with live_db.get_live_conn() as c:
        assert c.execute("SELECT reason FROM live_trades").fetchone()["reason"] == "oi_levels_missing"


def test_an_old_runner_does_not_trade_an_unknown_version(db, monkeypatch):
    monkeypatch.setattr(pe, "KNOWN_VERSIONS", frozenset())
    now = _today(9, 25, 2)
    ctx = _ctx(now, [])
    ctx["oi_bars"][U.SENSEX.key].bars_fn = lambda d: [(now.replace(minute=15 + i, second=0), 74190.0 if i < 9 else 74210.0)
                                                      for i in range(10)]
    assert pr.process_connection(USER, CONN, ctx=ctx, feed=Feed(), market=Market(), now=now,
                                 adapter_factory=Adapter) is False
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
