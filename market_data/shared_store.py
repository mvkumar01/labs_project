"""Canonical reader for Labs' shared options market-data store.

Recent sessions live as CSV files under ``shared_market_data/live``.  EOD
maintenance converts older sessions to compressed Parquet under
``shared_market_data/archive`` and removes the CSV to save space.  Historical
consumers must therefore treat both locations as one logical store.

This module is deliberately independent of strategy code so capture,
analytics, replay, and pricing all resolve the same immutable session data.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


class SharedMarketDataError(RuntimeError):
    """Raised when a requested shared-market session cannot be loaded."""


def option_source_candidates(
    underlying: str,
    trade_date: str,
    *,
    live_root: Path,
    archive_root: Path,
) -> tuple[Path, ...]:
    """Return supported sources in deterministic priority order."""
    stem = f"{underlying}_options_1min"
    return (
        Path(live_root) / trade_date / f"{stem}.csv",
        Path(archive_root) / trade_date / f"{stem}.parquet.gz",
        Path(archive_root) / trade_date / f"{stem}.parquet",
    )


def resolve_options_source(
    underlying: str,
    trade_date: str,
    *,
    live_root: Path,
    archive_root: Path,
) -> Path:
    """Resolve the live CSV or archived Parquet for one session.

    Missing data is an error, never a no-trade signal.  Callers that publish
    strategy results must let this exception abort before writing to the DB.
    """
    candidates = option_source_candidates(
        underlying, trade_date, live_root=live_root, archive_root=archive_root
    )
    for path in candidates:
        if path.is_file():
            return path
    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        f"No shared options data for {underlying} {trade_date}; searched: {searched}"
    )


def load_options_frame(
    underlying: str,
    trade_date: str,
    *,
    live_root: Path,
    archive_root: Path,
    columns: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Load one session from CSV or archived Parquet without changing rows.

    ``columns`` is applied at the storage reader when possible.  The selected
    source path and storage kind are recorded in ``DataFrame.attrs`` for audit
    output and tests.
    """
    path = resolve_options_source(
        underlying, trade_date, live_root=live_root, archive_root=archive_root
    )
    requested = list(columns) if columns is not None else None
    try:
        if ".parquet" in "".join(path.suffixes).lower():
            frame = pd.read_parquet(path, columns=requested)
            kind = "archive_parquet"
        else:
            frame = pd.read_csv(path, usecols=requested)
            kind = "live_csv"
    except Exception as exc:
        raise SharedMarketDataError(
            f"Unable to read shared options data {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if frame.empty:
        raise SharedMarketDataError(f"Shared options data is empty: {path}")
    frame.attrs["source_path"] = str(path)
    frame.attrs["source_kind"] = kind
    return frame
