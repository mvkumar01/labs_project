"""Basket replay: v2.11 signal segments re-priced as multi-leg structures.

Leg pricing must be executable (BUY = ask in / bid out, SELL = bid in / ask
out), charges applied per leg, and any missing/insane quote must make the whole
trade 'unavailable' — never a partial basket.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import labs.engine.basket_replay as br
from labs.engine.alpha_v212_tracker import QTY, _timestamp_key
from labs.engine.charges import round_trip_charges

DATE = "2026-07-02"
ENTRY_TS = "2026-07-02T09:35:00+05:30"
EXIT_TS = "2026-07-02T10:10:00+05:30"
EK, XK = _timestamp_key(ENTRY_TS), _timestamp_key(EXIT_TS)

# entry_spot 24012 -> ATM 24000, CE ITM200 = 23800, CE OTM200 = 24200
TRADE = {"seq": 1, "side": "CALL", "entry_ts": ENTRY_TS, "exit_ts": EXIT_TS,
         "entry_spot": 24012.0}

QUOTES = {
    (EK, 24000, "ce"): {"bid": 100.0, "ask": 102.0},
    (XK, 24000, "ce"): {"bid": 130.0, "ask": 132.0},
    (EK, 24000, "pe"): {"bid": 80.0, "ask": 82.0},
    (XK, 24000, "pe"): {"bid": 60.0, "ask": 62.0},
}


def test_long_synthetic_pricing():
    legs = br.BASKETS["CALL"]["long_synthetic"]["legs"]
    res = br.price_basket_trade(TRADE, legs, QUOTES)
    assert res["status"] == "priced"
    # BUY ce: exit_bid 130 - entry_ask 102 = +28 ; SELL pe: entry_bid 80 - exit_ask 62 = +18
    assert [l["pnl_pts"] for l in res["legs"]] == [28.0, 18.0]
    expected_gross = (28.0 + 18.0) * QTY
    expected_charges = (round_trip_charges(102.0, 130.0, QTY)["total"]
                        + round_trip_charges(80.0, 62.0, QTY)["total"])
    assert res["gross_rs"] == round(expected_gross, 2)
    assert res["charges_rs"] == round(expected_charges, 2)
    assert res["net_rs"] == round(expected_gross - expected_charges, 2)


def test_missing_quote_makes_whole_trade_unavailable():
    legs = br.BASKETS["CALL"]["bull_call_spread"]["legs"]   # needs 23800 + 24200
    res = br.price_basket_trade(TRADE, legs, QUOTES)        # neither strike quoted
    assert res["status"] == "unavailable"
    assert res["net_rs"] is None and res["legs"] == []


def test_protected_synthetic_short_three_legs():
    legs = br.BASKETS["PUT"]["protected_synthetic_short"]["legs"]
    quotes = dict(QUOTES)
    quotes.update({
        (EK, 24200, "ce"): {"bid": 40.0, "ask": 42.0},      # OTM200 hedge leg
        (XK, 24200, "ce"): {"bid": 55.0, "ask": 57.0},
    })
    res = br.price_basket_trade(dict(TRADE, side="PUT"), legs, quotes)
    assert res["status"] == "priced"
    assert len(res["legs"]) == 3
    # B ATM pe: 60 bid out - 82 ask in = -22 ; S ATM ce: 100 bid in - 132 ask out = -32
    # B OTM200 ce hedge: 55 bid out - 42 ask in = +13
    assert [l["pnl_pts"] for l in res["legs"]] == [-22.0, -32.0, 13.0]
    assert [l["strike"] for l in res["legs"]] == [24000, 24000, 24200]
    assert res["gross_rs"] == round((-22.0 - 32.0 + 13.0) * QTY, 2)


def test_replay_day_persists_all_baskets(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_strategy_trades (trade_date TEXT, seq INTEGER,"
        " side TEXT, entry_ts TEXT, exit_ts TEXT, entry_spot REAL,"
        " PRIMARY KEY (trade_date, seq))")
    conn.execute(
        "INSERT INTO paper_strategy_trades VALUES (?,?,?,?,?,?)",
        (DATE, 1, "CALL", ENTRY_TS, EXIT_TS, 24012.0))
    monkeypatch.setattr(br, "build_executable_book",
                        lambda d: ("07JUL2026", QUOTES))

    summary = br.replay_day(DATE, conn)

    # one row per (side, basket) — CALL trades priced only under CALL baskets
    n_rows = conn.execute("SELECT COUNT(*) FROM basket_daily").fetchone()[0]
    n_baskets = len(br.BASKETS["CALL"]) + len(br.BASKETS["PUT"])
    assert n_rows == n_baskets
    syn = conn.execute(
        "SELECT priced, unavailable, net_rs FROM basket_daily "
        "WHERE trade_date=? AND side='CALL' AND basket='long_synthetic'",
        (DATE,)).fetchone()
    assert syn[0] == 1 and syn[1] == 0 and abs(syn[2]) > 0
    spread = conn.execute(
        "SELECT priced, unavailable FROM basket_daily "
        "WHERE trade_date=? AND side='CALL' AND basket='bull_call_spread'",
        (DATE,)).fetchone()
    assert spread == (0, 1)                                  # strikes unquoted
    put_row = conn.execute(
        "SELECT n_trades FROM basket_daily WHERE side='PUT' AND basket='short_synthetic'"
    ).fetchone()
    assert put_row[0] == 0                                   # no PUT trades that day
    assert summary["CALL:long_synthetic"] != 0
    assert br.pending_dates(conn) == []                      # day fully replayed


def test_day_replayed_under_old_basket_set_is_pending_again():
    """Adding a basket must re-open already-replayed days so refresh fills the
    new rows; stale rows for removed baskets must not satisfy the check."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_strategy_trades (trade_date TEXT, seq INTEGER,"
        " side TEXT, entry_ts TEXT, exit_ts TEXT, entry_spot REAL,"
        " PRIMARY KEY (trade_date, seq))")
    conn.execute(
        "INSERT INTO paper_strategy_trades VALUES (?,?,?,?,?,?)",
        (DATE, 1, "CALL", ENTRY_TS, EXIT_TS, 24012.0))
    br._ensure_tables(conn)
    # Simulate an old replay: one current basket + one removed (naked) basket.
    conn.execute(
        "INSERT INTO basket_daily VALUES (?,?,?,?,1,1,0,0,0,0,?)",
        (DATE, "CALL", "long_synthetic", "X", "now"))
    conn.execute(
        "INSERT INTO basket_daily VALUES (?,?,?,?,1,1,0,0,0,0,?)",
        (DATE, "CALL", "naked_itm200", "X", "now"))
    assert br.pending_dates(conn) == [DATE]                  # new baskets missing
