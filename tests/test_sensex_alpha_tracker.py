import sqlite3

import pandas as pd
import pytest

from labs.engine import sensex_alpha_tracker as tracker


DATE = "2026-06-18"


def _bars(values):
    marks = pd.date_range(f"{DATE} 09:15", periods=len(values), freq="5min", tz=tracker.IST)
    return pd.DataFrame({
        "timestamp": marks,
        "spot": [80000 + i * 10 for i in range(len(values))],
        "alpha": values,
        "pe_delta": [0] * len(values),
        "ce_delta": [0] * len(values),
    })


def _quotes(bars):
    quotes = {}
    for row in bars.itertuples(index=False):
        timestamp = pd.Timestamp(row.timestamp).isoformat()
        for option_type in ("ce", "pe"):
            quotes[(timestamp, 80000, option_type)] = {
                "bid": 100 + row.Index if hasattr(row, "Index") else 100,
                "ask": 101,
                "tradingsymbol": f"SENSEX_TEST_{option_type.upper()}",
            }
    return quotes


def _config():
    return {
        "entry_threshold": 30.0,
        "target_abs": 100.0,
        "eod_exit": "15:25",
        "lot_size": 20,
    }


def test_inverted_crosses_and_zero_reversals() -> None:
    bars = _bars([0, 31, 10, -1, -31, -10, 1])
    quotes = _quotes(bars)
    trades = tracker.simulate(
        bars, quotes, {"expiry_code": "26618"}, _config()
    )

    assert [trade["side"] for trade in trades] == ["PUT", "CALL"]
    assert [trade["entry_reason"] for trade in trades] == [
        "cross_up_plus_30", "cross_down_minus_30"
    ]
    assert [trade["exit_reason"] for trade in trades] == [
        "reversal_zero", "reversal_zero"
    ]
    assert all(trade["status"] == "closed" for trade in trades)


def test_abs_alpha_formula_is_bounded_and_zero_safe() -> None:
    assert tracker.compute_abs_alpha(0, 0) == 0
    assert tracker.compute_abs_alpha(100, 0) == 100
    assert tracker.compute_abs_alpha(0, 100) == -100
    assert tracker.compute_abs_alpha(-100, 100) == -100
    assert tracker.compute_abs_alpha(100, -100) == 100


def test_monthly_expiry_matches_settled_date_within_same_month(
    monkeypatch, tmp_path
) -> None:
    baseline = pd.DataFrame({
        "symbol": ["SENSEX", "SENSEX"],
        "strike": [80000, 80000],
        "type": ["ce", "pe"],
        "oi": [100, 200],
        "expiry": ["2026-06-25", "2026-06-25"],
    })
    baseline.to_csv(tmp_path / "prev_day_oi_SENSEX_2026-06-18.csv", index=False)
    monkeypatch.setattr(tracker, "PREV_DAY_DIR", tmp_path)

    loaded, baseline_date = tracker._load_baseline("2026-06-19", "26JUN")

    assert baseline_date == "2026-06-18"
    assert len(loaded) == 2


def test_monthly_expiry_rejects_baseline_from_wrong_month(
    monkeypatch, tmp_path
) -> None:
    baseline = pd.DataFrame({
        "symbol": ["SENSEX", "SENSEX"],
        "strike": [80000, 80000],
        "type": ["ce", "pe"],
        "oi": [100, 200],
        "expiry": ["2026-07-02", "2026-07-02"],
    })
    baseline.to_csv(tmp_path / "prev_day_oi_SENSEX_2026-06-18.csv", index=False)
    monkeypatch.setattr(tracker, "PREV_DAY_DIR", tmp_path)

    with pytest.raises(tracker.SensexReplayInputError, match="rollover mismatch"):
        tracker._load_baseline("2026-06-19", "26JUN")


def test_hard_eod_exit_at_1525() -> None:
    bars = pd.DataFrame({
        "timestamp": pd.to_datetime(
            [f"{DATE} 15:15", f"{DATE} 15:20", f"{DATE} 15:25"]
        ).tz_localize(tracker.IST),
        "spot": [80000, 80010, 80020],
        "alpha": [0, 31, 20],
        "pe_delta": [0, 0, 0],
        "ce_delta": [0, 0, 0],
    })
    trades = tracker.simulate(
        bars, _quotes(bars), {"expiry_code": "26618"}, _config()
    )

    assert len(trades) == 1
    assert trades[0]["side"] == "PUT"
    assert trades[0]["exit_ts"][11:16] == "15:25"
    assert trades[0]["exit_reason"] == "eod_1525"


def test_option_execution_is_entry_ask_and_exit_bid_only() -> None:
    bars = _bars([0, 31, -1])
    entry_ts = pd.Timestamp(bars.iloc[1]["timestamp"]).isoformat()
    exit_ts = pd.Timestamp(bars.iloc[2]["timestamp"]).isoformat()
    quotes = {
        (entry_ts, 80000, "pe"): {
            "bid": 90.0, "ask": 100.0, "tradingsymbol": "SENSEX_PE"
        },
        (exit_ts, 80000, "pe"): {
            "bid": 130.0, "ask": 140.0, "tradingsymbol": "SENSEX_PE"
        },
    }

    trade = tracker.simulate(
        bars, quotes, {"expiry_code": "26618"}, _config()
    )[0]

    assert trade["entry_ask"] == 100.0
    assert trade["exit_bid"] == 130.0
    assert trade["option_pnl_pts"] == 30.0
    assert trade["option_gross_rs"] == 600.0
    assert trade["quote_status"] == "priced"


def test_missing_executable_quote_never_falls_back() -> None:
    bars = _bars([0, 31, -1])
    trade = tracker.simulate(
        bars, {}, {"expiry_code": "26618"}, _config()
    )[0]

    assert trade["quote_status"] == "entry_ask_unavailable"
    assert trade["option_pnl_pts"] is None
    assert trade["option_gross_rs"] is None
    assert trade["spot_pnl_pts"] != 0


def test_run_day_persists_separate_daily_and_trade_rows(monkeypatch) -> None:
    bars = _bars([0, 31, -1])
    quotes = _quotes(bars)
    context = {
        "prev_close": 79900.0,
        "range_lower": 78700.0,
        "range_upper": 81100.0,
        "expiry_code": "26618",
        "baseline_date": "2026-06-17",
    }
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tracker, "build_alpha_bars", lambda *_: (context, bars, quotes))
    monkeypatch.setattr(tracker, "load_config", _config)
    monkeypatch.setattr(tracker, "_session_over", lambda *_: False)

    result = tracker.run_day(DATE, connection=conn)

    assert result["n_trades"] == 1
    daily = conn.execute("SELECT * FROM sensex_alpha_daily").fetchone()
    trade = conn.execute("SELECT * FROM sensex_alpha_trades").fetchone()
    assert daily["trade_date"] == DATE
    assert daily["n_trades"] == 1
    assert trade["side"] == "PUT"
    assert trade["quote_status"] == "priced"


def test_input_failure_preserves_existing_rows(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    tracker._ensure_tables(conn)
    conn.execute(
        "INSERT INTO sensex_alpha_daily "
        "(trade_date,status,prev_close,range_lower,range_upper,n_trades,spot_pnl_pts,"
        "option_priced_trades,option_unavailable_trades,strategy_version,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (DATE, "traded", 80000, 78800, 81200, 1, 50, 1, 0, "before", "before"),
    )
    conn.commit()

    def fail(*_args, **_kwargs):
        raise tracker.SensexReplayInputError("baseline missing")

    monkeypatch.setattr(tracker, "build_alpha_bars", fail)

    with pytest.raises(tracker.SensexReplayInputError, match="baseline missing"):
        tracker.run_day(DATE, connection=conn)

    assert tuple(conn.execute(
        "SELECT strategy_version,spot_pnl_pts FROM sensex_alpha_daily WHERE trade_date=?",
        (DATE,),
    ).fetchone()) == ("before", 50.0)
