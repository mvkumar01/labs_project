import sqlite3

import pandas as pd
import pytest

from labs.engine import sensex_v211_tracker as tracker


DATE = "2026-06-18"


def _mark(hhmm: str) -> str:
    return pd.Timestamp(f"{DATE} {hhmm}", tz=tracker.IST).isoformat()


def _signal(pos: str = "call") -> dict:
    return {
        "pos": pos,
        "entry_ts": _mark("09:20"),
        "exit_ts": _mark("09:25"),
        "entry_rule": "test_cross",
        "reason": "zero",
    }


def _replay(signals: list[dict], session_done: bool = True) -> dict:
    return {
        "tier": "PC400",
        "direction": "DOWN",
        "sim_trades": signals,
        "session_done": session_done,
    }


def _priced_book(option_type: str = "ce") -> tuple[str, dict, dict]:
    entry_ts = _mark("09:20")
    exit_ts = _mark("09:25")
    return (
        "26618",
        {
            (entry_ts, 74900, option_type): {
                "bid": 98.0,
                "ask": 100.0,
                "tradingsymbol": f"SENSEX74900{option_type.upper()}",
            },
            (exit_ts, 74900, option_type): {
                "bid": 140.0,
                "ask": 142.0,
                "tradingsymbol": f"SENSEX74900{option_type.upper()}",
            },
        },
        {
            entry_ts: 74858.0,
            exit_ts: 74950.0,
        },
    )


def test_nifty_signal_prices_same_side_sensex_atm_ask_in_bid_out(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(
        tracker, "replay_champion_signals", lambda *_args, **_kwargs: _replay([_signal("call")])
    )
    monkeypatch.setattr(tracker, "build_sensex_book", lambda *_args: _priced_book("ce"))

    result = tracker.run_day(DATE, connection=conn)

    assert result["status"] == "traded"
    assert result["option_gross_rs"] == 800.0
    trade = conn.execute("SELECT * FROM sensex_v211_trades").fetchone()
    daily = conn.execute("SELECT * FROM sensex_v211_daily").fetchone()
    assert trade["side"] == "CALL"
    assert trade["strike"] == 74900
    assert trade["entry_ask"] == 100.0
    assert trade["exit_bid"] == 140.0
    assert trade["option_pnl_pts"] == 40.0
    assert trade["option_gross_rs"] == 800.0
    assert trade["quote_status"] == "priced"
    assert daily["option_priced_trades"] == 1
    assert daily["option_unavailable_trades"] == 0


def test_missing_executable_quote_never_uses_ltp_fallback(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    entry_ts = _mark("09:20")
    exit_ts = _mark("09:25")
    monkeypatch.setattr(
        tracker, "replay_champion_signals", lambda *_args, **_kwargs: _replay([_signal("put")])
    )
    monkeypatch.setattr(
        tracker,
        "build_sensex_book",
        lambda *_args: (
            "26618",
            {
                (entry_ts, 74900, "pe"): {
                    "bid": 90.0,
                    "ask": None,
                    "tradingsymbol": "SENSEX74900PE",
                },
                (exit_ts, 74900, "pe"): {
                    "bid": 130.0,
                    "ask": 132.0,
                    "tradingsymbol": "SENSEX74900PE",
                },
            },
            {entry_ts: 74858.0, exit_ts: 74950.0},
        ),
    )

    result = tracker.run_day(DATE, connection=conn)

    trade = conn.execute("SELECT * FROM sensex_v211_trades").fetchone()
    daily = conn.execute("SELECT * FROM sensex_v211_daily").fetchone()
    assert result["status"] == "partial_unavailable"
    assert result["option_gross_rs"] == 0
    assert trade["side"] == "PUT"
    assert trade["quote_status"] == "entry_ask_unavailable"
    assert trade["option_pnl_pts"] is None
    assert trade["option_gross_rs"] is None
    assert daily["option_priced_trades"] == 0
    assert daily["option_unavailable_trades"] == 1


def test_require_all_quotes_fail_closed_preserves_existing_rows(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    tracker._ensure_tables(conn)
    conn.execute(
        "INSERT INTO sensex_v211_daily "
        "(trade_date,status,tier,gap_dir,expiry_code,n_trades,option_gross_rs,"
        "option_priced_trades,option_unavailable_trades,strategy_version,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (DATE, "traded", "OLD", "UP", "OLD", 1, 123.0, 1, 0, "before", "before"),
    )
    conn.commit()
    entry_ts = _mark("09:20")
    exit_ts = _mark("09:25")
    monkeypatch.setattr(
        tracker, "replay_champion_signals", lambda *_args, **_kwargs: _replay([_signal("call")])
    )
    monkeypatch.setattr(
        tracker,
        "build_sensex_book",
        lambda *_args: ("26618", {}, {entry_ts: 74858.0, exit_ts: 74950.0}),
    )

    with pytest.raises(tracker.SensexV211InputError, match="pricing incomplete"):
        tracker.run_day(DATE, connection=conn, require_all_quotes=True)

    row = conn.execute(
        "SELECT tier,option_gross_rs,strategy_version FROM sensex_v211_daily "
        "WHERE trade_date=?",
        (DATE,),
    ).fetchone()
    assert tuple(row) == ("OLD", 123.0, "before")


def test_no_trade_day_persists_without_loading_sensex_book(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(
        tracker, "replay_champion_signals", lambda *_args, **_kwargs: _replay([])
    )

    def should_not_load(*_args, **_kwargs):
        raise AssertionError("SENSEX book should not load without signal trades")

    monkeypatch.setattr(tracker, "build_sensex_book", should_not_load)

    result = tracker.run_day(DATE, connection=conn)

    assert result["status"] == "no_trade"
    assert result["expiry_code"] is None
    daily = conn.execute("SELECT * FROM sensex_v211_daily").fetchone()
    assert daily["n_trades"] == 0
    assert daily["option_gross_rs"] == 0.0
