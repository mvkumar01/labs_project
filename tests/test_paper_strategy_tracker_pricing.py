from pathlib import Path

import pandas as pd
import pytest

from labs.engine import paper_strategy_tracker as tracker


def _write_option_rows(root: Path) -> None:
    day_dir = root / "2026-06-01"
    day_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {"timestamp": "2026-06-01 09:20:00", "strike": 23550,
             "option_type": "PE", "expiry": "26602", "ltp": 100.0},
            {"timestamp": "2026-06-01 09:24:00", "strike": 23550,
             "option_type": "PE", "expiry": "26602", "ltp": 110.0},
            {"timestamp": "2026-06-01 09:25:00", "strike": 23550,
             "option_type": "PE", "expiry": "26602", "ltp": 120.0},
            {"timestamp": "2026-06-01 09:20:00", "strike": 23550,
             "option_type": "PE", "expiry": "26609", "ltp": 200.0},
            {"timestamp": "2026-06-01 09:24:00", "strike": 23550,
             "option_type": "PE", "expiry": "26609", "ltp": 210.0},
            {"timestamp": "2026-06-01 09:25:00", "strike": 23550,
             "option_type": "PE", "expiry": "26609", "ltp": 220.0},
        ]
    ).to_csv(day_dir / "NIFTY_options_1min.csv", index=False)


def test_premium_lookup_uses_nearest_expiry_at_exact_alpha_mark(
    tmp_path, monkeypatch
) -> None:
    _write_option_rows(tmp_path)
    monkeypatch.setattr(tracker, "SHARED_LIVE_DIR", tmp_path)

    lookup = tracker._premium_lookup("2026-06-01")

    assert tracker._premium(
        lookup, "2026-06-01T09:20:00+05:30", 23550, "pe"
    ) == 100.0
    assert tracker._premium(
        lookup, "2026-06-01T09:25:00+05:30", 23550, "pe"
    ) == 120.0


def test_premium_lookup_never_relabels_later_bucket_price_as_signal_mark(
    tmp_path, monkeypatch
) -> None:
    _write_option_rows(tmp_path)
    monkeypatch.setattr(tracker, "SHARED_LIVE_DIR", tmp_path)

    lookup = tracker._premium_lookup("2026-06-01")

    assert all("09:24:00" not in key[0] for key in lookup)
    assert set(lookup.values()) == {100.0, 120.0}


def test_price_trade_rejects_missing_exact_ltp() -> None:
    trade = {
        "pos": "put",
        "entry_spot": 23560.0,
        "exit_spot": 23520.0,
        "entry_ts": "2026-06-01T09:20:00+05:30",
        "exit_ts": "2026-06-01T09:25:00+05:30",
    }

    with pytest.raises(ValueError, match="Missing exact nearest-expiry LTP"):
        tracker._price_trade({}, trade)


@pytest.mark.parametrize(
    ("pos", "expected_strike", "option_type"),
    [
        ("call", 23350, "ce"),
        ("put", 23750, "pe"),
    ],
)
def test_price_trade_uses_200_point_itm_contract(
    pos: str, expected_strike: int, option_type: str
) -> None:
    entry_ts = "2026-06-01T09:20:00+05:30"
    exit_ts = "2026-06-01T09:25:00+05:30"
    lookup = {
        (entry_ts, expected_strike, option_type): 300.0,
        (exit_ts, expected_strike, option_type): 320.0,
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

    priced = tracker._price_trade(lookup, trade)

    assert priced["strike"] == expected_strike
