"""
Labs strategy runner.
Runs every 60 seconds during market hours.

Two evaluation paths:
  - Leg-based  (bots with rows in lab_bot_legs) — uses condition_evaluator
  - Classic    (bots without legs)              — uses Strategy class registry
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
from labs.engine.resampler import get_resampled_data, to_5min
from labs.engine.indicator_engine import compute_all
from labs.engine.position_manager import check_exit
from labs.engine.paper_executor import open_position, close_position
from labs.engine.condition_evaluator import TFCache, evaluate_entry, evaluate_gates, evaluate_exits
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

LEG_SIDES = {"C1": "CE", "C2": "CE", "P1": "PE", "P2": "PE"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _market_open(now): return MARKET_OPEN <= now.strftime("%H:%M") <= MARKET_CLOSE
def _past_eod(now):    return now.strftime("%H:%M") >= EOD_CUTOFF
def _at_5min_close(now): return (now.minute % 5 == 0) and (now.second < 5)


def _load_active_bots(conn):
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


def _load_legs(conn, bot_id):
    rows = conn.execute(
        "SELECT * FROM lab_bot_legs WHERE bot_id = ? AND is_enabled = 1", (bot_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _open_position_for_leg(conn, bot_id, leg_code):
    row = conn.execute(
        "SELECT * FROM positions WHERE bot_id=? AND leg_code=? AND status='open'",
        (bot_id, leg_code),
    ).fetchone()
    return dict(row) if row else None


def _open_position_classic(conn, bot_id):
    row = conn.execute(
        "SELECT * FROM positions WHERE bot_id=? AND status='open'", (bot_id,)
    ).fetchone()
    return dict(row) if row else None


def _daily_trades(conn, bot_id, trade_date):
    r = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE bot_id=? AND trade_date=?",
        (bot_id, trade_date),
    ).fetchone()
    return r["n"] if r else 0


def _build_params(bot):
    p = dict(bot)
    p["entry_rules"] = json.loads(bot.get("entry_rules_json", "[]"))
    p["exit_rules"]  = json.loads(bot.get("exit_rules_json",  "[]"))
    return p


# ── Leg-based processing ──────────────────────────────────────────────────────

def _process_leg(
    leg, bot, trade_date, now,
    tf_cache, df_1min, options_df,
    current_spot, kite, conn,
):
    leg_code   = leg["leg_code"]
    side       = LEG_SIDES[leg_code]
    entry_conds = json.loads(leg["entry_conditions_json"])
    entry_gates = json.loads(leg["entry_gates_json"])
    exit_conds  = json.loads(leg["exit_conditions_json"])
    sl_conds    = json.loads(leg["stoploss_conditions_json"])
    entry_logic = leg.get("entry_logic", "AND")

    position = _open_position_for_leg(conn, bot["bot_id"], leg_code)

    if position:
        current_ltp = latest_ltp(options_df, position["symbol"])
        if current_ltp is None:
            current_ltp = float(position["entry_ltp"])

        if _past_eod(now):
            trade = close_position(position, current_ltp, current_spot, "eod", conn=conn)
            log.info(f"[{bot['bot_id']}:{leg_code}] EOD exit pnl={trade['pnl_pts']:+.1f}pts")
            return

        # Stoploss checked first
        sl_hit = evaluate_exits(sl_conds, tf_cache, position, current_ltp, current_spot)
        if sl_hit:
            trade = close_position(position, current_ltp, current_spot, f"sl:{sl_hit}", conn=conn)
            log.info(f"[{bot['bot_id']}:{leg_code}] SL({sl_hit}) pnl={trade['pnl_pts']:+.1f}pts")
            return

        # Then exit conditions (only on bar close for indicator exits)
        if _at_5min_close(now):
            ex_hit = evaluate_exits(exit_conds, tf_cache, position, current_ltp, current_spot)
            if ex_hit:
                trade = close_position(position, current_ltp, current_spot, f"exit:{ex_hit}", conn=conn)
                log.info(f"[{bot['bot_id']}:{leg_code}] Exit({ex_hit}) pnl={trade['pnl_pts']:+.1f}pts")
        return

    # Flat — only enter on 5-min bar close
    if not _at_5min_close(now):
        return

    session_start = bot.get("session_start", "09:20")
    session_end   = bot.get("session_end", "15:15")
    t = now.strftime("%H:%M")
    if not (session_start <= t <= session_end):
        return
    if _past_eod(now):
        return

    max_trades = int(bot.get("max_trades_per_day", 3))
    if _daily_trades(conn, bot["bot_id"], trade_date) >= max_trades:
        return

    if not evaluate_entry(entry_conds, entry_logic, tf_cache):
        return
    if not evaluate_gates(entry_gates, tf_cache):
        return

    params = _build_params(bot)
    bar_ts = str(get_resampled_data(df_1min, "5m", now=now).index[-1])
    new_pos = open_position(
        kite, {**bot, "underlying": bot["underlying"]}, params, side,
        bar_ts, current_spot, None, options_df, conn=conn,
    )
    if new_pos:
        # Patch leg_code into the just-inserted position row
        conn.execute(
            "UPDATE positions SET leg_code=? WHERE position_id=?",
            (leg_code, new_pos["position_id"])
        )
        conn.commit()
        log.info(f"[{bot['bot_id']}:{leg_code}] OPEN {side} spot={current_spot:.0f} ltp={new_pos['entry_ltp']:.2f}")
    else:
        log.warning(f"[{bot['bot_id']}:{leg_code}] Signal but no contract at spot={current_spot:.0f}")


def process_bot_with_legs(bot, legs, trade_date, now, kite, conn):
    underlying = bot["underlying"]
    df_1min    = load_spot_1min(underlying, trade_date)
    if df_1min.empty:
        return

    options_df   = load_options_1min(underlying, trade_date)
    current_spot = float(df_1min.iloc[-1]["close"])
    tf_cache     = TFCache(df_1min, now)

    for leg in legs:
        try:
            _process_leg(leg, bot, trade_date, now, tf_cache, df_1min,
                         options_df, current_spot, kite, conn)
        except Exception as exc:
            log.error(f"[{bot['bot_id']}:{leg['leg_code']}] {exc}", exc_info=True)


# ── Classic processing (unchanged from original) ──────────────────────────────

def process_bot_classic(bot, trade_date, now, kite, conn):
    params     = _build_params(bot)
    underlying = bot["underlying"]
    strategy   = get_strategy(bot["strategy_type"])

    session_start = params.get("session_start", "09:20")
    session_end   = params.get("session_end",   "15:15")
    t = now.strftime("%H:%M")

    df_1min = load_spot_1min(underlying, trade_date)
    if df_1min.empty:
        return

    df_5min_raw = to_5min(df_1min, now=now)
    if df_5min_raw.empty:
        return

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
    at_5min      = _at_5min_close(now)

    position = _open_position_classic(conn, bot["bot_id"])

    if position:
        current_ltp = latest_ltp(options_df, position["symbol"]) or float(position["entry_ltp"])
        if _past_eod(now):
            trade = close_position(position, current_ltp, current_spot, "eod", conn=conn)
            log.info(f"[{bot['bot_id']}] EOD exit pnl={trade['pnl_pts']:+.1f}pts")
            return
        exit_reason = check_exit(position, current_ltp, current_spot, df_5min, params, strategy, check_indicator=at_5min)
        if exit_reason:
            trade = close_position(position, current_ltp, current_spot, exit_reason, conn=conn)
            log.info(f"[{bot['bot_id']}] Exit({exit_reason}) pnl={trade['pnl_pts']:+.1f}pts")
        return

    if not at_5min:
        return
    if not (session_start <= t <= session_end):
        return
    if _past_eod(now):
        return
    if _daily_trades(conn, bot["bot_id"], trade_date) >= int(params.get("max_trades_per_day", 3)):
        return

    signal_side = strategy.entry_signal(df_5min, params)
    if signal_side is None:
        return

    bar_ts     = str(df_5min.index[-1])
    signal_val = float(df_5min.iloc[-1].get("rsi", 0))
    new_pos = open_position(kite, bot, params, signal_side, bar_ts, current_spot, signal_val, options_df, conn=conn)
    if new_pos:
        log.info(f"[{bot['bot_id']}] OPEN {signal_side} spot={current_spot:.0f} ltp={new_pos['entry_ltp']:.2f}")
    else:
        log.warning(f"[{bot['bot_id']}] Signal={signal_side} but no contract at spot={current_spot:.0f}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def process_bot(bot, trade_date, now, kite, conn):
    """Route to leg-based or classic path based on whether legs are configured."""
    legs = _load_legs(conn, bot["bot_id"])
    if legs:
        process_bot_with_legs(bot, legs, trade_date, now, kite, conn)
    else:
        process_bot_classic(bot, trade_date, now, kite, conn)


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
            for bot in _load_active_bots(conn):
                try:
                    process_bot(bot, trade_date, now, kite, conn)
                except Exception as exc:
                    log.error(f"[{bot['bot_id']}] {exc}", exc_info=True)
        finally:
            conn.close()

        elapsed   = (datetime.now(IST) - now).total_seconds()
        time.sleep(max(COLLECTOR_INTERVAL_SECS - elapsed, 1))


if __name__ == "__main__":
    run()
