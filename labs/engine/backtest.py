"""
Historical Labs bot backtesting.

Backtests are deliberately in-memory: no rows are written to live positions,
trades, signals, or daily_summary.
"""
from __future__ import annotations

import json
import logging
import math
import re
import tarfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from collector.instruments import _round_strike
from config.labs_config import (
    ARCHIVE_DIR,
    DATA_DIR,
    EOD_CUTOFF,
    MARKET_CLOSE,
    MARKET_OPEN,
    SHARED_ARCHIVE_DIR,
    SHARED_LIVE_DIR,
    UNDERLYINGS,
)
from labs.engine.condition_evaluator import TFCache, evaluate_entry, evaluate_exits, evaluate_gates
from labs.engine.indicator_engine import compute_all
from labs.engine.position_manager import check_exit
from labs.engine.resampler import get_resampled_data, to_5min
from labs.services.bot_service import get_bot, get_legs, list_bots
from labs.strategies.registry import get_strategy

log = logging.getLogger(__name__)

LEG_SIDES = {"C1": "CE", "C2": "CE", "P1": "PE", "P2": "PE"}
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SMA50_GATE_TYPES = {
    "spot_gt_sma",
    "spot_lt_sma",
    "spot_above_sma_by",
    "spot_below_sma_by",
    "sma_gt_spot",
    "sma_lt_spot",
}


@dataclass(frozen=True)
class DataSource:
    kind: str
    root: Path
    path: Path
    trade_date: str
    underlying: str
    data_type: str
    member: str | None = None

    @property
    def label(self) -> str:
        if self.member:
            return f"{self.path}!{self.member}"
        return str(self.path)


class OptionSnapshotCursor:
    """Maintain the latest known row per option contract as time advances.

    Backtest consumers only need each contract's most recent row at ``now``.
    Re-filtering the complete intraday options history for every spot minute
    made a multi-day request quadratic in the number of snapshots.
    """

    def __init__(self, options_df: pd.DataFrame):
        self._options = options_df
        self._timestamps = pd.DatetimeIndex(options_df["timestamp"])
        self._cursor = 0
        self._latest = options_df.iloc[0:0].copy()

    def at(self, now: datetime) -> pd.DataFrame:
        end = int(self._timestamps.searchsorted(now, side="right"))
        if end <= self._cursor:
            return self._latest

        new_rows = self._options.iloc[self._cursor:end]
        if self._latest.empty:
            combined = new_rows.copy()
        else:
            combined = pd.concat([self._latest, new_rows], ignore_index=True)
        self._latest = (
            combined.drop_duplicates(subset="tradingsymbol", keep="last")
            .reset_index(drop=True)
        )
        self._cursor = end
        return self._latest


def get_backtest_bots() -> list[dict]:
    return [{"bot_id": b["bot_id"], "name": b["name"], "underlying": b["underlying"]} for b in list_bots()]


def scan_data_ranges() -> dict[str, Any]:
    sources = _scan_sources()
    result: dict[str, Any] = {"underlyings": {}, "sources": _source_roots()}

    for underlying in UNDERLYINGS:
        spot_days = {s.trade_date for s in sources if s.underlying == underlying and s.data_type == "spot"}
        option_days = {s.trade_date for s in sources if s.underlying == underlying and s.data_type == "options"}
        all_days = sorted(spot_days | option_days)
        # Spot is also derivable from the `spot` column inside options files.
        effective_spot_days = spot_days | option_days
        result["underlyings"][underlying] = {
            "first_date": all_days[0] if all_days else None,
            "last_date": all_days[-1] if all_days else None,
            "trading_days": len(all_days),
            "spot_exists": bool(effective_spot_days),
            "options_exists": bool(option_days),
            "sma50_warmup_possible": bool(effective_spot_days),
            "spot_days": len(effective_spot_days),
            "option_days": len(option_days),
        }

    return result


def run_backtest(
    bot_id: str,
    underlying: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    sources = _scan_sources()
    bot = get_bot(bot_id)
    if bot is None:
        return {"ok": False, "error": "Bot not found"}
    if underlying not in UNDERLYINGS:
        return {"ok": False, "error": "Unsupported underlying"}

    bot = {**bot, "underlying": underlying}
    legs = [l for l in get_legs(bot_id) if int(l.get("is_enabled", 1))]
    params = _build_params(bot)
    needs_warmup = _needs_sma50_warmup(legs) if legs else True

    log.info(
        "[backtest] selected bot=%s underlying=%s range=%s..%s",
        bot_id,
        underlying,
        start_date,
        end_date,
    )

    trades: list[dict] = []
    skipped_days: list[dict] = []
    data_files: list[str] = []

    for trade_date in _date_range(start_date, end_date):
        spot_source = _best_source(sources, underlying, trade_date, "spot")
        options_source = _best_source(sources, underlying, trade_date, "options")
        if spot_source:
            data_files.append(spot_source.label)
        if options_source:
            data_files.append(options_source.label)

        if options_source is None:
            skipped_days.append({"date": trade_date, "reason": "No options data for selected date"})
            log.warning("[backtest] skip %s %s: No options data for selected date", underlying, trade_date)
            continue

        options_df = _normalize_options(_load_source(options_source, parse_dates=["timestamp"]), underlying)

        if spot_source is not None:
            day_spot = _normalize_spot(_load_source(spot_source, parse_dates=["timestamp"]))
        else:
            # Shared-store format has no separate spot file — derive spot from the
            # `spot` column embedded in the options snapshots.
            day_spot = _spot_from_options(options_df)
            if day_spot.empty:
                skipped_days.append({"date": trade_date, "reason": "No spot data"})
                log.warning("[backtest] skip %s %s: No spot data", underlying, trade_date)
                continue
        warmup_spot = _load_warmup_spot(sources, underlying, trade_date, day_spot)

        log.info(
            "[backtest] %s %s files spot=%s options=%s rows spot=%s warmup=%s options=%s",
            underlying,
            trade_date,
            spot_source.label if spot_source else f"{options_source.label} (spot col)",
            options_source.label,
            len(day_spot),
            len(warmup_spot),
            len(options_df),
        )

        if day_spot.empty:
            skipped_days.append({"date": trade_date, "reason": "No spot data"})
            continue
        if options_df.empty:
            skipped_days.append({"date": trade_date, "reason": "No options data for selected date"})
            continue

        day_trades, day_skips = _run_day(bot, params, legs, trade_date, day_spot, warmup_spot, options_df, needs_warmup)
        trades.extend(day_trades)
        skipped_days.extend(day_skips)
        log.info("[backtest] %s %s trades_generated=%s", underlying, trade_date, len(day_trades))

    summary = _summarize(trades)
    day_pnl = _day_wise_pnl(trades)
    return {
        "ok": True,
        "bot": {"bot_id": bot_id, "name": bot["name"], "underlying": underlying},
        "range": {"start_date": start_date, "end_date": end_date},
        "summary": summary,
        "day_wise_pnl": day_pnl,
        "trades": trades,
        "skipped_days": skipped_days,
        "logs": {
            "data_files_found": sorted(set(data_files)),
            "trades_generated": len(trades),
        },
    }


def _run_day(
    bot: dict,
    params: dict,
    legs: list[dict],
    trade_date: str,
    day_spot: pd.DataFrame,
    warmup_spot: pd.DataFrame,
    options_df: pd.DataFrame,
    needs_warmup: bool,
) -> tuple[list[dict], list[dict]]:
    trades: list[dict] = []
    skipped: list[dict] = []
    open_positions: dict[str, dict] = {}
    warmup_failed = False
    ever_ready = False
    option_snapshots = OptionSnapshotCursor(options_df)

    for now in day_spot.index:
        t = now.strftime("%H:%M")
        if t < MARKET_OPEN or t > MARKET_CLOSE:
            continue

        spot_until_now = warmup_spot[warmup_spot.index <= now]
        if spot_until_now.empty:
            continue
        current_spot = float(spot_until_now.iloc[-1]["close"])
        options_until_now = option_snapshots.at(now)
        tf_cache = TFCache(spot_until_now, now)

        if needs_warmup:
            ready = len(get_resampled_data(spot_until_now, "5m", now=now)) >= 50
            ever_ready = ever_ready or ready
            if not ready:
                warmup_failed = True
                continue

        if legs:
            for leg in legs:
                _process_leg_backtest(
                    bot,
                    params,
                    leg,
                    trade_date,
                    now,
                    tf_cache,
                    spot_until_now,
                    options_until_now,
                    current_spot,
                    open_positions,
                    trades,
                    skipped,
                )
        else:
            _process_classic_backtest(
                bot,
                params,
                trade_date,
                now,
                spot_until_now,
                options_until_now,
                current_spot,
                open_positions,
                trades,
                skipped,
            )

    if needs_warmup and warmup_failed and not ever_ready:
        skipped.append({"date": trade_date, "reason": "Insufficient SMA50 warmup"})

    if open_positions:
        exit_spot = float(day_spot.iloc[-1]["close"])
        for key, position in list(open_positions.items()):
            exit_ltp = _latest_ltp_at(options_df, position["symbol"], day_spot.index[-1])
            if exit_ltp is None:
                skipped.append({"date": trade_date, "reason": "No valid contract LTP", "symbol": position["symbol"]})
                exit_ltp = float(position["entry_ltp"])
            trades.append(_close_backtest_position(position, exit_ltp, exit_spot, "eod", day_spot.index[-1]))
            del open_positions[key]

    return trades, skipped


def _process_leg_backtest(
    bot: dict,
    params: dict,
    leg: dict,
    trade_date: str,
    now: datetime,
    tf_cache: TFCache,
    spot_until_now: pd.DataFrame,
    options_until_now: pd.DataFrame,
    current_spot: float,
    open_positions: dict[str, dict],
    trades: list[dict],
    skipped: list[dict],
) -> None:
    leg_code = leg["leg_code"]
    side = LEG_SIDES[leg_code]
    position = open_positions.get(leg_code)

    if position:
        current_ltp = _latest_ltp_from_frame(options_until_now, position["symbol"])
        if current_ltp is None:
            skipped.append({"date": trade_date, "reason": "No valid contract LTP", "symbol": position["symbol"]})
            current_ltp = float(position["entry_ltp"])
        if now.strftime("%H:%M") >= EOD_CUTOFF:
            trades.append(_close_backtest_position(position, current_ltp, current_spot, "eod", now))
            del open_positions[leg_code]
            return

        sl_hit = evaluate_exits(_json_list(leg["stoploss_conditions_json"]), tf_cache, position, current_ltp, current_spot)
        if sl_hit:
            trades.append(_close_backtest_position(position, current_ltp, current_spot, f"sl:{sl_hit}", now))
            del open_positions[leg_code]
            return

        if _at_5min_close(now):
            ex_hit = evaluate_exits(_json_list(leg["exit_conditions_json"]), tf_cache, position, current_ltp, current_spot)
            if ex_hit:
                trades.append(_close_backtest_position(position, current_ltp, current_spot, f"exit:{ex_hit}", now))
                del open_positions[leg_code]
        return

    if not _can_enter(bot, trade_date, now, trades):
        return
    if not evaluate_entry(_json_list(leg["entry_conditions_json"]), leg.get("entry_logic", "AND"), tf_cache):
        return
    if not evaluate_gates(_json_list(leg["entry_gates_json"]), tf_cache):
        return

    position = _open_backtest_position(bot, params, side, leg_code, trade_date, now, current_spot, options_until_now)
    if position:
        open_positions[leg_code] = position
    else:
        skipped.append({"date": trade_date, "reason": "No valid contract LTP", "leg_code": leg_code})


def _process_classic_backtest(
    bot: dict,
    params: dict,
    trade_date: str,
    now: datetime,
    spot_until_now: pd.DataFrame,
    options_until_now: pd.DataFrame,
    current_spot: float,
    open_positions: dict[str, dict],
    trades: list[dict],
    skipped: list[dict],
) -> None:
    strategy = get_strategy(bot["strategy_type"])
    df_5min_raw = to_5min(spot_until_now, now=now)
    if df_5min_raw.empty:
        return
    df_5min = compute_all(
        df_5min_raw,
        rsi_period=params["rsi_period"],
        ema_fast=params["ema_fast_period"],
        ema_slow=params["ema_slow_period"],
        sma_period=params["sma_period"],
    )
    df_5min["_rsi_oversold"] = params["rsi_oversold"]
    df_5min["_rsi_overbought"] = params["rsi_overbought"]

    position = open_positions.get("classic")
    if position:
        current_ltp = _latest_ltp_from_frame(options_until_now, position["symbol"])
        if current_ltp is None:
            skipped.append({"date": trade_date, "reason": "No valid contract LTP", "symbol": position["symbol"]})
            current_ltp = float(position["entry_ltp"])
        if now.strftime("%H:%M") >= EOD_CUTOFF:
            trades.append(_close_backtest_position(position, current_ltp, current_spot, "eod", now))
            del open_positions["classic"]
            return
        exit_reason = check_exit(position, current_ltp, current_spot, df_5min, params, strategy, check_indicator=_at_5min_close(now))
        if exit_reason:
            trades.append(_close_backtest_position(position, current_ltp, current_spot, exit_reason, now))
            del open_positions["classic"]
        return

    if not _can_enter(bot, trade_date, now, trades):
        return
    signal_side = strategy.entry_signal(df_5min, params)
    if signal_side is None:
        return
    position = _open_backtest_position(bot, params, signal_side, "classic", trade_date, now, current_spot, options_until_now)
    if position:
        open_positions["classic"] = position
    else:
        skipped.append({"date": trade_date, "reason": "No valid contract LTP", "side": signal_side})


def _open_backtest_position(
    bot: dict,
    params: dict,
    side: str,
    leg_code: str,
    trade_date: str,
    now: datetime,
    spot: float,
    options_until_now: pd.DataFrame,
) -> dict | None:
    contract = _resolve_contract_from_options(bot, params, side, spot, options_until_now)
    if contract is None:
        return None
    ltp = _latest_ltp_from_frame(options_until_now, contract["symbol"])
    if ltp is None or ltp == 0:
        return None
    return {
        "position_id": str(uuid.uuid4()),
        "bot_id": bot["bot_id"],
        "trade_date": trade_date,
        "underlying": bot["underlying"],
        "side": side,
        "symbol": contract["symbol"],
        "strike": contract["strike"],
        "expiry": contract["expiry"],
        "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "entry_ltp": float(ltp),
        "entry_spot": float(spot),
        "lot_size": UNDERLYINGS[bot["underlying"]]["lot_size"],
        "qty": 1,
        "status": "open",
        "leg_code": leg_code,
    }


def _close_backtest_position(position: dict, exit_ltp: float, exit_spot: float, reason: str, now: datetime) -> dict:
    entry_time = datetime.strptime(position["entry_time"], "%Y-%m-%d %H:%M:%S")
    hold_mins = max(int((now - entry_time).total_seconds() / 60), 0)
    entry_ltp = float(position["entry_ltp"])
    q = int(position["lot_size"]) * int(position["qty"])
    pnl_pts = float(exit_ltp) - entry_ltp
    pnl_rs = pnl_pts * q
    charges = _charges(entry_ltp, float(exit_ltp), q)
    net_pnl_rs = round(pnl_rs - charges, 2)
    return {
        "trade_id": str(uuid.uuid4()),
        "bot_id": position["bot_id"],
        "trade_date": position["trade_date"],
        "underlying": position["underlying"],
        "leg_code": position.get("leg_code"),
        "side": position["side"],
        "symbol": position["symbol"],
        "strike": position["strike"],
        "expiry": position["expiry"],
        "entry_time": position["entry_time"],
        "exit_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "entry_ltp": round(entry_ltp, 2),
        "exit_ltp": round(float(exit_ltp), 2),
        "entry_spot": round(float(position["entry_spot"]), 2),
        "exit_spot": round(float(exit_spot), 2),
        "lot_size": int(position["lot_size"]),
        "qty": int(position["qty"]),
        "pnl_pts": round(pnl_pts, 2),
        "pnl_rs": round(pnl_rs, 2),
        "charges": charges,
        "net_pnl_rs": net_pnl_rs,
        "exit_reason": reason,
        "holding_mins": hold_mins,
    }


def _resolve_contract_from_options(bot: dict, params: dict, side: str, spot: float, options_df: pd.DataFrame) -> dict | None:
    if options_df.empty:
        return None
    step = UNDERLYINGS[bot["underlying"]]["strike_step"]
    strike_mode = params.get("strike_mode", "atm")
    offset_pts = int(params.get("strike_offset_pts", 0) or 0)
    if strike_mode == "atm":
        strike = _round_strike(spot, step)
    elif strike_mode == "itm_N":
        strike = _round_strike(spot + (-offset_pts if side == "CE" else offset_pts), step)
    else:
        strike = _round_strike(spot + (offset_pts if side == "CE" else -offset_pts), step)

    rows = options_df[options_df["tradingsymbol"].astype(str).str.endswith(side)]
    if "strike" in rows.columns:
        rows = rows[pd.to_numeric(rows["strike"], errors="coerce") == strike]
    else:
        rows = rows[rows["tradingsymbol"].astype(str).str.contains(str(strike), regex=False)]
    if rows.empty:
        return None
    symbols = sorted(set(rows["tradingsymbol"].astype(str)), key=lambda s: (len(s), s))
    symbol = symbols[0]
    matched = rows[rows["tradingsymbol"].astype(str) == symbol].iloc[-1]
    return {
        "symbol": symbol,
        "strike": strike,
        "expiry": str(matched.get("expiry", "")),
    }


def _can_enter(bot: dict, trade_date: str, now: datetime, trades: list[dict]) -> bool:
    if not _at_5min_close(now):
        return False
    t = now.strftime("%H:%M")
    if not (bot.get("session_start", "09:20") <= t <= bot.get("session_end", "15:15")):
        return False
    if t >= EOD_CUTOFF:
        return False
    max_trades = int(bot.get("max_trades_per_day", 3) or 3)
    return sum(1 for tr in trades if tr["bot_id"] == bot["bot_id"] and tr["trade_date"] == trade_date) < max_trades


def _at_5min_close(now: datetime) -> bool:
    return now.minute % 5 == 0


def _charges(entry_ltp: float, exit_ltp: float, quantity: int) -> float:
    try:
        turnover = (entry_ltp + exit_ltp) * quantity
        charges = 40 + 0.00053 * turnover + 0.001 * (exit_ltp * quantity)
        if math.isfinite(charges):
            return round(charges, 2)
    except Exception:
        pass
    return 0.0


def _summarize(trades: list[dict]) -> dict[str, Any]:
    total = len(trades)
    gross = sum(float(t["pnl_rs"]) for t in trades)
    charges = sum(float(t["charges"]) for t in trades)
    net = sum(float(t["net_pnl_rs"]) for t in trades)
    wins = sum(1 for t in trades if float(t["net_pnl_rs"]) > 0)
    return {
        "total_trades": total,
        "gross_pnl": round(gross, 2),
        "charges": round(charges, 2),
        "net_pnl": round(net, 2),
        "win_rate": round((wins / total) * 100, 2) if total else 0.0,
        "average_trade": round(net / total, 2) if total else 0.0,
        "max_drawdown": _max_drawdown(trades),
    }


def _max_drawdown(trades: list[dict]) -> float:
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda r: r["exit_time"]):
        equity += float(t["net_pnl_rs"])
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return round(max_dd, 2)


def _day_wise_pnl(trades: list[dict]) -> list[dict]:
    by_day: dict[str, dict] = {}
    for tr in trades:
        row = by_day.setdefault(tr["trade_date"], {"date": tr["trade_date"], "trades": 0, "gross_pnl": 0.0, "charges": 0.0, "net_pnl": 0.0})
        row["trades"] += 1
        row["gross_pnl"] += float(tr["pnl_rs"])
        row["charges"] += float(tr["charges"])
        row["net_pnl"] += float(tr["net_pnl_rs"])
    return [
        {**row, "gross_pnl": round(row["gross_pnl"], 2), "charges": round(row["charges"], 2), "net_pnl": round(row["net_pnl"], 2)}
        for row in sorted(by_day.values(), key=lambda r: r["date"])
    ]


def _build_params(bot: dict) -> dict:
    params = dict(bot)
    params["entry_rules"] = _json_list(bot.get("entry_rules_json", "[]"))
    params["exit_rules"] = _json_list(bot.get("exit_rules_json", "[]"))
    return params


def _json_list(value: str | list | None) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _needs_sma50_warmup(legs: list[dict]) -> bool:
    for leg in legs:
        for gate in _json_list(leg.get("entry_gates_json", "[]")):
            if not gate.get("enabled", True):
                continue
            if gate.get("type") in SMA50_GATE_TYPES and int(gate.get("params", {}).get("period", 50)) >= 50:
                return True
    return False


def _normalize_spot(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").set_index("timestamp")
    df = df.sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = df["close"] if "close" in df.columns else 0
    return df[["open", "high", "low", "close", "volume"]]


def _spot_from_options(options_df: pd.DataFrame) -> pd.DataFrame:
    """Build a spot OHLC frame from the `spot` column of an options snapshot file.

    The shared-store collector embeds spot in every options row rather than
    writing a separate spot CSV, so derive one value per timestamp.
    """
    if options_df.empty or "spot" not in options_df.columns:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    s = options_df[["timestamp", "spot"]].copy()
    s["spot"] = pd.to_numeric(s["spot"], errors="coerce")
    s = s.dropna(subset=["spot"])
    s = s[s["spot"] > 0]
    if s.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    s = s.groupby("timestamp", as_index=False)["spot"].last().rename(columns={"spot": "close"})
    return _normalize_spot(s)


def _normalize_options(df: pd.DataFrame, underlying: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if "tradingsymbol" not in df.columns and "symbol" in df.columns:
        df["tradingsymbol"] = df["symbol"]
    if "option_type" not in df.columns:
        df["option_type"] = df["tradingsymbol"].astype(str).str[-2:]
    if "strike" not in df.columns:
        df["strike"] = df["tradingsymbol"].astype(str).str.extract(r"(\d+)(?:CE|PE)$")[0]
    df["underlying"] = df.get("underlying", underlying)
    return df.sort_values("timestamp").reset_index(drop=True)


def _latest_ltp_from_frame(options_df: pd.DataFrame, symbol: str) -> float | None:
    if options_df.empty:
        return None
    rows = options_df[options_df["tradingsymbol"].astype(str) == symbol]
    if rows.empty:
        return None
    value = pd.to_numeric(rows.iloc[-1].get("ltp"), errors="coerce")
    if pd.isna(value):
        return None
    return float(value)


def _latest_ltp_at(options_df: pd.DataFrame, symbol: str, now: datetime) -> float | None:
    return _latest_ltp_from_frame(options_df[options_df["timestamp"] <= now], symbol)


def _load_warmup_spot(sources: list[DataSource], underlying: str, trade_date: str, day_spot: pd.DataFrame) -> pd.DataFrame:
    frames = []
    current = date.fromisoformat(trade_date)
    for delta in range(10, 0, -1):
        prior = (current - timedelta(days=delta)).isoformat()
        src = _best_source(sources, underlying, prior, "spot")
        if src:
            frames.append(_normalize_spot(_load_source(src, parse_dates=["timestamp"])))
            continue
        # Fall back to spot embedded in that day's options file.
        opt_src = _best_source(sources, underlying, prior, "options")
        if opt_src:
            opt_df = _normalize_options(_load_source(opt_src, parse_dates=["timestamp"]), underlying)
            prior_spot = _spot_from_options(opt_df)
            if not prior_spot.empty:
                frames.append(prior_spot)
    frames.append(day_spot)
    combined = pd.concat(frames, axis=0) if frames else day_spot
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def _load_source(src: DataSource, parse_dates: list[str] | None = None) -> pd.DataFrame:
    suffix = "".join(src.path.suffixes)
    if src.member:
        with tarfile.open(src.path, "r:*") as tar:
            member = tar.extractfile(src.member)
            if member is None:
                return pd.DataFrame()
            return pd.read_csv(member, parse_dates=parse_dates)
    if ".parquet" in suffix:
        return pd.read_parquet(src.path)
    return pd.read_csv(src.path, parse_dates=parse_dates)


def _scan_sources() -> list[DataSource]:
    sources: list[DataSource] = []
    roots = [
        ("data/live", DATA_DIR),
        ("data/archive", ARCHIVE_DIR),
        ("shared_market_data/live", SHARED_LIVE_DIR),
        ("shared_market_data/archive", SHARED_ARCHIVE_DIR),
    ]
    for kind, root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() == ".csv":
                src = _source_from_path(kind, root, path)
                if src:
                    sources.append(src)
            elif "".join(path.suffixes).lower().endswith(".tar.gz"):
                sources.extend(_sources_from_tar(kind, root, path))
            elif ".parquet" in "".join(path.suffixes).lower():
                src = _source_from_path(kind, root, path)
                if src:
                    sources.append(src)
    return sources


def _source_from_path(kind: str, root: Path, path: Path) -> DataSource | None:
    text = str(path)
    day = _extract_date(text)
    underlying = _extract_underlying(path.name)
    data_type = _extract_data_type(path.name)
    if not day or not underlying or not data_type:
        return None
    return DataSource(kind, root, path, day, underlying, data_type)


def _sources_from_tar(kind: str, root: Path, path: Path) -> list[DataSource]:
    found = []
    try:
        with tarfile.open(path, "r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile() or not member.name.endswith(".csv"):
                    continue
                day = _extract_date(member.name) or _extract_date(path.name)
                underlying = _extract_underlying(member.name)
                data_type = _extract_data_type(member.name)
                if day and underlying and data_type:
                    found.append(DataSource(kind, root, path, day, underlying, data_type, member.name))
    except tarfile.TarError:
        log.warning("[backtest] unable to inspect archive %s", path)
    return found


def _best_source(sources: list[DataSource], underlying: str, trade_date: str, data_type: str) -> DataSource | None:
    matches = [s for s in sources if s.underlying == underlying and s.trade_date == trade_date and s.data_type == data_type]
    if not matches:
        return None
    priority = {"shared_market_data/live": 0, "data/live": 1, "shared_market_data/archive": 2, "data/archive": 3}
    return sorted(matches, key=lambda s: priority.get(s.kind, 99))[0]


def _extract_date(text: str) -> str | None:
    match = DATE_RE.search(text)
    return match.group(0) if match else None


def _extract_underlying(name: str) -> str | None:
    # Prefer the longest matching name so "BANKNIFTY" is not shadowed by the
    # "NIFTY" substring check.
    upper = name.upper()
    best: str | None = None
    for underlying in UNDERLYINGS:
        if underlying in upper and (best is None or len(underlying) > len(best)):
            best = underlying
    return best


def _extract_data_type(name: str) -> str | None:
    lower = name.lower()
    if "spot" in lower:
        return "spot"
    if "option" in lower:
        return "options"
    return None


def _sma50_possible(underlying: str, sources: list[DataSource]) -> bool:
    spot_days = sorted({s.trade_date for s in sources if s.underlying == underlying and s.data_type == "spot"})
    return len(spot_days) > 0


def _source_roots() -> list[dict[str, Any]]:
    return [
        {"label": "shared_market_data/live", "path": str(SHARED_LIVE_DIR), "exists": SHARED_LIVE_DIR.exists()},
        {"label": "shared_market_data/archive", "path": str(SHARED_ARCHIVE_DIR), "exists": SHARED_ARCHIVE_DIR.exists()},
        {"label": "data/live", "path": str(DATA_DIR), "exists": DATA_DIR.exists()},
        {"label": "data/archive", "path": str(ARCHIVE_DIR), "exists": ARCHIVE_DIR.exists()},
    ]


def _date_range(start_date: str, end_date: str) -> list[str]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError("end_date must be on or after start_date")
    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days
