from pathlib import Path

from labs.engine.backtest import DataSource, _best_source


DATE = "2026-06-24"


def _source(name: str) -> DataSource:
    root = Path("/shared/archive")
    return DataSource(
        kind="shared_market_data/archive",
        root=root,
        path=root / DATE / name,
        trade_date=DATE,
        underlying="SENSEX",
        data_type="options",
    )


def test_backtest_prefers_zstd_over_legacy_parquet_formats() -> None:
    sources = [
        _source("SENSEX_options_1min.parquet"),
        _source("SENSEX_options_1min.parquet.gz"),
        _source("SENSEX_options_1min.parquet.zst"),
    ]

    selected = _best_source(sources, "SENSEX", DATE, "options")

    assert selected is not None
    assert selected.path.name == "SENSEX_options_1min.parquet.zst"


def test_live_csv_still_beats_zstd_archive() -> None:
    archive = _source("SENSEX_options_1min.parquet.zst")
    live_root = Path("/shared/live")
    live = DataSource(
        kind="shared_market_data/live",
        root=live_root,
        path=live_root / DATE / "SENSEX_options_1min.csv",
        trade_date=DATE,
        underlying="SENSEX",
        data_type="options",
    )

    selected = _best_source([archive, live], "SENSEX", DATE, "options")

    assert selected is live
