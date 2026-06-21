from pathlib import Path

import pandas as pd
import pytest

from labs.engine import paper_strategy_tracker as tracker


def _write_option_rows(root: Path) -> None:
    day_dir = root / "2026-06-01"
    day_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            # nearest expiry (26602 = June 2) at exact 5-min marks
            {"timestamp": "2026-06-01 09:20:00", "strike": 23550,
             "option_type": "PE", "expiry": "26602", "ltp": 100.0},
            {"timestamp": "2026-06-01 09:24:00", "strike": 23550,
             "option_type": "PE", "expiry": "26602", "ltp": 110.0},
            {"timestamp": "2026-06-01 09:25:00", "strike": 23550,
             "option_type": "PE", "expiry": "26602", "ltp": 120.0},
            # next expiry (26609 = June 9)
            {"timestamp": "2026-06-01 09:20:00", "strike": 23550,
             "option_type": "PE", "expiry": "26609", "ltp": 200.0},
            {"timestamp": "2026-06-01 09:24:00", "strike": 23550,
             "option_type": "PE", "expiry": "26609", "ltp": 210.0},
            {"timestamp": "2026-06-01 09:25:00", "strike": 23550,
             "option_type": "PE", "expiry": "26609", "ltp": 220.0},
        ]
    ).to_csv(day_dir / "NIFTY_options_1min.csv", index=False)


# ── Price book tests ──────────────────────────────────────────────────────────

def test_build_price_books_nearest_expiry_at_exact_alpha_mark(
    tmp_path, monkeypatch
) -> None:
    _write_option_rows(tmp_path)
    monkeypatch.setattr(tracker, "SHARED_LIVE_DIR", tmp_path)

    books = tracker._build_price_books("2026-06-01")
    near = books["nearest_weekly"]
    assert near is not None
    assert tracker._premium(near["prices"], "2026-06-01T09:20:00+05:30", 23550, "pe") == 100.0
    assert tracker._premium(near["prices"], "2026-06-01T09:25:00+05:30", 23550, "pe") == 120.0


def test_build_price_books_next_expiry_at_exact_alpha_mark(
    tmp_path, monkeypatch
) -> None:
    _write_option_rows(tmp_path)
    monkeypatch.setattr(tracker, "SHARED_LIVE_DIR", tmp_path)

    books = tracker._build_price_books("2026-06-01")
    nxt = books["next_weekly"]
    assert nxt is not None
    assert nxt["expiry_code"] == "26609"
    assert tracker._premium(nxt["prices"], "2026-06-01T09:20:00+05:30", 23550, "pe") == 200.0
    assert tracker._premium(nxt["prices"], "2026-06-01T09:25:00+05:30", 23550, "pe") == 220.0


def test_build_price_books_never_relabels_off_grid_row(
    tmp_path, monkeypatch
) -> None:
    _write_option_rows(tmp_path)
    monkeypatch.setattr(tracker, "SHARED_LIVE_DIR", tmp_path)

    books = tracker._build_price_books("2026-06-01")
    near_prices = books["nearest_weekly"]["prices"]
    assert all("09:24:00" not in key[0] for key in near_prices)
    assert set(near_prices.values()) == {100.0, 120.0}


def test_build_price_books_same_expiry_code_for_entry_and_exit(
    tmp_path, monkeypatch
) -> None:
    """All rows in a book belong to a single expiry code — entry and exit never mix."""
    _write_option_rows(tmp_path)
    monkeypatch.setattr(tracker, "SHARED_LIVE_DIR", tmp_path)

    books = tracker._build_price_books("2026-06-01")
    assert books["nearest_weekly"]["expiry_code"] == "26602"
    assert books["next_weekly"]["expiry_code"] == "26609"


# ── _price_trade strike and ATM tests ────────────────────────────────────────

@pytest.mark.parametrize(
    ("pos", "offset", "expected_strike", "option_type"),
    [
        ("call", 0,   23550, "ce"),   # ATM CALL
        ("put",  0,   23550, "pe"),   # ATM PUT
        ("call", 200, 23350, "ce"),   # ITM 200 CALL
        ("put",  200, 23750, "pe"),   # ITM 200 PUT
    ],
)
def test_price_trade_strike_selection(
    pos: str, offset: int, expected_strike: int, option_type: str
) -> None:
    entry_ts = "2026-06-01T09:20:00+05:30"
    exit_ts  = "2026-06-01T09:25:00+05:30"
    prices = {
        (entry_ts, expected_strike, option_type): 300.0,
        (exit_ts,  expected_strike, option_type): 320.0,
    }
    trade = {
        "pos": pos,
        "entry_spot": 23560.0,
        "exit_spot": 23580.0,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "pnl": 20.0,
        "entry_rule": "test",
        "reason": "test",
    }
    priced = tracker._price_trade(prices, trade, offset)
    assert priced["strike"] == expected_strike


def test_price_trade_default_offset_is_itm200() -> None:
    """_price_trade() without explicit offset uses ITM_DISTANCE=200."""
    entry_ts = "2026-06-01T09:20:00+05:30"
    exit_ts  = "2026-06-01T09:25:00+05:30"
    prices = {
        (entry_ts, 23350, "ce"): 300.0,
        (exit_ts,  23350, "ce"): 320.0,
    }
    trade = {
        "pos": "call",
        "entry_spot": 23560.0,
        "exit_spot": 23580.0,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "pnl": 20.0,
        "entry_rule": "test",
        "reason": "test",
    }
    priced = tracker._price_trade(prices, trade)
    assert priced["strike"] == 23350


def test_price_trade_rejects_missing_exact_ltp() -> None:
    trade = {
        "pos": "put",
        "entry_spot": 23560.0,
        "exit_spot": 23520.0,
        "entry_ts": "2026-06-01T09:20:00+05:30",
        "exit_ts": "2026-06-01T09:25:00+05:30",
    }
    with pytest.raises(ValueError, match="Missing exact LTP"):
        tracker._price_trade({}, trade)
