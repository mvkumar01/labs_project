from datetime import datetime

import pandas as pd

from labs.engine.backtest import OptionSnapshotCursor, _latest_ltp_from_frame


def test_option_snapshot_cursor_matches_latest_historical_rows():
    options = pd.DataFrame(
        [
            {
                "timestamp": "2026-06-18 09:15:00",
                "tradingsymbol": "NIFTY24000CE",
                "ltp": 100.0,
            },
            {
                "timestamp": "2026-06-18 09:15:00",
                "tradingsymbol": "NIFTY24000PE",
                "ltp": 110.0,
            },
            {
                "timestamp": "2026-06-18 09:16:00",
                "tradingsymbol": "NIFTY24000CE",
                "ltp": 101.0,
            },
            {
                "timestamp": "2026-06-18 09:17:00",
                "tradingsymbol": "NIFTY24000PE",
                "ltp": 108.0,
            },
        ]
    )
    options["timestamp"] = pd.to_datetime(options["timestamp"])
    cursor = OptionSnapshotCursor(options)

    for minute in ("09:15", "09:16", "09:17"):
        now = datetime.fromisoformat(f"2026-06-18 {minute}:00")
        historical = options[options["timestamp"] <= now]
        snapshot = cursor.at(now)

        expected = historical.drop_duplicates(subset="tradingsymbol", keep="last")
        expected = expected.sort_values("tradingsymbol").reset_index(drop=True)
        actual = snapshot.sort_values("tradingsymbol").reset_index(drop=True)
        pd.testing.assert_frame_equal(actual, expected)

        assert set(snapshot["tradingsymbol"]) == set(historical["tradingsymbol"])
        for symbol in historical["tradingsymbol"].unique():
            assert _latest_ltp_from_frame(snapshot, symbol) == _latest_ltp_from_frame(historical, symbol)


def test_option_snapshot_cursor_reuses_snapshot_when_no_new_rows():
    options = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-06-18 09:15:00"),
                "tradingsymbol": "NIFTY24000CE",
                "ltp": 100.0,
            }
        ]
    )
    cursor = OptionSnapshotCursor(options)

    first = cursor.at(datetime.fromisoformat("2026-06-18 09:15:00"))
    later = cursor.at(datetime.fromisoformat("2026-06-18 09:16:00"))

    assert later is first
    assert _latest_ltp_from_frame(later, "NIFTY24000CE") == 100.0
