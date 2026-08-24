import json
import sqlite3

import pytest

from labs.engine import paper_strategy_tracker as tracker


DATE = "2026-06-01"


def _seed_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    tracker._ensure_tables(conn)
    conn.execute(
        "INSERT INTO paper_strategy_daily "
        "(trade_date,status,tier,gap_dir,n_trades,pnl_pts,gross_rs,charges_rs,net_rs,strategy_version,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (DATE, "traded", "PC50", "UP", 1, 10, 1000, 50, 950, "before", "before"),
    )
    conn.commit()
    return conn


def test_missing_replay_input_preserves_existing_result(monkeypatch) -> None:
    conn = _seed_conn()
    monkeypatch.setattr(tracker, "get_conn", lambda: conn)
    monkeypatch.setattr(
        tracker,
        "_resolve_day",
        lambda *_: {"lower": 23000, "upper": 24000, "bucket": "PC50",
                    "direction": "UP", "vix": 15, "biggap": False},
    )
    monkeypatch.setattr(tracker.champion_inputs, "ohlc_by_minute", lambda *_: {})
    monkeypatch.setattr(
        tracker.champion_inputs,
        "day_context",
        lambda *_: (0.0, "Mon", True, "TRAIL"),
    )
    monkeypatch.setattr(
        tracker.champion_inputs,
        "alpha_source",
        lambda *_: ("regime", False),
    )

    def fail(*_args, **_kwargs):
        raise FileNotFoundError("archived session missing")

    monkeypatch.setattr(tracker.champion_inputs, "build_sim_inputs", fail)

    with pytest.raises(tracker.ReplayInputError, match="existing rows retained"):
        tracker.run_day(DATE, override={"lower": 23000, "upper": 24000,
                                        "bucket": "PC50", "direction": "UP"})

    row = conn.execute(
        "SELECT status,n_trades,net_rs,strategy_version FROM paper_strategy_daily WHERE trade_date=?",
        (DATE,),
    ).fetchone()
    assert tuple(row) == ("traded", 1, 950.0, "before")


def test_missing_locked_state_preserves_existing_result(monkeypatch) -> None:
    conn = _seed_conn()
    monkeypatch.setattr(tracker, "get_conn", lambda: conn)
    monkeypatch.setattr(tracker, "_resolve_day", lambda *_: None)

    with pytest.raises(tracker.ReplayInputError, match="No locked range state"):
        tracker.run_day(DATE)

    assert conn.execute(
        "SELECT net_rs FROM paper_strategy_daily WHERE trade_date=?", (DATE,)
    ).fetchone()[0] == 950.0


def test_verified_locked_skip_is_not_treated_as_missing(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "hybrid_range_state.json"
    state_path.write_text(json.dumps({
        "trade_date": DATE,
        "locked": True,
        "verified_open_locked": True,
        "bucket": "SKIP",
        "direction": "UP",
        "lower": None,
        "upper": None,
    }), encoding="utf-8")
    monkeypatch.setattr(tracker, "HYBRID_STATE_FILE", state_path)
    monkeypatch.setattr(tracker, "_read_locked_hybrid_state", lambda *_: None)

    assert tracker._resolve_day(DATE, None) == {
        "bucket": "SKIP",
        "direction": "UP",
    }


def test_verified_skip_persists_no_trade_row(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    tracker._ensure_tables(conn)
    monkeypatch.setattr(
        tracker,
        "_resolve_day",
        lambda *_: {"bucket": "SKIP", "direction": "UP"},
    )

    result = tracker.run_day(DATE, connection=conn)

    assert result == {
        "trade_date": DATE,
        "status": "no_trade",
        "net_rs": 0.0,
        "n_trades": 0,
    }
    row = conn.execute(
        "SELECT status,tier,gap_dir,n_trades,net_rs "
        "FROM paper_strategy_daily WHERE trade_date=?",
        (DATE,),
    ).fetchone()
    assert tuple(row) == ("no_trade", "SKIP", "UP", 0, 0.0)
