"""
Backfill spot 1-min OHLC CSVs from Kite historical_data.

Earlier spot files were written by the buggy spot_collector that wrote
quote["ohlc"] (a daily aggregate) into per-minute rows, leaving
open/high/low frozen and only `close` varying. This script overwrites
those CSVs with proper per-minute candles fetched directly from Kite
historical_data.

Existing files are backed up to `data/live/_corrupt_backup/` before being
overwritten — re-running the script is idempotent.

Kite's minute-interval historical lookback is ~60 days. Days older than
the lookback cannot be reconstructed from Kite; for the NIFTY index the
clean file `data/analytics/nifty_1min_ohlc.csv` covers Jul 2025 - Apr 16
2026 and should be used as the canonical source for that range.

Usage:
    python collector/backfill_spot_ohlc.py                       # all 3 underlyings, 60d
    python collector/backfill_spot_ohlc.py --days 30
    python collector/backfill_spot_ohlc.py --underlying NIFTY    # one index
    python collector/backfill_spot_ohlc.py --date 2026-05-20     # one specific day
"""
from __future__ import annotations

import argparse
import csv
import logging
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import auth.session_manager as session_manager
from config.labs_config import UNDERLYINGS, DATA_DIR

IST = pytz.timezone("Asia/Kolkata")

HEADER = ["timestamp", "open", "high", "low", "close", "volume"]
MARKET_OPEN_HM = (9, 15)
MARKET_CLOSE_HM = (15, 30)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backfill] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def _spot_csv_path(underlying: str, trade_date: str) -> Path:
    return DATA_DIR / f"{trade_date}_{underlying}_spot_1min.csv"


def _trading_days(end_date: datetime, n: int) -> list[datetime]:
    """Return up to n weekday dates ending at end_date (inclusive, IST-naive)."""
    days: list[datetime] = []
    cursor = end_date
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _to_ist_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(IST).replace(tzinfo=None)


def _backup_existing(path: Path, backup_dir: Path) -> None:
    if path.exists():
        backup_path = backup_dir / path.name
        if not backup_path.exists():
            shutil.copy2(path, backup_path)


def backfill_day(kite, underlying: str, trade_date: datetime, backup_dir: Path) -> int:
    cfg = UNDERLYINGS[underlying]
    token = cfg["instrument_token"]
    date_str = trade_date.strftime("%Y-%m-%d")

    from_dt = trade_date.replace(hour=MARKET_OPEN_HM[0],  minute=MARKET_OPEN_HM[1],  second=0, microsecond=0)
    to_dt   = trade_date.replace(hour=MARKET_CLOSE_HM[0], minute=MARKET_CLOSE_HM[1], second=0, microsecond=0)

    try:
        bars = kite.historical_data(
            instrument_token=token,
            from_date=from_dt,
            to_date=to_dt,
            interval="minute",
        )
    except Exception as exc:
        log.error("%s %s: fetch error - %s", date_str, underlying, exc)
        return 0

    if not bars:
        log.info("%s %s: no bars (holiday/weekend?)", date_str, underlying)
        return 0

    path = _spot_csv_path(underlying, date_str)
    _backup_existing(path, backup_dir)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        for bar in bars:
            ts_str = _to_ist_naive(bar["date"]).strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([
                ts_str,
                bar["open"], bar["high"], bar["low"], bar["close"],
                bar.get("volume", 0),
            ])
    log.info("%s %s: wrote %d bars -> %s", date_str, underlying, len(bars), path.name)
    return len(bars)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60,
                        help="Trading-day lookback (max ~60 for Kite minute interval).")
    parser.add_argument("--underlying", type=str, default=None,
                        help="Restrict to one underlying (NIFTY/BANKNIFTY/SENSEX).")
    parser.add_argument("--date", type=str, default=None,
                        help="Backfill a single YYYY-MM-DD date instead of a lookback range.")
    args = parser.parse_args()

    if args.underlying and args.underlying not in UNDERLYINGS:
        log.error("Unknown underlying: %s. Valid: %s", args.underlying, list(UNDERLYINGS.keys()))
        sys.exit(1)
    targets = [args.underlying] if args.underlying else list(UNDERLYINGS.keys())

    backup_dir = DATA_DIR / "_corrupt_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    kite = session_manager.get_kite()

    if args.date:
        try:
            single = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            log.error("Bad --date format. Expected YYYY-MM-DD."); sys.exit(1)
        days = [single]
    else:
        today_ist = datetime.now(IST).replace(tzinfo=None)
        days = _trading_days(today_ist, args.days)

    log.info("Backfilling %d day(s) x %d underlying(s)", len(days), len(targets))
    log.info("Range: %s -> %s", days[0].date(), days[-1].date())
    log.info("Backups: %s", backup_dir)

    total = 0
    for trade_date in days:
        for underlying in targets:
            total += backfill_day(kite, underlying, trade_date, backup_dir)
            # Light throttle to stay clear of Kite historical rate limits (3 req/s).
            time.sleep(0.4)

    log.info("Done. %d total bars written.", total)


if __name__ == "__main__":
    main()
