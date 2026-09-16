from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from labs.engine import proposer_sensex_backfill as backfill
from labs.engine import proposer_sensex_tracker as tracker
from labs.engine.charges import sensex_round_trip_charges

DAY = "2026-09-11"
SPOT = 74520.0
CE_STRIKE = 74300          # rounded ATM (74500) minus 200
PE_STRIKE = 74700


def _quotes(ce_ltp: dict[str, float] | None = None) -> pd.DataFrame:
    """One session of SENSEX marks, one CE and one PE contract."""
    ce_ltp = ce_ltp or {}
    rows = []
    for minute in pd.date_range(f"{DAY} 09:15", f"{DAY} 09:30", freq="1min"):
        stamp = minute.strftime("%Y-%m-%d %H:%M:%S")
        ltp = ce_ltp.get(minute.strftime("%H:%M"), 100.0)
        rows.append({"timestamp": stamp, "spot": SPOT, "strike": CE_STRIKE,
                     "option_type": "CE", "expiry": "26917", "bid": ltp - 1.0,
                     "ask": ltp + 1.0, "ltp": ltp,
                     "tradingsymbol": f"SENSEX26917{CE_STRIKE}CE"})
        rows.append({"timestamp": stamp, "spot": SPOT, "strike": PE_STRIKE,
                     "option_type": "PE", "expiry": "26917", "bid": 79.0,
                     "ask": 81.0, "ltp": 80.0,
                     "tradingsymbol": f"SENSEX26917{PE_STRIKE}PE"})
    return pd.DataFrame(rows)


def _predictor(microtrend: str = "U", strong_conf: float = 60.0) -> pd.DataFrame:
    return pd.DataFrame([
        {"trade_date": DAY, "ts": f"{DAY} 09:00:00", "kind": "regime",
         "label": "bearish", "conf": 44.0, "microtrend": "", "mom5": ""},
        {"trade_date": DAY, "ts": f"{DAY} 09:16:00", "kind": "5class",
         "label": "Strong Bull", "conf": strong_conf, "microtrend": microtrend, "mom5": "U"},
    ])


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "labs_test.db")
    conn.row_factory = sqlite3.Row
    tracker._ensure_tables(conn)
    return conn


def _install(monkeypatch, frame: pd.DataFrame) -> None:
    monkeypatch.setattr(tracker, "load_options_frame", lambda *a, **k: frame)
    monkeypatch.setattr(tracker, "select_expiry_code", lambda *a, **k: "26917")
    monkeypatch.setattr(tracker, "_warmup_spot", lambda *a, **k: pd.Series(dtype="float64"))


def test_strong_print_sets_side_and_strike_against_the_regime(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    tracker.seed_predictor_rows(_predictor(), connection=conn)
    _install(monkeypatch, _quotes())
    result = tracker.run_day(DAY, persist=False, connection=conn)

    assert result["side_day"] == "PE"                    # bearish 09:00 regime
    trade = result["trades"][0]
    assert trade["side"] == "CE"                         # Strong Bull >45% overrides it
    assert trade["signal"] == "5class_strong_bull"
    assert trade["strike"] == CE_STRIKE                  # rounded ATM - 200
    assert result["qty"] == 500 and result["lots"] == 25


def test_entry_is_ask_and_exit_is_bid_with_sensex_charges(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    tracker.seed_predictor_rows(_predictor(), connection=conn)
    _install(monkeypatch, _quotes({"09:22": 103.0, "09:23": 103.0}))
    trade = tracker.run_day(DAY, persist=False, connection=conn)["trades"][0]

    assert trade["entry_ask"] == pytest.approx(101.0)
    assert trade["exit_bid"] == pytest.approx(102.0)
    assert trade["gross_rs"] == pytest.approx((102.0 - 101.0) * 500)
    expected = sensex_round_trip_charges(101.0, 102.0, 500)["total"]
    assert trade["charges_rs"] == pytest.approx(round(expected, 2))
    assert trade["net_rs"] == pytest.approx(round(trade["gross_rs"] - trade["charges_rs"], 2))


def test_daily_target_is_2_5pct_of_the_first_trade_premium_and_ends_the_day(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    tracker.seed_predictor_rows(_predictor(), connection=conn)
    _install(monkeypatch, _quotes({"09:22": 103.0, "09:23": 103.0, "09:25": 103.0}))
    result = tracker.run_day(DAY, persist=False, connection=conn)

    assert result["first_trade_premium_rs"] == pytest.approx(100.0 * 500)
    assert result["day_target_rs"] == pytest.approx(0.025 * 100.0 * 500)
    assert result["day_done_by_target"] is True
    assert result["n_trades"] == 1                       # session stops after the target
    assert result["trades"][0]["exit_rule"] == "daily_target"
    assert result["trades"][0]["exit_ts"].endswith("09:22:00")


def test_micro_trend_against_the_position_exits(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    tracker.seed_predictor_rows(_predictor(microtrend="D"), connection=conn)
    _install(monkeypatch, _quotes())
    trade = tracker.run_day(DAY, persist=False, connection=conn)["trades"][0]
    assert trade["exit_rule"] == "microtrend_reversal"


def test_missing_predictor_rows_fail_closed(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    _install(monkeypatch, _quotes())
    with pytest.raises(tracker.ProposerInputError):
        tracker.run_day(DAY, persist=False, connection=conn)


def test_weekend_is_never_treated_as_a_session(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    _install(monkeypatch, _quotes())
    with pytest.raises(tracker.ProposerInputError):
        tracker.run_day("2026-09-12", persist=False, connection=conn)   # Saturday


def test_persists_daily_and_trade_rows(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    tracker.seed_predictor_rows(_predictor(), connection=conn)
    _install(monkeypatch, _quotes({"09:22": 103.0, "09:23": 103.0}))
    tracker.run_day(DAY, connection=conn)

    daily = conn.execute("SELECT * FROM proposer_daily WHERE trade_date=?", (DAY,)).fetchone()
    trades = conn.execute("SELECT * FROM proposer_trades WHERE trade_date=?", (DAY,)).fetchall()
    assert daily["qty"] == 500 and daily["strategy_version"] == tracker.STRATEGY_VERSION
    assert len(trades) == 1 and trades[0]["exit_rule"] == "daily_target"


def test_unavailable_day_is_audited_without_a_fake_trade(tmp_path):
    conn = _conn(tmp_path)
    tracker.record_unavailable(DAY, "no SENSEX quotes", connection=conn)
    row = conn.execute("SELECT * FROM proposer_daily WHERE trade_date=?", (DAY,)).fetchone()
    assert row["status"] == "unavailable" and row["n_trades"] == 0
    assert conn.execute("SELECT COUNT(*) FROM proposer_trades WHERE trade_date=?",
                        (DAY,)).fetchone()[0] == 0


def test_capture_parses_five_class_votes(tmp_path):
    path = tmp_path / "predictor_full.tsv"
    path.write_text(
        "2026-09-11\t09:21\t5\tStrong Bull\t46\t60,19,3,4,13\tnUnU-nn\t+ok\n"
        "2026-09-11\t09:00\tregime\tbearish\t44cal\tBull 22%\tcrude: (bear)\t+ok\n",
        encoding="utf-8")
    frame = backfill.load_capture(path)
    five = frame[frame["kind"] == "5class"].iloc[0]
    assert five["conf"] == 46.0
    assert five["microtrend"] == "n" and five["mom5"] == "U"
    assert frame[frame["kind"] == "regime"].iloc[0]["conf"] == 44.0


def test_ui_and_paper_loop_are_wired():
    root = Path(__file__).resolve().parents[1]
    routes = (root / "labs" / "ui" / "routes.py").read_text(encoding="utf-8")
    template = (root / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    loop = (root / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    daily = (root / "pa_paper_tracker.py").read_text(encoding="utf-8")
    assert "proposer_sensex" in routes and "proposer" in template
    assert "proposer_sensex_tracker" in loop and "proposer_sensex_tracker" in daily
