"""
Labs strategy runner.
Runs every 60 seconds during market hours.
Processes all active bots: evaluates entry/exit signals, manages paper positions.

PA scheduled task: python labs/engine/strategy_runner.py
"""
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from auth.session_manager import get_kite
from config.labs_config import (
    UNDERLYINGS, MARKET_OPEN, EOD_CUTOFF, MARKET_CLOSE,
    COLLECTOR_INTERVAL_SECS, LOG_DIR,
)
from labs.engine.data_loader import load_spot_1min, load_options_1min, latest_ltp
from labs.engine.resampler import to_5min
from labs.engine.indicator_engine import compute_all
from labs.engine.position_manager import check_exit
from labs.engine.paper_executor import open_position, close_position
from labs.strategies.registry import get_strategy
from storage.db import get_conn, init_db

IST = pytz.timezone("Asia/Kolkata")

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [runner] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"runner_{datetime.now(IST).strftime('%Y-%m-%d')}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def _market_open(now: datetime) -> bool:
    t = now.strftime("%H:%M")
    return MARKET_OPEN <= t <= MARKET_CLOSE


def _past_eod_cutoff(now: datetime) -> bool:
    return now.strftime("%H:%M") >= EOD_CUTOFF


def _next_minute_boundary(now: datetime) -> datetime:
    return (now + timedelta(minutes=1)).replace(second=0, microsecond=0)


def _load_active_bots(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT b.*, p.rsi_period, p.ema_fast_period, p.ema_slow_period, p.sma_period,
               p.rsi_oversold, p.rsi_overbought, p.entry_rules_json, p.exit_rules_json,
               p.spot_target_pts, p.spot_sl_pts, p.ltp_target_pts, p.ltp_sl_pts,
               p.max_trades_per_day, p.allow_reentry, p.session_start, p.session_end,
               p.expiry_mode, p.strike_mode, p.strike_offset_pts, p.hold_same_contract
        FROM bots b JOIN bot_params p ON b.bot_id = p.bot_id
        WHERE b.status = 'active'
    """).fetchall()
    return [dict(r) for r in rows]


def _load_open_position(conn, bot_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM positions WHERE bot_id = ? AND status = 'open'", (bot_id,)
    ).fetchone()
    return dict(row) if row else None


def _daily_trade_count(conn, bot_id: str, trade_date: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE bot_id = ? AND trade_date = ?",
        (bot_id, trade_date),
    ).fetchone()
    return row["n"] if row else 0


def _build_params(bot: dict) -> dict:
    params = dict(bot)
    params["entry_rules"] = json.loads(bot.get("entry_rules_json", "[]"))
    params["exit_rules"]  = json.loads(bot.get("exit_rules_json",  "[]"))
    return params


def _is_5min_bar_close(now: datetime) -> bool:
    """True when we are within 5 seconds of a 5-minute boundary (bar just closed)."""
    minute = now.minute
    second = now.second
    return (minute % 5 == 0) and (second < 5)


def process_bot(bot: dict, trade_date: str, now: datetime, kite, conn) -> None:
    params     = _build_params(bot)
    underlying = bot["underlying"]
    strategy   = get_strategy(bot["strategy_type"])

    session_start = params.get("session_start", "09:20")
    session_end   = params.get("session_end",   "15:15")
    t = now.strftime("%H:%M")

    # Load raw data
    df_1min  = load_spot_1min(underlying, trade_date)
    if df_1min.empty:
        return

    # Resample to 5-min completed bars
    df_5min_raw = to_5min(df_1min, now=now)
    if df_5min_raw.empty:
        return

    # Compute indicators; inject thresholds as helper columns so base.py can read them
    df_5min = compute_all(
        df_5min_raw,
        rsi_period=params["rsi_period"],
        ema_fast=params["ema_fast_period"],
        ema_slow=params["ema_slow_period"],
        sma_period=params["sma_period"],
    )
    df_5min["_rsi_oversold"]   = params["rsi_oversold"]
    df_5min["_rsi_overbought"] = params["rsi_overbought"]

    current_spot = float(df_1min.iloc[-1]["close"])
    options_df   = load_options_1min(underlying, trade_date)

    at_5min_close = _is_5min_bar_close(now)

    # --- Check open position first ---
    position = _load_open_position(conn, bot["bot_id"])

    if position:
        current_ltp = latest_ltp(options_df, position["symbol"])
        if current_ltp is None:
            current_ltp = float(position["entry_ltp"])  # fallback

        # EOD forced exit
        if _past_eod_cutoff(now):
            trade = close_position(position, current_ltp, current_spot, "eod", conn=conn)
            log.info(f"[{bot['bot_id']}] EOD exit  pnl={trade['pnl_pts']:+.1f}pts")
            return

        exit_reason = check_exit(
            position, current_ltp, current_spot,
            df_5min, params, strategy,
            check_indicator=at_5min_close,
        )
        if exit_reason:
            trade = close_position(position, current_ltp, current_spot, exit_reason, conn=conn)
            log.info(f"[{bot['bot_id']}] Exit({exit_reason})  pnl={trade['pnl_pts']:+.1f}pts")
        return

    # --- Flat: evaluate entry (only on 5-min bar close) ---
    if not at_5min_close:
        return
    if not (session_start <= t <= session_end):
        return
    if _past_eod_cutoff(now):
        return

    max_trades = params.get("max_trades_per_day", 3)
    if _daily_trade_count(conn, bot["bot_id"], trade_date) >= max_trades:
        return

    signal_side = strategy.entry_signal(df_5min, params)
    if signal_side is None:
        return

    signal_val = float(df_5min.iloc[-1].get("rsi", 0))
    bar_ts     = str(df_5min.index[-1])

    new_pos = open_position(
        kite, bot, params, signal_side, bar_ts,
        current_spot, signal_val, options_df, conn=conn,
    )
    if new_pos:
        log.info(f"[{bot['bot_id']}] OPEN {signal_side}  spot={current_spot:.0f}  ltp={new_pos['entry_ltp']:.2f}")
    else:
        log.warning(f"[{bot['bot_id']}] Signal={signal_side} but no contract found at spot={current_spot:.0f}")


def run():
    init_db()
    log.info("Labs strategy runner started.")
    kite = get_kite()

    while True:
        now        = datetime.now(IST)
        trade_date = now.strftime("%Y-%m-%d")

        if not _market_open(now):
            if now.strftime("%H:%M") > MARKET_CLOSE:
                log.info("Market closed. Runner exiting.")
                break
            time.sleep(10)
            continue

        conn = get_conn()
        try:
            bots = _load_active_bots(conn)
            for bot in bots:
                try:
                    process_bot(bot, trade_date, now, kite, conn)
                except Exception as exc:
                    log.error(f"[{bot['bot_id']}] Error: {exc}", exc_info=True)
        finally:
            conn.close()

        elapsed    = (datetime.now(IST) - now).total_seconds()
        sleep_for  = max(COLLECTOR_INTERVAL_SECS - elapsed, 1)
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
