import sqlite3

import pandas as pd
import pytest

from labs.engine import alpha_v212_tracker as tracker
from live.engine import champion_sim


DATE = "2026-06-18"


def _mark(hhmm: str) -> str:
    return pd.Timestamp(f"{DATE} {hhmm}", tz=tracker.IST).isoformat()


def _segment() -> dict:
    return {
        "pos": "call",
        "entry_ts": _mark("09:20"),
        "exit_ts": _mark("09:25"),
        "entry_spot": 23510.0,
        "exit_spot": 23520.0,
        "pnl": 10.0,
        "entry_rule": "RULE1",
        "reason": "ENTRY_SPOT_SL",
    }


def _replay(segments: list[dict], session_done: bool = True) -> dict:
    return {
        "tier": "PC250",
        "direction": "UP",
        "segments": segments,
        "session_done": session_done,
    }


def _book(include_exit: bool = True) -> tuple[str, dict]:
    quotes = {
        (_mark("09:20"), 23300, "ce"): {
            "bid": 99.0,
            "ask": 100.0,
            "tradingsymbol": "NIFTY2661823300CE",
        },
    }
    if include_exit:
        quotes[(_mark("09:25"), 23300, "ce")] = {
            "bid": 110.0,
            "ask": 111.0,
            "tradingsymbol": "NIFTY2661823300CE",
        }
    return "26618", quotes


def _recovery_inputs():
    times = pd.date_range(f"{DATE} 09:15", periods=5, freq="5min")
    adf = pd.DataFrame(
        {
            "timestamp": times,
            "alpha": [0.0, 30.0, 40.0, 50.0, 100.0],
            "d_pe_sum": [1.0] * 5,
            "d_ce_sum": [1.0] * 5,
            "denom": [100.0] * 5,
            "spot": [100.0, 100.0, 99.0, 101.0, 110.0],
        }
    )
    by_minute = {}
    for ts in pd.date_range(f"{DATE} 09:15", f"{DATE} 09:39", freq="min"):
        by_minute[ts.strftime("%H:%M")] = (100.0, 101.0, 100.0, 100.0)
    by_minute["09:25"] = (100.0, 100.0, 99.0, 99.0)
    by_minute["09:30"] = (99.0, 102.0, 99.0, 101.0)
    for minute in range(35, 40):
        by_minute[f"09:{minute}"] = (105.0, 111.0, 105.0, 110.0)
    return adf, champion_sim.OHLC(by_minute)


def test_entry_spot_stop_then_confirmed_recovery_reenters_same_side() -> None:
    adf, ohlc = _recovery_inputs()

    pnl, segments = champion_sim.simulate(
        adf, {}, {}, ohlc, DATE, False, 1.0, "PC50", "Thu", "STD",
        0, 1000, enable_entry_spot_recovery=True,
    )

    assert pnl == 9.0
    assert [segment["reason"] for segment in segments] == [
        "ENTRY_SPOT_SL", "TGT_ALPHA"
    ]
    assert [segment["pos"] for segment in segments] == ["call", "call"]
    assert segments[0]["pnl"] == -1.0
    assert segments[0]["exit_spot"] == 99.0
    # 09:25 candle high/low detects the stop and its close supplies spot P&L;
    # the earliest causal executable quote is the following 09:26 mark.
    assert pd.Timestamp(segments[0]["exit_ts"]).strftime("%H:%M") == "09:26"
    assert pd.Timestamp(segments[1]["entry_ts"]).strftime("%H:%M") == "09:31"


def test_completed_candle_execution_cannot_cross_eod_square_off() -> None:
    assert champion_sim._event_is_executable_before_eod(
        DATE, pd.Timestamp(f"{DATE} 15:25")
    )
    assert not champion_sim._event_is_executable_before_eod(
        DATE, pd.Timestamp(f"{DATE} 15:26")
    )


def test_overlay_default_off_preserves_v211_single_segment() -> None:
    adf, ohlc = _recovery_inputs()

    pnl, segments = champion_sim.simulate(
        adf, {}, {}, ohlc, DATE, False, 1.0, "PC50", "Thu", "STD", 0, 1000
    )

    assert pnl == 10.0
    assert len(segments) == 1
    assert segments[0]["reason"] == "TGT_ALPHA"


def test_pricing_uses_first_quote_after_event_never_prior_quote() -> None:
    segment = _segment()
    strike = 23300
    quotes = {
        (_mark("09:19"), strike, "ce"): {"bid": 90.0, "ask": 91.0},
        (_mark("09:21"), strike, "ce"): {
            "bid": 99.0, "ask": 100.0, "tradingsymbol": "NIFTY_TEST_CE",
        },
        (_mark("09:26"), strike, "ce"): {
            "bid": 110.0, "ask": 111.0, "tradingsymbol": "NIFTY_TEST_CE",
        },
    }

    trade = tracker._price_segment(segment, "26618", quotes)

    assert trade["quote_status"] == "priced"
    assert pd.Timestamp(trade["entry_ts"]).strftime("%H:%M") == "09:21"
    assert pd.Timestamp(trade["exit_ts"]).strftime("%H:%M") == "09:26"
    assert trade["entry_ask"] == 100.0
    assert trade["exit_bid"] == 110.0


def test_pricing_skips_exact_minute_row_without_two_sided_book() -> None:
    segment = _segment()
    strike = 23300
    quotes = {
        (_mark("09:20"), strike, "ce"): {"bid": None, "ask": None},
        (_mark("09:21"), strike, "ce"): {
            "bid": 99.0, "ask": 100.0, "tradingsymbol": "NIFTY_TEST_CE",
        },
        (_mark("09:25"), strike, "ce"): {"bid": 110.0, "ask": None},
        (_mark("09:27"), strike, "ce"): {
            "bid": 111.0, "ask": 112.0, "tradingsymbol": "NIFTY_TEST_CE",
        },
    }

    trade = tracker._price_segment(segment, "26618", quotes)

    assert trade["quote_status"] == "priced"
    assert pd.Timestamp(trade["entry_ts"]).strftime("%H:%M") == "09:21"
    assert pd.Timestamp(trade["exit_ts"]).strftime("%H:%M") == "09:27"
    assert trade["entry_ask"] == 100.0
    assert trade["exit_bid"] == 111.0


def test_tracker_prices_itm200_at_ask_in_bid_out_and_persists(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tracker, "replay_v212", lambda *_args, **_kwargs: _replay([_segment()]))
    monkeypatch.setattr(tracker, "build_executable_book", lambda *_args: _book())

    result = tracker.run_day(DATE, connection=conn)

    assert result["status"] == "traded"
    assert result["n_segments"] == 1
    assert result["gross_rs"] == 650.0
    trade = conn.execute("SELECT * FROM alpha_v212_trades").fetchone()
    daily = conn.execute("SELECT * FROM alpha_v212_daily").fetchone()
    assert trade["strike"] == 23300
    assert trade["entry_ask"] == 100.0
    assert trade["exit_bid"] == 110.0
    assert trade["option_pnl_pts"] == 10.0
    assert trade["net_rs"] == pytest.approx(650.0 - trade["charges_rs"])
    assert daily["priced_segments"] == 1
    assert daily["unavailable_segments"] == 0


def test_require_all_quotes_preserves_existing_v212_rows(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    tracker._ensure_tables(conn)
    conn.execute(
        "INSERT INTO alpha_v212_daily "
        "(trade_date,status,tier,gap_dir,expiry_code,n_segments,priced_segments,"
        "unavailable_segments,spot_pnl_pts,gross_rs,charges_rs,net_rs,"
        "strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (DATE, "traded", "OLD", "UP", "OLD", 1, 1, 0, 10, 650, 50, 600, "before", "before"),
    )
    conn.commit()
    monkeypatch.setattr(tracker, "replay_v212", lambda *_args, **_kwargs: _replay([_segment()]))
    monkeypatch.setattr(
        tracker, "build_executable_book", lambda *_args: _book(include_exit=False)
    )

    with pytest.raises(tracker.AlphaV212InputError, match="pricing incomplete"):
        tracker.run_day(
            DATE, connection=conn, require_all_quotes=True
        )

    row = conn.execute(
        "SELECT tier,net_rs,strategy_version FROM alpha_v212_daily WHERE trade_date=?",
        (DATE,),
    ).fetchone()
    assert tuple(row) == ("OLD", 600.0, "before")
