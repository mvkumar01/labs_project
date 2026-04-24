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
import uuid
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
from labs.engine.data_loader import (
    load_spot_1min,
    load_spot_1min_with_warmup,
    load_options_1min,
    latest_ltp,
)
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


def _next_minute_boundary(now: datetime) -> datetime:
    """Return the next whole-minute datetime so the loop stays aligned."""
    return (now + timedelta(minutes=1)).replace(second=0, microsecond=0)


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


def _log_signal_debug(
    conn,
    bot: dict,
    ts: datetime,
    bar_close: float,
    signal_type: str,
    skip_reason: str,
    acted: int = 0,
    rsi: float | None = None,
    ema_fast: float | None = None,
    ema_slow: float | None = None,
    sma: float | None = None,
):
    conn.execute(
        """
        INSERT INTO signals (
            signal_id, bot_id, ts, underlying, bar_close,
            rsi, ema_fast, ema_slow, sma, signal_type, acted, skip_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            bot["bot_id"],
            ts.strftime("%Y-%m-%d %H:%M:%S"),
            bot["underlying"],
            bar_close,
            rsi,
            ema_fast,
            ema_slow,
            sma,
            signal_type,
            acted,
            skip_reason,
        ),
    )


def _df_latest_ts(df):
    if df is None or df.empty:
        return None
    if "timestamp" in getattr(df, "columns", []):
        try:
            return df["timestamp"].max()
        except Exception:
            pass
    try:
        if hasattr(df.index, "max"):
            return df.index.max()
    except Exception:
        pass
    return None


def _needs_sma50_warmup(legs: list[dict]) -> bool:
    sma_gate_types = {"spot_gt_sma", "spot_lt_sma", "spot_above_sma_by", "spot_below_sma_by", "sma_gt_spot", "sma_lt_spot"}
    for leg in legs:
        for gate in json.loads(leg.get("entry_gates_json", "[]")):
            if not gate.get("enabled", True):
                continue
            if gate.get("type") in sma_gate_types and int(gate.get("params", {}).get("period", 50)) >= 50:
                return True
    return False


def _log_warmup_if_needed(bot: dict, df_1min, now, required_bars: int = 50) -> bool:
    meta = getattr(df_1min, "attrs", {}).get("warmup", {}) or {}
    bars = len(get_resampled_data(df_1min, "5m", now=now))
    ready = bars >= required_bars
    log.info(
        f"[{bot['bot_id']}:{bot['name']}] warmup symbol={bot['underlying']} "
        f"live_rows={meta.get('live_rows', 0)} historical_rows_loaded={meta.get('historical_rows', 0)} "
        f"completed_5m_bars={bars} SMA50 ready={'YES' if ready else 'NO'}"
    )
    if not ready:
        log.warning(
            f"[{bot['bot_id']}:{bot['name']}] insufficient warmup for SMA50 "
            f"symbol={bot['underlying']} bars_available={bars} bars_required={required_bars}"
        )
    return ready


def _force_eod_square_off(conn, now) -> int:
    if not _past_eod(now):
        return 0

    rows = conn.execute(
        "SELECT * FROM positions WHERE status='open' ORDER BY trade_date, entry_time"
    ).fetchall()
    positions = [dict(r) for r in rows]
    if not positions:
        return 0

    log.info(
        f"EOD square-off triggered open_positions={len(positions)} "
        f"now={now.strftime('%Y-%m-%d %H:%M:%S')} cutoff={EOD_CUTOFF}"
    )

    spot_cache: dict[str, float] = {}
    options_cache: dict[str, object] = {}
    closed = 0

    for position in positions:
        underlying = position["underlying"]
        trade_date = position["trade_date"]

        if underlying not in spot_cache:
            spot_df = load_spot_1min(underlying, trade_date)
            options_cache[underlying] = load_options_1min(underlying, trade_date)
            if not spot_df.empty:
                spot_cache[underlying] = float(spot_df.iloc[-1]["close"])
            else:
                spot_cache[underlying] = float(position["entry_spot"])

        current_spot = spot_cache[underlying]
        options_df = options_cache[underlying]
        current_ltp = latest_ltp(options_df, position["symbol"])
        if current_ltp is None:
            current_ltp = float(position["entry_ltp"])

        try:
            trade = close_position(position, current_ltp, current_spot, "eod", conn=conn)
        except Exception as exc:
            log.error(
                f"EOD square-off failed symbol={position['symbol']} qty={position['qty']} "
                f"exit_price={current_ltp:.2f} err={exc}",
                exc_info=True,
            )
            continue

        log.info(
            f"EOD square-off triggered symbol={position['symbol']} qty={position['qty']} "
            f"exit_price={current_ltp:.2f} pnl={trade['pnl_pts']:+.1f}pts "
            f"gross_rs={trade['pnl_rs']:+.2f} net_rs={trade['net_pnl_rs']:+.2f}"
        )
        closed += 1

    return closed


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
        log.info(
            f"[{bot['bot_id']}:{leg_code}] skip entry: at_5min_close=False "
            f"now={now.strftime('%H:%M:%S')}"
        )
        return

    session_start = bot.get("session_start", "09:20")
    session_end   = bot.get("session_end", "15:15")
    t = now.strftime("%H:%M")
    if not (session_start <= t <= session_end):
        log.info(
            f"[{bot['bot_id']}:{leg_code}] skip entry: session window closed "
            f"now={t} window={session_start}-{session_end}"
        )
        return
    if _past_eod(now):
        log.info(f"[{bot['bot_id']}:{leg_code}] skip entry: past EOD cutoff now={t}")
        return

    max_trades = int(bot.get("max_trades_per_day", 3))
    if _daily_trades(conn, bot["bot_id"], trade_date) >= max_trades:
        log.info(
            f"[{bot['bot_id']}:{leg_code}] skip entry: max trades reached "
            f"trade_date={trade_date} max_trades={max_trades}"
        )
        return

    entry_ok = evaluate_entry(entry_conds, entry_logic, tf_cache)
    log.info(f"[{bot['bot_id']}:{leg_code}] entry evaluated={entry_ok}")
    if not entry_ok:
        with conn:
            _log_signal_debug(
                conn, bot, now, current_spot, side,
                f"{leg_code}:entry_rejected",
                acted=0,
            )
        log.info(f"[{bot['bot_id']}:{leg_code}] skip entry: entry conditions failed")
        return
    gates_ok = evaluate_gates(entry_gates, tf_cache)
    log.info(f"[{bot['bot_id']}:{leg_code}] gates evaluated={gates_ok}")
    if not gates_ok:
        with conn:
            _log_signal_debug(
                conn, bot, now, current_spot, side,
                f"{leg_code}:gates_rejected",
                acted=0,
            )
        log.info(f"[{bot['bot_id']}:{leg_code}] skip entry: entry gates failed")
        return

    params = _build_params(bot)
    bar_ts = str(get_resampled_data(df_1min, "5m", now=now).index[-1])
    log.info(
        f"[{bot['bot_id']}:{leg_code}] open_position() called "
        f"bar_ts={bar_ts} spot={current_spot:.0f} side={side}"
    )
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
        with conn:
            _log_signal_debug(
                conn, bot, now, current_spot, side,
                f"{leg_code}:open_position_rejected",
                acted=0,
            )
        log.warning(f"[{bot['bot_id']}:{leg_code}] open_position() rejected at spot={current_spot:.0f}")


def process_bot_with_legs(bot, legs, trade_date, now, kite, conn):
    underlying = bot["underlying"]
    df_1min    = load_spot_1min_with_warmup(underlying, trade_date, target_completed_bars=70, lookback_days=10)
    options_df  = load_options_1min(underlying, trade_date)
    log.info(
        f"[{bot['bot_id']}:{bot['name']}] active={bot['status']=='active'} "
        f"spot_rows={len(df_1min)} spot_latest={_df_latest_ts(df_1min)} "
        f"options_rows={len(options_df)} options_latest={_df_latest_ts(options_df)}"
    )
    if df_1min.empty:
        log.info(f"[{bot['bot_id']}:{bot['name']}] skip: spot dataframe empty for {trade_date}")
        return

    if _needs_sma50_warmup(legs) and not _log_warmup_if_needed(bot, df_1min, now):
        return

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

    df_1min = load_spot_1min_with_warmup(underlying, trade_date, target_completed_bars=70, lookback_days=10)
    options_df = load_options_1min(underlying, trade_date)
    log.info(
        f"[{bot['bot_id']}:{bot['name']}] active={bot['status']=='active'} "
        f"spot_rows={len(df_1min)} spot_latest={_df_latest_ts(df_1min)} "
        f"options_rows={len(options_df)} options_latest={_df_latest_ts(options_df)}"
    )
    if df_1min.empty:
        log.info(f"[{bot['bot_id']}:{bot['name']}] skip: spot dataframe empty for {trade_date}")
        return

    # If the strategy depends on 50-period SMA gates, keep the runner quiet until
    # enough completed bars are available from the warmup window.
    if not _log_warmup_if_needed(bot, df_1min, now):
        return

    df_5min_raw = to_5min(df_1min, now=now)
    if df_5min_raw.empty:
        log.info(f"[{bot['bot_id']}:{bot['name']}] skip: no completed 5m bars yet")
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
        log.info(
            f"[{bot['bot_id']}] skip entry: at_5min_close=False now={now.strftime('%H:%M:%S')}"
        )
        return
    if not (session_start <= t <= session_end):
        log.info(
            f"[{bot['bot_id']}] skip entry: session window closed now={t} "
            f"window={session_start}-{session_end}"
        )
        return
    if _past_eod(now):
        log.info(f"[{bot['bot_id']}] skip entry: past EOD cutoff now={t}")
        return
    if _daily_trades(conn, bot["bot_id"], trade_date) >= int(params.get("max_trades_per_day", 3)):
        log.info(
            f"[{bot['bot_id']}] skip entry: max trades reached trade_date={trade_date}"
        )
        return

    signal_side = strategy.entry_signal(df_5min, params)
    log.info(f"[{bot['bot_id']}] entry evaluated={signal_side is not None} signal_side={signal_side}")
    if signal_side is None:
        with conn:
            _log_signal_debug(
                conn, bot, now, current_spot, "SKIP",
                "classic:entry_rejected",
                acted=0,
            )
        log.info(f"[{bot['bot_id']}] skip entry: strategy returned None")
        return

    bar_ts     = str(df_5min.index[-1])
    signal_val = float(df_5min.iloc[-1].get("rsi", 0))
    log.info(
        f"[{bot['bot_id']}] open_position() called bar_ts={bar_ts} "
        f"spot={current_spot:.0f} side={signal_side}"
    )
    new_pos = open_position(kite, bot, params, signal_side, bar_ts, current_spot, signal_val, options_df, conn=conn)
    if new_pos:
        log.info(f"[{bot['bot_id']}] OPEN {signal_side} spot={current_spot:.0f} ltp={new_pos['entry_ltp']:.2f}")
    else:
        with conn:
            _log_signal_debug(
                conn, bot, now, current_spot, signal_side,
                "classic:open_position_rejected",
                acted=0,
                rsi=signal_val,
            )
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
    log.info(f"Labs strategy runner started. cwd={Path.cwd()} base={BASE_DIR}")

    while True:
        kite = get_kite()
        now        = datetime.now(IST)
        trade_date = now.strftime("%Y-%m-%d")
        log.info(
            f"loop now={now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"market_open={_market_open(now)} 5m_close={_at_5min_close(now)}"
        )

        conn = get_conn()
        try:
            if _past_eod(now):
                closed = _force_eod_square_off(conn, now)
                if closed:
                    log.info(f"EOD square-off complete closed_positions={closed}")

            if not _market_open(now):
                sleep_for = 60
                log.info(
                    f"Market closed. Runner sleeping {sleep_for}s before retry."
                )
                time.sleep(sleep_for)
                continue

            bots = _load_active_bots(conn)
            log.info(f"active_bots={len(bots)}")
            for bot in bots:
                try:
                    process_bot(bot, trade_date, now, kite, conn)
                except Exception as exc:
                    log.error(f"[{bot['bot_id']}] {exc}", exc_info=True)
        finally:
            conn.close()

        elapsed   = (datetime.now(IST) - now).total_seconds()
        sleep_for = max((_next_minute_boundary(now) - datetime.now(IST)).total_seconds(), 1)
        log.info(f"sleeping {sleep_for:.1f}s")
        time.sleep(sleep_for)
        continue


if __name__ == "__main__":
    run()
