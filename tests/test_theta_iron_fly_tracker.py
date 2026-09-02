from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from labs.engine import theta_iron_fly_tracker as tracker


ENTRY = {
    (25000, "ce"): (100.0, 101.0),
    (25000, "pe"): (90.0, 91.0),
    (25400, "ce"): (10.0, 11.0),
    (24600, "pe"): (9.0, 10.0),
}


def _frame(*, target_hit: bool = True, missing_long_put: bool = False) -> pd.DataFrame:
    if target_hit:
        ten = {
            (25000, "ce"): (60.0, 61.0),
            (25000, "pe"): (50.0, 51.0),
            (25400, "ce"): (7.0, 8.0),
            (24600, "pe"): (6.0, 7.0),
        }
    else:
        ten = {
            (25000, "ce"): (96.0, 97.0),
            (25000, "pe"): (86.0, 87.0),
            (25400, "ce"): (9.0, 10.0),
            (24600, "pe"): (8.0, 9.0),
        }
    close = {
        (25000, "ce"): (90.0, 91.0),
        (25000, "pe"): (80.0, 81.0),
        (25400, "ce"): (8.0, 9.0),
        (24600, "pe"): (7.0, 8.0),
    }
    rows = []
    for timestamp, quotes in (
        ("2026-06-01 09:20", ENTRY),
        ("2026-06-01 10:00", ten),
        ("2026-06-01 15:00", close),
    ):
        for (strike, option_type), (bid, ask) in quotes.items():
            if missing_long_put and strike == 24600:
                continue
            rows.append({
                "timestamp": pd.Timestamp(timestamp, tz="Asia/Kolkata"),
                "spot": 25010.0,
                "strike": strike,
                "type": option_type,
                "bid": bid,
                "ask": ask,
                "tradingsymbol": f"NIFTY26604{strike}{option_type.upper()}",
            })
    return pd.DataFrame(rows)


def _install(monkeypatch, frame: pd.DataFrame) -> None:
    monkeypatch.setattr(
        tracker, "_normalise_frame", lambda trade_date: (frame, "26604")
    )


def test_iron_fly_uses_executable_prices_and_defined_risk(monkeypatch):
    _install(monkeypatch, _frame())

    result = tracker.run_day("2026-06-01", require_close=True, persist=False)

    assert result["status"] == "closed"
    assert result["exit_reason"] == "PROFIT_TARGET_20PCT"
    assert result["exit_ts"][11:16] == "10:00"
    assert result["net_credit_rs"] == pytest.approx((100 + 90 - 11 - 10) * 65)
    assert result["target_rs"] == pytest.approx(result["net_credit_rs"] * 0.20)
    assert result["capital_required_rs"] == pytest.approx((400 - 169) * 65)
    assert result["n_legs"] == 4
    assert result["charges_rs"] > 0

    legs = {leg["leg"]: leg for leg in result["legs"]}
    assert legs["SHORT_CALL"]["entry_price"] == 100
    assert legs["SHORT_CALL"]["exit_price"] == 61
    assert legs["LONG_CALL_WING"]["entry_price"] == 11
    assert legs["LONG_CALL_WING"]["exit_price"] == 7


def test_time_exit_is_used_when_target_does_not_hit(monkeypatch):
    _install(monkeypatch, _frame(target_hit=False))

    result = tracker.run_day("2026-06-01", require_close=True, persist=False)

    assert result["status"] == "closed"
    assert result["exit_reason"] == "TIME_1500"
    assert result["exit_ts"][11:16] == "15:00"


def test_missing_wing_fails_closed(monkeypatch):
    _install(monkeypatch, _frame(missing_long_put=True))

    with pytest.raises(
        tracker.ThetaIronFlyInputError, match="common executable four-leg quote"
    ):
        tracker.run_day("2026-06-01", require_close=True, persist=False)


def test_persists_one_daily_row_and_four_legs(monkeypatch):
    _install(monkeypatch, _frame())
    conn = sqlite3.connect(":memory:")

    tracker.run_day("2026-06-01", require_close=True, connection=conn)

    daily = conn.execute(
        "SELECT status,atm_strike,lower_wing_strike,upper_wing_strike,exit_reason "
        "FROM theta_iron_fly_daily"
    ).fetchone()
    assert daily == ("closed", 25000, 24600, 25400, "PROFIT_TARGET_20PCT")
    assert conn.execute(
        "SELECT COUNT(*) FROM theta_iron_fly_trades"
    ).fetchone()[0] == 4


def test_unavailable_day_never_creates_fake_legs():
    conn = sqlite3.connect(":memory:")

    tracker.record_unavailable(
        "2026-06-01", "missing wing quote", connection=conn
    )

    row = conn.execute(
        "SELECT status,n_legs,net_rs,error FROM theta_iron_fly_daily"
    ).fetchone()
    assert row == ("unavailable", 0, 0.0, "missing wing quote")
    assert conn.execute(
        "SELECT COUNT(*) FROM theta_iron_fly_trades"
    ).fetchone()[0] == 0


def test_paper_runtime_and_ui_are_wired():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    routes = (root / "labs" / "ui" / "routes.py").read_text(encoding="utf-8")
    loop = (root / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")

    assert "09:20 Iron Fly" in template
    assert "/api/theta_iron_fly/backfill" in routes
    assert "run_theta_iron_fly_day" in loop
