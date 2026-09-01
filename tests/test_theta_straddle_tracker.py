from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from labs.engine import theta_straddle_tracker as tracker
from labs.engine import theta_straddle_backfill as backfill
from labs.engine.charges import short_option_round_trip_charges


def _quotes(*, include_put: bool = True) -> pd.DataFrame:
    rows = []
    marks = [
        ("2026-06-01 09:19:00", 110.0, 111.0, 100.0, 101.0),
        ("2026-06-01 09:20:00", 100.0, 101.0, 90.0, 91.0),
        ("2026-06-01 10:00:00", 80.0, 81.0, 70.0, 71.0),
        ("2026-06-01 15:15:00", 60.0, 61.0, 50.0, 51.0),
    ]
    for timestamp, ce_bid, ce_ask, pe_bid, pe_ask in marks:
        rows.append({
            "timestamp": timestamp,
            "spot": 25010.0,
            "strike": 25000,
            "option_type": "CE",
            "expiry": "26604",
            "bid": ce_bid,
            "ask": ce_ask,
            "tradingsymbol": "NIFTY2660425000CE",
        })
        if include_put:
            rows.append({
                "timestamp": timestamp,
                "spot": 25010.0,
                "strike": 25000,
                "option_type": "PE",
                "expiry": "26604",
                "bid": pe_bid,
                "ask": pe_ask,
                "tradingsymbol": "NIFTY2660425000PE",
            })
    return pd.DataFrame(rows)


def _install_quotes(monkeypatch, frame: pd.DataFrame) -> None:
    monkeypatch.setattr(tracker, "load_options_frame", lambda *args, **kwargs: frame)
    monkeypatch.setattr(tracker, "select_expiry_code", lambda *args, **kwargs: "26604")


def test_closed_straddle_uses_bid_in_ask_out_and_estimated_capital(monkeypatch):
    _install_quotes(monkeypatch, _quotes())
    result = tracker.run_day("2026-06-01", require_close=True, persist=False)

    assert result["status"] == "closed"
    assert result["strike"] == 25000
    assert result["qty"] == 65
    assert result["gross_rs"] == pytest.approx((100 - 61 + 90 - 51) * 65)
    assert result["premium_credit_rs"] == pytest.approx((100 + 90) * 65)
    assert result["capital_required_rs"] == pytest.approx(25010 * 65 * 0.10)
    assert result["charges_rs"] > 0
    assert result["net_rs"] < result["gross_rs"]


def test_open_mark_never_uses_a_quote_before_entry(monkeypatch):
    frame = _quotes()
    frame = frame[pd.to_datetime(frame["timestamp"]) < pd.Timestamp("2026-06-01 15:15")]
    _install_quotes(monkeypatch, frame)

    result = tracker.run_day("2026-06-01", persist=False)

    assert result["status"] == "open"
    assert result["exit_ts"][11:16] == "10:00"
    assert result["legs"][0]["exit_buy_ask"] == 81.0


def test_missing_straddle_leg_fails_closed(monkeypatch):
    _install_quotes(monkeypatch, _quotes(include_put=False))
    with pytest.raises(tracker.ThetaStraddleInputError, match="common executable CE/PE bid"):
        tracker.run_day("2026-06-01", require_close=True, persist=False)


def test_weekend_data_is_never_treated_as_a_trade(monkeypatch):
    _install_quotes(monkeypatch, _quotes())
    with pytest.raises(tracker.ThetaStraddleInputError, match="not a trading weekday"):
        tracker.run_day("2026-06-06", require_close=True, persist=False)


def test_persists_daily_and_two_leg_ledgers(monkeypatch):
    _install_quotes(monkeypatch, _quotes())
    conn = sqlite3.connect(":memory:")

    tracker.run_day("2026-06-01", require_close=True, connection=conn)

    daily = conn.execute(
        "SELECT status, capital_required_rs, premium_credit_rs FROM theta_straddle_daily"
    ).fetchone()
    assert daily[0] == "closed"
    assert daily[1] > 0
    assert daily[2] > 0
    assert conn.execute("SELECT COUNT(*) FROM theta_straddle_trades").fetchone()[0] == 2


def test_unavailable_day_is_audited_without_a_fake_trade():
    conn = sqlite3.connect(":memory:")
    tracker.record_unavailable(
        "2026-06-01", "opening archive starts at 10:37", connection=conn
    )

    row = conn.execute(
        "SELECT status, n_legs, net_rs, error FROM theta_straddle_daily"
    ).fetchone()
    assert row == ("unavailable", 0, 0.0, "opening archive starts at 10:37")
    assert conn.execute("SELECT COUNT(*) FROM theta_straddle_trades").fetchone()[0] == 0


def test_short_option_stt_is_on_opening_sell_premium():
    charges = short_option_round_trip_charges(100, 60, 65)
    assert charges["stt"] == pytest.approx(round(100 * 65 * 0.0015, 2))


def test_ui_and_paper_loop_are_wired():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    routes = (root / "labs" / "ui" / "routes.py").read_text(encoding="utf-8")
    loop = (root / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")

    assert "09:20 Theta Straddle" in template
    assert "Estimated capital" in template
    assert "/api/theta_straddle/backfill" in routes
    assert "run_theta_straddle_day" in loop


def test_backfill_date_scan_excludes_weekends(monkeypatch):
    class Entry:
        def __init__(self, name):
            self.name = name

        def is_dir(self):
            return True

    class Root:
        def __init__(self, names):
            self.names = names

        def exists(self):
            return True

        def iterdir(self):
            return [Entry(name) for name in self.names]

    live = Root(["2026-06-05", "2026-06-06"])
    archive = Root([])
    monkeypatch.setattr(backfill, "SHARED_LIVE_DIR", live)
    monkeypatch.setattr(backfill, "SHARED_ARCHIVE_DIR", archive)
    monkeypatch.setattr(backfill, "resolve_options_source", lambda *args, **kwargs: True)

    assert backfill._available_dates("2026-06-01", "2026-06-07") == ["2026-06-05"]
