from pathlib import Path

import pandas as pd
import pytest

from market_data.shared_store import load_options_frame, resolve_options_source


DATE = "2026-06-01"
NAME = "NIFTY_options_1min"


def _frame(ltp: float) -> pd.DataFrame:
    return pd.DataFrame(
        [{
            "timestamp": "2026-06-01 09:20:00",
            "underlying": "NIFTY",
            "tradingsymbol": "NIFTY2660223500CE",
            "strike": 23500,
            "option_type": "CE",
            "expiry": "26602",
            "ltp": ltp,
            "bid": ltp - 0.1,
            "ask": ltp + 0.1,
            "oi": 1000,
            "volume": 10,
            "spot": 23510,
        }]
    )


def test_live_csv_has_priority_over_archive(tmp_path: Path) -> None:
    live = tmp_path / "live"
    archive = tmp_path / "archive"
    (live / DATE).mkdir(parents=True)
    (archive / DATE).mkdir(parents=True)
    _frame(100).to_csv(live / DATE / f"{NAME}.csv", index=False)
    _frame(200).to_parquet(archive / DATE / f"{NAME}.parquet.gz", index=False)

    result = load_options_frame(
        "NIFTY", DATE, live_root=live, archive_root=archive
    )

    assert result.iloc[0]["ltp"] == 100
    assert result.attrs["source_kind"] == "live_csv"


def test_archived_parquet_is_transparent_fallback(tmp_path: Path) -> None:
    live = tmp_path / "live"
    archive = tmp_path / "archive"
    (archive / DATE).mkdir(parents=True)
    expected = _frame(200)
    expected.to_parquet(
        archive / DATE / f"{NAME}.parquet.zst",
        compression="zstd",
        compression_level=3,
        index=False,
    )

    result = load_options_frame(
        "NIFTY", DATE, live_root=live, archive_root=archive
    )

    assert len(result) == len(expected)
    assert result.iloc[0]["ltp"] == 200
    assert result.attrs["source_kind"] == "archive_parquet"
    assert resolve_options_source(
        "NIFTY", DATE, live_root=live, archive_root=archive
    ).name.endswith(".parquet.zst")


def test_zstd_archive_has_priority_over_legacy_gzip(tmp_path: Path) -> None:
    live = tmp_path / "live"
    archive = tmp_path / "archive"
    (archive / DATE).mkdir(parents=True)
    _frame(200).to_parquet(
        archive / DATE / f"{NAME}.parquet.gz", compression="gzip", index=False
    )
    _frame(300).to_parquet(
        archive / DATE / f"{NAME}.parquet.zst",
        compression="zstd",
        compression_level=3,
        index=False,
    )

    result = load_options_frame(
        "NIFTY", DATE, live_root=live, archive_root=archive
    )

    assert result.iloc[0]["ltp"] == 300


def test_missing_session_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No shared options data"):
        load_options_frame(
            "NIFTY",
            DATE,
            live_root=tmp_path / "live",
            archive_root=tmp_path / "archive",
        )
