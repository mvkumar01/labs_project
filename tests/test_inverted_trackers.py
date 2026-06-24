import sqlite3

import pandas as pd

from labs.engine import sensex_alpha_inverted_tracker as sx_inv
from labs.engine import sensex_alpha_tracker as sx_base
from labs.engine import sensex_v211_inverted_tracker as v211_inv


DATE = "2026-06-18"


def _mark(hhmm: str) -> str:
    return pd.Timestamp(f"{DATE} {hhmm}", tz=sx_base.IST).isoformat()


def test_v211_inverted_swaps_call_signal_to_put_and_persists(monkeypatch) -> None:
    entry, exit_ = _mark("09:20"), _mark("09:25")
    replay = {
        "tier": "PC400",
        "direction": "DOWN",
        "session_done": True,
        "sim_trades": [{
            "pos": "call", "entry_ts": entry, "exit_ts": exit_,
            "entry_rule": "test", "reason": "zero",
        }],
    }
    book = (
        "26618",
        {
            (entry, 74900, "pe"): {
                "bid": 98.0, "ask": 100.0, "tradingsymbol": "SENSEX74900PE",
            },
            (exit_, 74900, "pe"): {
                "bid": 140.0, "ask": 142.0, "tradingsymbol": "SENSEX74900PE",
            },
        },
        {entry: 74858.0, exit_: 74950.0},
    )
    monkeypatch.setattr(v211_inv, "replay_champion_signals", lambda *_a, **_k: replay)
    monkeypatch.setattr(v211_inv, "build_sensex_book", lambda *_a: book)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    result = v211_inv.run_day(DATE, connection=conn)

    trade = conn.execute("SELECT * FROM sensex_v211_inverted_trades").fetchone()
    assert result["option_gross_rs"] == 800.0
    assert trade["original_side"] == "CALL"
    assert trade["side"] == "PUT"
    assert trade["entry_ask"] == 100.0
    assert trade["exit_bid"] == 140.0


def test_sensex_alpha_inverted_preserves_timing_but_swaps_option(monkeypatch) -> None:
    marks = pd.date_range(f"{DATE} 09:15", periods=3, freq="5min", tz=sx_base.IST)
    bars = pd.DataFrame({
        "timestamp": marks,
        "spot": [80000.0, 80010.0, 80020.0],
        "alpha": [0.0, 31.0, -1.0],
        "pe_delta": [0.0, 0.0, 0.0],
        "ce_delta": [0.0, 0.0, 0.0],
    })
    entry, exit_ = marks[1].isoformat(), marks[2].isoformat()
    quotes = {
        (entry, 80000, "pe"): {
            "bid": 90.0, "ask": 95.0, "tradingsymbol": "ORIGINAL_PE",
        },
        (exit_, 80000, "pe"): {
            "bid": 100.0, "ask": 105.0, "tradingsymbol": "ORIGINAL_PE",
        },
        (entry, 80000, "ce"): {
            "bid": 110.0, "ask": 115.0, "tradingsymbol": "INVERTED_CE",
        },
        (exit_, 80000, "ce"): {
            "bid": 130.0, "ask": 135.0, "tradingsymbol": "INVERTED_CE",
        },
    }
    context = {
        "prev_close": 79900.0,
        "range_lower": 78700.0,
        "range_upper": 81100.0,
        "expiry_code": "26618",
        "baseline_date": "2026-06-17",
    }
    config = {
        "entry_threshold": 30.0, "target_abs": 100.0,
        "eod_exit": "15:25", "lot_size": 20,
    }
    monkeypatch.setattr(sx_base, "load_config", lambda: config)
    monkeypatch.setattr(
        sx_base, "build_alpha_bars", lambda *_a, **_k: (context, bars, quotes)
    )
    monkeypatch.setattr(sx_base, "_session_over", lambda *_a: False)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    result = sx_inv.run_day(DATE, connection=conn)

    trade = conn.execute("SELECT * FROM sensex_alpha_inverted_trades").fetchone()
    assert result["option_gross_rs"] == 300.0
    assert trade["original_side"] == "PUT"
    assert trade["side"] == "CALL"
    assert trade["entry_ts"] == entry
    assert trade["exit_ts"] == exit_
    assert trade["tradingsymbol"] == "INVERTED_CE"
    assert trade["spot_pnl_pts"] == 10.0
