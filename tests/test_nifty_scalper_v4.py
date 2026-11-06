"""Nifty Scalper V4: V3 with two entry blocks (live/engine/entry_blocks.py), paper book and live preset. Orders use fakes
or DRY_RUN."""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import jinja2
import pytest

from labs.engine import proposer_px_tracker as tr
from labs.engine.oi_unwind_tracker import cpr_levels
from live import live_executor as ex, live_service as svc, proposer_runner as pr
from live.engine import entry_blocks as eb, proposer_engine as pe
from tests.test_nifty_scalper_v1 import NOW, NiftyMarket, cycle, setup_nifty
from tests.test_proposer_live import USER, CONN, db  # noqa: F401
from tests.test_configure_strategy import client, CONN_ID, USER_ID, CSRF_TOKEN, _set_strategy, _strategy_pair  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
V3, V4 = pe.STRATEGY_VERSION_NIFTY_SCALPER_V3, pe.STRATEGY_VERSION_NIFTY_SCALPER_V4
CPR = {"S3": 22800.0, "S2": 22880.0, "S1": 22940.0, "BC": 22990.0, "P": 23000.0, "TC": 23010.0,
       "R1": 23060.0, "R2": 23120.0, "R3": 23200.0}
PLAIN = (23000.0, 23012.0, 22998.0, 23010.0)            # body 10 of a 14-pt range: no reversal shape


@pytest.mark.parametrize("candle,shape", [
    ((100.0, 110.0, 90.0, 101.0), True),                 # doji: body 1 of 20
    ((100.0, 101.0, 90.0, 101.0), True),                 # hammer: body 1, lower wick 10, no upper wick
    ((101.0, 111.0, 100.0, 100.0), True),                # inverted hammer
    ((100.0, 110.0, 99.0, 109.0), False),                # full body
    ((100.0, 104.0, 99.0, 102.5), False),                # body 2.5 of 5, wicks short
    ((100.0, 100.0, 100.0, 100.0), False),               # no range
])
def test_small_body_shapes(candle, shape):
    assert eb.small_body(*candle) is shape


@pytest.mark.parametrize("side,spot,blocked", [
    ("CALL", 23045.0, True),          # R1 15 above, inclusive
    ("CALL", 23044.06, False),        # 15.94 above: passes (16 Oct 2026's 09:22 PUT case, mirrored)
    ("PUT", 23005.0, True),           # P 5 below (TC above does not count for a PUT)
    ("PUT", 23025.94, False),         # TC 15.94 below
    ("CALL", 23250.0, False),         # no level above
    ("PUT", 23010.0, True),           # spot on TC: the level below is P, 10 away
])
def test_cpr_room_looks_only_the_way_the_trade_travels(side, spot, blocked):
    assert eb.cpr_room_blocked(side, spot, CPR) is blocked


def test_no_cpr_passes_and_the_candle_still_applies():
    assert not eb.cpr_room_blocked("CALL", 23045.0, None)
    t = datetime(2026, 11, 2, 9, 21)
    assert eb.block_reason("CALL", 23045.0, t.replace(minute=22, second=25), None,
                           [(t, 100.0, 110.0, 90.0, 101.0)]) == eb.REASON_CANDLE


def test_the_candle_is_the_last_finished_one_never_the_forming_one():
    t = datetime(2026, 11, 2, 9, 15)
    bars = [(t + timedelta(minutes=i), *PLAIN) for i in range(6)] + [(t.replace(minute=21), 100.0, 110.0, 90.0, 101.0),
                                                                     (t.replace(minute=22), *PLAIN)]
    assert eb.last_finished(bars, t.replace(minute=22, second=25)) == (100.0, 110.0, 90.0, 101.0)   # 09:21
    assert eb.last_finished(bars, t.replace(minute=21, second=59)) == PLAIN                         # 09:20
    assert eb.last_finished(bars, t.replace(minute=15, second=30)) is None
    assert eb.block_reason("CALL", 23250.0, t.replace(minute=22, second=25), CPR, bars) == eb.REASON_CANDLE
    assert eb.block_reason("CALL", 23250.0, t.replace(minute=23, second=25), CPR, bars) is None


def test_previous_cpr_is_the_last_session_before_the_day():
    fri = [(datetime(2026, 10, 30, 9, 14), 1.0, 99999.0, 1.0, 1.0),            # pre-open: ignored
           (datetime(2026, 10, 30, 9, 15), 100.0, 120.0, 95.0, 110.0), (datetime(2026, 10, 30, 15, 29), 110.0, 112.0, 90.0, 105.0)]
    got = eb.previous_cpr(date(2026, 11, 2), lambda d: fri if d == "2026-10-30" else [])
    assert got == cpr_levels(120.0, 90.0, 105.0)
    assert eb.previous_cpr(date(2026, 11, 2), lambda d: []) is None


def test_v4_is_v3_with_the_blocks_and_no_recovery_mode():
    assert pe.params_for(V4) == replace(pe.params_for(V3), bank_regardless_of_book=True)
    assert pe.params_for(V4, .02) == replace(pe.params_for(V3, .02), bank_regardless_of_book=True)
    assert pe.params_for(V4).bank_regardless_of_book and not pe.params_for(V3).bank_regardless_of_book
    assert V4 in pe.KNOWN_VERSIONS and V4 in pe.DAY_TARGET_VERSIONS and V4 in pe.NIFTY_SCALPER_VERSIONS
    assert pe.ENTRY_BLOCK_VERSIONS == {V4} and pr.instrument_for(V4) is pr.NIFTY_I
    assert pr.print_clock_for(V4) == pr.PRINT_CLOCK_3MIN and ex.is_proposer_strategy(V4)
    assert ex.PROPOSER_DECISION_ABI == "proposer-dt25-live-v18"
    assert tr.NIFTY_V4.key in tr.NIFTY_BOOKS and tr.NIFTY_V4.key in tr.DELTA_STRIKE_BOOKS
    assert tr.NIFTY_V4.key in tr.ENTRY_BLOCK_BOOKS and tr.NIFTY_V4.print_gap_s == tr.NIFTY_V3.print_gap_s
    assert tr.params_on("2026-10-20", tr.NIFTY_V4) == replace(tr.params_on("2026-10-20", tr.NIFTY_V3),
                                                              bank_regardless_of_book=True)


def _v4(monkeypatch, candle=PLAIN, file_has_it=True, broker=None, cpr=None):
    ctx, market, feed = setup_nifty(monkeypatch)
    svc.set_config(USER, CONN, "strategy_version", V4)
    minute = NOW.replace(second=0) - timedelta(minutes=1)
    prior = [(datetime(2026, 10, 20, 9, 15), 22000.0, 22100.0, 21900.0, 22050.0)]
    bars = {"2026-10-21": [(minute, *candle)] if file_has_it else [], "2026-10-20": prior if cpr is None else cpr}
    monkeypatch.setattr(pr, "nifty_bars", lambda d: bars.get(d, []))
    asked = []
    monkeypatch.setattr(pr, "broker_candle", lambda m: asked.append(m) or broker)
    return ctx, market, feed, asked


def test_live_enters_on_a_plain_candle_with_room(db, monkeypatch):  # noqa: F811
    ctx, market, feed, asked = _v4(monkeypatch)
    assert cycle(ctx, market, feed) and svc.get_trade_state(USER, CONN)["position"] == "OPEN"
    assert asked == []                                                  # the collector file had 09:21


@pytest.mark.parametrize("kwargs,reason", [
    ({"candle": (23000.0, 23010.0, 22990.0, 23001.0)}, eb.REASON_CANDLE),
    ({"file_has_it": False, "broker": (23000.0, 23010.0, 22990.0, 23001.0)}, eb.REASON_CANDLE),
    ({"file_has_it": False}, "candle_unknown"),
    ({"cpr": [(datetime(2026, 10, 20, 9, 15), 23000.0, 23040.0, 22980.0, 23020.0)]}, eb.REASON_CPR),
])
def test_live_holds_the_entry_and_spends_the_print(db, monkeypatch, caplog, kwargs, reason):  # noqa: F811
    ctx, market, feed, _ = _v4(monkeypatch, **kwargs)
    with caplog.at_level("INFO", logger="live.proposer_runner"):
        assert not cycle(ctx, market, feed)
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    assert f"entry held ({reason})" in caplog.text
    monkeypatch.setattr(pr, "nifty_bars", lambda d: [(NOW.replace(second=0), *PLAIN)])
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=40))   # same print: not retried


def test_live_v4_falls_back_to_the_broker_candle_when_the_file_is_late(db, monkeypatch):  # noqa: F811
    ctx, market, feed, asked = _v4(monkeypatch, file_has_it=False, broker=PLAIN)
    assert cycle(ctx, market, feed)
    assert asked == [NOW.replace(second=0) - timedelta(minutes=1)]


def test_v3_never_reads_the_blocks(db, monkeypatch):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch)
    svc.set_config(USER, CONN, "strategy_version", V3)
    monkeypatch.setattr(pr, "entry_block", lambda *a, **k: pytest.fail("V3 consulted the V4 blocks"))
    assert cycle(ctx, market, feed)


def test_tab_registered_and_renders(tmp_path):
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import NIFTY_SCALPER_KEYS
    from labs.ui.routes import LIVE_TABS
    key = tr.NIFTY_V4.key
    assert LIVE_TABS[key] == "Nifty Scalper V4" and MORE_BOOKS[key]["instrument"] == "NIFTY options"
    assert key in NIFTY_SCALPER_KEYS
    page = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_" + key + ".html' %}" in page and f"/labs/api/{key}/backfill" in page
    assert f'("{key}", run_{key}_day)' in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    module = __import__(f"labs.engine.{key}_book", fromlist=["BOOK"])
    assert module.BOOK is tr.NIFTY_V4 and module.DEFAULT_START == "2026-07-01"
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn, tr.NIFTY_V4)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template(f"_{key}.html").render(**{f"{key}_rows": [], f"{key}_trades": [],
                                                      f"{key}_stats": {"daily_target_pct": 0.015}})
    assert "1.5% day target" in html and "within 15 points" in html and "Paper only" in html
    assert 'id="proposer-nifty-v4-backfill"' in html and "proposer_nifty_v3" not in html


def _post(client, strategy, day_target="1.5"):                               # noqa: F811
    return client.post("/live/configure", data={"csrf_token": CSRF_TOKEN, "lots": "1", "daily_loss_cap": "3000",
                                                "strategy": strategy, "day_target": day_target})


def test_configure_offers_v4_and_its_orders_need_a_v17_runner(client):       # noqa: F811
    _set_strategy("proposer", V3)
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", ex.PROPOSER_DECISION_ABI)
    assert 'value="nifty_scalper_v4"' in client.get("/live/configure").get_data(as_text=True)
    assert _post(client, "nifty_scalper_v4").status_code == 302
    assert _strategy_pair() == ("proposer", V4)
    assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "1.5"
    assert ex.expected_decision_abi(V4) == "proposer-dt25-live-v18"      # a v16 runner's heartbeat does not match


# ─────────────────────────────────────── V4 banks the day target whatever the book (2026-11-05) ──
T_BANK = datetime(2026, 11, 5, 10, 0)


def _print(asof, x5="strong_bull"):
    return {"regime": "neutral", "x5": x5, "x5_conf": 0.6, "x5_asof": asof.isoformat()}


@pytest.mark.parametrize("bank_regardless,banks", [(True, True), (False, False)])
def test_day_target_with_the_book_under_water(bank_regardless, banks):
    e = pe.ProposerEngine(pe.ProposerParams(daily_target_pct=0.025, bank_regardless_of_book=bank_regardless))
    held = pe.Position(side="CE", entry_price=400, qty=20, entry_spot=74000)     # target 2.5% of 8,000 = Rs 200
    e.set_book(day_realized=0.0, book_net=-200_000.0)
    sig = e.evaluate(T_BANK, _print(T_BANK), held, option_ltp=410, spot=74010)   # +Rs 200 unrealised
    assert (sig.reason == "daily_target") is banks
    e.set_book(day_realized=200.0, book_net=-199_800.0)                         # the exit booked the target
    e.evaluate(T_BANK + timedelta(seconds=2), _print(T_BANK), pe.Position(), option_ltp=None, spot=1)
    assert e.banked() is banks
    t1 = T_BANK + timedelta(minutes=10)
    nxt = e.evaluate(t1, _print(t1), pe.Position(), option_ltp=None, spot=1)
    assert (nxt.reason == "day_target_banked") is banks and (nxt.action == "ENTER") is not banks


def test_fresh_book_banks_either_way():
    for flag in (True, False):
        e = pe.ProposerEngine(pe.ProposerParams(daily_target_pct=0.025, bank_regardless_of_book=flag))
        e.set_book(day_realized=0.0, book_net=0.0)
        held = pe.Position(side="CE", entry_price=400, qty=20, entry_spot=74000)
        assert e.evaluate(T_BANK, _print(T_BANK), held, option_ltp=410, spot=74010).reason == "daily_target"


def test_only_v4_skips_recovery_mode():
    on = {v for v in pe.KNOWN_VERSIONS if pe.params_for(v).bank_regardless_of_book}
    assert on == {V4}
    assert tr.params_on("2026-11-05", tr.NIFTY_V4).bank_regardless_of_book
    assert not any(tr.params_on("2026-11-05", b).bank_regardless_of_book
                   for b in (tr.NIFTY_V3, tr.NIFTY_V5, tr.V5_DT25, tr.V6_DT25))
