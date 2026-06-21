"""Persistence tests for four-way contract comparison (paper_contract_daily/trades).

Uses an in-memory SQLite connection so the production DB is never touched.
"""
import sqlite3

import pytest

from labs.engine import paper_strategy_tracker as tracker

TRADE_DATE = "2026-06-01"
ENTRY_TS = "2026-06-01T09:20:00+05:30"
EXIT_TS  = "2026-06-01T09:25:00+05:30"


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    tracker._ensure_tables(conn)
    return conn


def _make_prices(near_ltp=100.0, next_ltp=200.0):
    """Minimal price entries covering ATM and ITM-200 for both CE and PE."""
    strikes = {
        "ce": [23350, 23550],   # ATM-200 and ATM for calls (entry_spot 23560 → ATM 23550)
        "pe": [23550, 23750],   # ATM and ATM+200 for puts
    }
    def _book(ltp):
        prices = {}
        for otype, ks in strikes.items():
            for k in ks:
                prices[(ENTRY_TS, k, otype)] = ltp
                prices[(EXIT_TS,  k, otype)] = ltp + 10
        return prices

    return {
        "nearest_weekly": {"expiry_code": "26602", "prices": _book(near_ltp)},
        "next_weekly":    {"expiry_code": "26609", "prices": _book(next_ltp)},
    }


def _make_sim_trade(pos="put"):
    return {
        "pos": pos,
        "entry_spot": 23560.0,
        "exit_spot": 23540.0,
        "entry_ts": ENTRY_TS,
        "exit_ts":  EXIT_TS,
        "pnl": -20.0,
        "entry_rule": "R1",
        "reason": "alpha_sl",
    }


# ── Four rows per date ────────────────────────────────────────────────────────

def test_four_daily_rows_written_per_trade_date():
    conn = _make_conn()
    tracker._save_all_comparison_variants(conn, TRADE_DATE, _make_prices(), [_make_sim_trade()], True)

    rows = conn.execute(
        "SELECT variant FROM paper_contract_daily WHERE trade_date=? ORDER BY variant",
        (TRADE_DATE,)
    ).fetchall()
    assert len(rows) == 4
    assert {r["variant"] for r in rows} == {"near_atm", "near_itm200", "next_atm", "next_itm200"}


def test_four_trade_rows_written_per_strategy_trade():
    conn = _make_conn()
    tracker._save_all_comparison_variants(conn, TRADE_DATE, _make_prices(), [_make_sim_trade()], True)

    rows = conn.execute(
        "SELECT variant FROM paper_contract_trades WHERE trade_date=? AND seq=1",
        (TRADE_DATE,)
    ).fetchall()
    assert len(rows) == 4


# ── near_itm200 matches primary logic ────────────────────────────────────────

def test_near_itm200_net_matches_direct_price_trade():
    books = _make_prices()
    conn = _make_conn()
    sim = [_make_sim_trade(pos="put")]
    tracker._save_all_comparison_variants(conn, TRADE_DATE, books, sim, True)

    primary = tracker._price_trade(books["nearest_weekly"]["prices"], sim[0], 200)

    row = conn.execute(
        "SELECT net_rs FROM paper_contract_daily WHERE trade_date=? AND variant='near_itm200'",
        (TRADE_DATE,)
    ).fetchone()
    assert row is not None
    assert abs(row["net_rs"] - primary["net_rs"]) < 0.01


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_rerunning_same_date_is_idempotent():
    conn = _make_conn()
    books = _make_prices()
    sim = [_make_sim_trade()]
    tracker._save_all_comparison_variants(conn, TRADE_DATE, books, sim, True)
    tracker._save_all_comparison_variants(conn, TRADE_DATE, books, sim, True)

    count = conn.execute(
        "SELECT COUNT(*) FROM paper_contract_daily WHERE trade_date=?", (TRADE_DATE,)
    ).fetchone()[0]
    assert count == 4


# ── No-trade days ─────────────────────────────────────────────────────────────

def test_no_trade_day_writes_four_zero_net_no_trade_rows():
    conn = _make_conn()
    tracker._save_all_comparison_variants(conn, TRADE_DATE, {}, [], True)

    rows = conn.execute(
        "SELECT variant, status, net_rs FROM paper_contract_daily WHERE trade_date=?",
        (TRADE_DATE,)
    ).fetchall()
    assert len(rows) == 4
    assert all(r["status"] == "no_trade" for r in rows)
    assert all(r["net_rs"] == 0.0 for r in rows)


# ── Missing next-weekly expiry ────────────────────────────────────────────────

def test_missing_next_weekly_marks_next_variants_unavailable():
    conn = _make_conn()
    # Only nearest_weekly in books
    books = {"nearest_weekly": _make_prices()["nearest_weekly"]}
    tracker._save_all_comparison_variants(conn, TRADE_DATE, books, [_make_sim_trade()], True)

    near_rows = conn.execute(
        "SELECT status FROM paper_contract_daily WHERE trade_date=? AND variant LIKE 'near_%'",
        (TRADE_DATE,)
    ).fetchall()
    next_rows = conn.execute(
        "SELECT status, net_rs FROM paper_contract_daily WHERE trade_date=? AND variant LIKE 'next_%'",
        (TRADE_DATE,)
    ).fetchall()

    assert all(r["status"] == "priced" for r in near_rows)
    assert all(r["status"] == "unavailable" for r in next_rows)
    assert all(r["net_rs"] is None for r in next_rows)


# ── Missing quote marks only that variant unavailable ────────────────────────

def test_missing_quote_marks_only_that_variant_unavailable():
    """Removing one LTP entry from next_weekly leaves near variants priced."""
    books = _make_prices()
    del books["next_weekly"]["prices"][(ENTRY_TS, 23750, "pe")]  # break next_itm200 PUT

    conn = _make_conn()
    tracker._save_all_comparison_variants(conn, TRADE_DATE, books, [_make_sim_trade(pos="put")], True)

    next_itm = conn.execute(
        "SELECT status, net_rs, error FROM paper_contract_daily "
        "WHERE trade_date=? AND variant='next_itm200'",
        (TRADE_DATE,)
    ).fetchone()
    assert next_itm["status"] == "unavailable"
    assert next_itm["net_rs"] is None
    assert next_itm["error"] is not None

    near_itm = conn.execute(
        "SELECT status FROM paper_contract_daily WHERE trade_date=? AND variant='near_itm200'",
        (TRADE_DATE,)
    ).fetchone()
    assert near_itm["status"] == "priced"


# ── Strike offsets are correct ────────────────────────────────────────────────

def test_variant_strike_offsets_are_correct():
    """near_atm uses ATM strike; near_itm200 uses ATM-200 for CALL."""
    conn = _make_conn()
    tracker._save_all_comparison_variants(
        conn, TRADE_DATE, _make_prices(), [_make_sim_trade(pos="call")], True
    )
    trade_rows = conn.execute(
        "SELECT variant, strike FROM paper_contract_trades WHERE trade_date=? AND seq=1",
        (TRADE_DATE,)
    ).fetchall()
    by_variant = {r["variant"]: r["strike"] for r in trade_rows}

    atm = tracker._r50(23560.0)   # = 23550
    assert by_variant["near_atm"]    == atm           # offset 0
    assert by_variant["near_itm200"] == atm - 200     # ITM 200
    assert by_variant["next_atm"]    == atm           # offset 0
    assert by_variant["next_itm200"] == atm - 200


# ── Unavailable net_rs is NULL, not zero ──────────────────────────────────────

def test_unavailable_net_rs_is_null_not_zero():
    conn = _make_conn()
    # Pass empty books so all four have no expiry → unavailable
    tracker._save_all_comparison_variants(conn, TRADE_DATE, {}, [_make_sim_trade()], True)

    rows = conn.execute(
        "SELECT status, net_rs FROM paper_contract_daily WHERE trade_date=?",
        (TRADE_DATE,)
    ).fetchall()
    assert all(r["status"] == "unavailable" for r in rows)
    assert all(r["net_rs"] is None for r in rows)
