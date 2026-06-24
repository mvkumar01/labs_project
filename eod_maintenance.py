"""
Labs EOD maintenance script.
Run at 15:40 IST daily (PA scheduled task).

1. Writes daily_summary rows for all active/paused bots.
2. Compresses today's raw CSVs into data/archive/YYYY-MM-DD.tar.gz.
3. Deletes raw CSVs older than KEEP_DAYS days from data/live/.
"""
import sys
import tarfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from config.labs_config import DATA_DIR, ARCHIVE_DIR, LOG_DIR, SHARED_LIVE_DIR, SHARED_ARCHIVE_DIR
from storage.db import get_conn, init_db

IST      = pytz.timezone("Asia/Kolkata")
KEEP_DAYS = 7


# ── Daily summary ────────────────────────────────────────────────────────────

def write_daily_summaries(trade_date: str, conn) -> int:
    bots = conn.execute(
        "SELECT DISTINCT bot_id, underlying FROM bots WHERE status IN ('active','paused')"
    ).fetchall()

    written = 0
    for b in bots:
        bot_id, underlying = b["bot_id"], b["underlying"]
        trades = conn.execute(
            "SELECT pnl_rs, holding_mins FROM trades WHERE bot_id=? AND trade_date=?",
            (bot_id, trade_date),
        ).fetchall()

        total  = len(trades)
        wins   = sum(1 for t in trades if t["pnl_rs"] > 0)
        losses = total - wins
        pnl_rs = sum(t["pnl_rs"] for t in trades)
        pnl_pts_row = conn.execute(
            "SELECT COALESCE(SUM(pnl_pts),0) as s FROM trades WHERE bot_id=? AND trade_date=?",
            (bot_id, trade_date),
        ).fetchone()
        pnl_pts    = float(pnl_pts_row["s"])
        avg_hold   = (sum(t["holding_mins"] for t in trades) / total) if total else None

        # Max drawdown: running cumulative, track peak, measure max drop
        max_dd = None
        if trades:
            peak = 0.0
            cum  = 0.0
            dd   = 0.0
            for t in trades:
                cum += t["pnl_rs"]
                peak = max(peak, cum)
                dd   = min(dd, cum - peak)
            max_dd = round(dd, 2)

        summary_id = str(uuid.uuid4())
        conn.execute("""
            INSERT OR REPLACE INTO daily_summary
            (summary_id, bot_id, trade_date, underlying, total_trades, winning_trades,
             losing_trades, pnl_pts, pnl_rs, max_drawdown_rs, avg_hold_mins)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (summary_id, bot_id, trade_date, underlying, total,
              wins, losses, round(pnl_pts, 2), round(pnl_rs, 2), max_dd, avg_hold))
        written += 1

    return written


# ── Archive CSVs ─────────────────────────────────────────────────────────────

def archive_today(trade_date: str) -> Path | None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    today_files = list(DATA_DIR.glob(f"{trade_date}_*.csv"))
    if not today_files:
        print(f"[eod] No CSV files found for {trade_date} — nothing to archive.")
        return None

    archive_path = ARCHIVE_DIR / f"{trade_date}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        for f in today_files:
            tar.add(f, arcname=f.name)

    total_mb = archive_path.stat().st_size / 1024 / 1024
    print(f"[eod] Archived {len(today_files)} files → {archive_path} ({total_mb:.2f} MB)")
    return archive_path


# ── Archive shared market CSVs → ZSTD Parquet ────────────────────────────────

def archive_shared_market(trade_date: str) -> int:
    """
    Converts each {UNDERLYING}_options_1min.csv in SHARED_LIVE_DIR/<trade_date>/
    to a ZSTD level-3 Parquet file in SHARED_ARCHIVE_DIR/<trade_date>/,
    then removes the source CSV.  Returns number of files archived.
    """
    day_dir = SHARED_LIVE_DIR / trade_date
    if not day_dir.exists():
        print(f"[eod] No shared market data for {trade_date} — nothing to archive.")
        return 0

    archive_day = SHARED_ARCHIVE_DIR / trade_date
    archive_day.mkdir(parents=True, exist_ok=True)

    count = 0
    for csv_path in sorted(day_dir.glob("*_options_1min.csv")):
        df = pd.read_csv(csv_path, parse_dates=["timestamp"])
        out_path = archive_day / csv_path.name.replace(".csv", ".parquet.zst")
        tmp_path = out_path.with_name(out_path.name + ".tmp")
        df.to_parquet(
            tmp_path,
            compression="zstd",
            compression_level=3,
            index=False,
        )
        # Never delete the only copy until the archive can be read back and its
        # row count agrees with the source CSV.
        verified = pd.read_parquet(tmp_path, columns=["timestamp"])
        if len(verified) != len(df):
            raise RuntimeError(
                f"Archive row-count mismatch for {csv_path}: "
                f"CSV={len(df)} parquet={len(verified)}"
            )
        tmp_path.replace(out_path)
        csv_path.unlink()
        count += 1

    if count:
        # Remove empty date directory
        try:
            day_dir.rmdir()
        except OSError:
            pass
        print(f"[eod] Archived {count} shared market files → {archive_day}")
    return count


# ── Purge old shared market date dirs ────────────────────────────────────────

def purge_old_shared_market(keep_days: int = KEEP_DAYS) -> int:
    """Remove shared live date directories older than keep_days."""
    if not SHARED_LIVE_DIR.exists():
        return 0
    cutoff = datetime.now(IST).date() - timedelta(days=keep_days)
    deleted = 0
    for day_dir in SHARED_LIVE_DIR.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            from datetime import date
            dir_date = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if dir_date < cutoff:
            for f in day_dir.glob("*.csv"):
                archive_stem = f.name.removesuffix(".csv")
                archive_files = [
                    SHARED_ARCHIVE_DIR / day_dir.name / f"{archive_stem}.parquet.zst",
                    SHARED_ARCHIVE_DIR / day_dir.name / f"{archive_stem}.parquet.gz",
                    SHARED_ARCHIVE_DIR / day_dir.name / f"{archive_stem}.parquet",
                ]
                if any(path.is_file() and path.stat().st_size > 0 for path in archive_files):
                    f.unlink()
                else:
                    print(
                        f"[eod] KEEPING {f}: verified archive is missing; "
                        "refusing destructive purge."
                    )
            try:
                day_dir.rmdir()
            except OSError:
                pass
            deleted += 1
    if deleted:
        print(f"[eod] Removed {deleted} old shared market date dirs.")
    return deleted


# ── Purge old raw CSVs ───────────────────────────────────────────────────────

def purge_old_files(keep_days: int = KEEP_DAYS) -> int:
    cutoff = datetime.now(IST).date() - timedelta(days=keep_days)
    deleted = 0
    for f in DATA_DIR.glob("*.csv"):
        # Extract date prefix: YYYY-MM-DD_...
        parts = f.name.split("_")
        if len(parts) < 1:
            continue
        try:
            from datetime import date
            file_date = date.fromisoformat(parts[0])
        except ValueError:
            continue
        if file_date < cutoff:
            f.unlink()
            deleted += 1
    if deleted:
        print(f"[eod] Purged {deleted} raw CSV files older than {keep_days} days.")
    return deleted


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    init_db()
    now        = datetime.now(IST)
    trade_date = now.strftime("%Y-%m-%d")
    print(f"[eod] Starting maintenance for {trade_date}")

    conn = get_conn()
    try:
        with conn:
            n = write_daily_summaries(trade_date, conn)
        print(f"[eod] Wrote {n} daily_summary rows.")
    finally:
        conn.close()

    archive_today(trade_date)
    purge_old_files()
    archive_shared_market(trade_date)
    purge_old_shared_market()
    print("[eod] Done.")


if __name__ == "__main__":
    run()
