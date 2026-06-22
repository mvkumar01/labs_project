"""SENSEX-own abs-denominator Alpha paper tracker.

This engine is intentionally independent of the NIFTY champion. It computes
SENSEX Alpha from exact five-minute OI snapshots, settled prior-day SENSEX OI,
and a configurable previous-close strike window. Its observed direction is
inverted: +30 cross buys a PUT; -30 cross buys a CALL.

Option execution is a conservative paper model: buy at the exact-mark ask and
sell the same ATM/nearest-expiry contract at the exact-mark bid. LTP is never
used as an execution fallback. Spot P&L is retained even when either executable
quote is unavailable.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from config.labs_config import (
    BASE_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS,
)
from market_data.expiry import expiry_sort_date, select_expiry_code
from market_data.shared_store import load_options_frame
from storage.db import get_conn

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "SENSEX"
STRATEGY_VERSION = "sensex_own_alpha_abs_v1"
CONFIG_PATH = BASE_DIR / "config" / "sensex_alpha.json"


class SensexReplayInputError(RuntimeError):
    """Required market/baseline input is unavailable or unsafe to replay."""


def _alpha_base_dir() -> Path:
    pa_dir = Path("/home/mvkumar01/alphaIMB")
    if pa_dir.exists():
        return pa_dir
    local_dir = Path.home() / "alphaIMB"
    if local_dir.exists():
        return local_dir
    return Path(__file__).resolve().parents[3] / "alphaIMB"


PREV_DAY_DIR = _alpha_base_dir() / "data" / "prev_day"


def load_config() -> dict:
    defaults = {
        "half_width": 1200.0,
        "entry_threshold": 30.0,
        "target_abs": 100.0,
        "eod_exit": "15:25",
        "expiry_mode": "nearest_weekly",
        "lot_size": int(UNDERLYINGS[SYMBOL]["lot_size"]),
    }
    if CONFIG_PATH.is_file():
        defaults.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    defaults["half_width"] = float(defaults["half_width"])
    defaults["entry_threshold"] = float(defaults["entry_threshold"])
    defaults["target_abs"] = float(defaults["target_abs"])
    defaults["lot_size"] = int(defaults["lot_size"])
    if defaults["half_width"] <= 0 or defaults["entry_threshold"] <= 0:
        raise ValueError("SENSEX Alpha widths and thresholds must be positive")
    if defaults["target_abs"] > 100 or defaults["target_abs"] <= 0:
        raise ValueError("SENSEX Alpha target_abs must be in (0, 100]")
    if defaults["expiry_mode"] != "nearest_weekly":
        raise ValueError("SENSEX Alpha currently requires nearest_weekly expiry")
    datetime.strptime(defaults["eod_exit"], "%H:%M")
    return defaults


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensex_alpha_daily (
            trade_date              TEXT PRIMARY KEY,
            status                  TEXT NOT NULL,
            prev_close              REAL NOT NULL,
            range_lower             REAL NOT NULL,
            range_upper             REAL NOT NULL,
            latest_mark             TEXT,
            latest_spot             REAL,
            latest_alpha            REAL,
            position_side           TEXT,
            n_trades                INTEGER NOT NULL,
            spot_pnl_pts             REAL NOT NULL,
            option_gross_rs          REAL,
            option_priced_trades     INTEGER NOT NULL,
            option_unavailable_trades INTEGER NOT NULL,
            expiry_code             TEXT,
            baseline_date           TEXT,
            strategy_version        TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sensex_alpha_trades (
            trade_date       TEXT NOT NULL,
            seq              INTEGER NOT NULL,
            status           TEXT NOT NULL,
            side             TEXT NOT NULL,
            strike           INTEGER NOT NULL,
            tradingsymbol    TEXT,
            expiry_code      TEXT,
            entry_ts         TEXT NOT NULL,
            exit_ts          TEXT NOT NULL,
            entry_alpha      REAL NOT NULL,
            exit_alpha       REAL NOT NULL,
            entry_spot       REAL NOT NULL,
            exit_spot        REAL NOT NULL,
            spot_pnl_pts     REAL NOT NULL,
            entry_bid        REAL,
            entry_ask        REAL,
            exit_bid         REAL,
            exit_ask         REAL,
            option_pnl_pts   REAL,
            option_gross_rs  REAL,
            quote_status     TEXT NOT NULL,
            entry_reason     TEXT NOT NULL,
            exit_reason      TEXT NOT NULL,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def _previous_weekdays(trade_date: str, limit: int = 15) -> list[str]:
    cursor = pd.Timestamp(trade_date).normalize()
    days: list[str] = []
    while len(days) < limit:
        cursor -= pd.Timedelta(days=1)
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y-%m-%d"))
    return days


def _load_baseline(trade_date: str, expiry_code: str) -> tuple[pd.DataFrame, str]:
    expected_expiry = expiry_sort_date(expiry_code)
    for baseline_date in _previous_weekdays(trade_date):
        path = PREV_DAY_DIR / f"prev_day_oi_{SYMBOL}_{baseline_date}.csv"
        if not path.is_file():
            continue
        frame = pd.read_csv(path)
        required = {"strike", "type", "oi"}
        missing = required.difference(frame.columns)
        if missing:
            raise SensexReplayInputError(
                f"Baseline {path} missing columns: {sorted(missing)}"
            )
        if "symbol" in frame.columns:
            frame = frame[frame["symbol"].astype(str).str.upper() == SYMBOL]
        if "expiry" in frame.columns and expected_expiry is not None:
            baseline_expiries = {
                pd.Timestamp(value).date()
                for value in frame["expiry"].dropna().astype(str).unique()
            }
            if baseline_expiries and baseline_expiries != {expected_expiry}:
                raise SensexReplayInputError(
                    f"Expiry rollover mismatch for {trade_date}: live={expiry_code} "
                    f"({expected_expiry}), baseline={sorted(str(x) for x in baseline_expiries)}"
                )
        frame["type"] = frame["type"].astype(str).str.lower()
        frame["strike"] = pd.to_numeric(frame["strike"], errors="coerce")
        frame["oi"] = pd.to_numeric(frame["oi"], errors="coerce")
        frame = frame.dropna(subset=["strike", "oi"])
        frame = frame[frame["type"].isin(["ce", "pe"])]
        grouped = frame.groupby(["strike", "type"], as_index=False)["oi"].sum()
        if grouped.empty:
            raise SensexReplayInputError(f"Baseline contains no usable SENSEX rows: {path}")
        return grouped, baseline_date
    raise SensexReplayInputError(
        f"No settled SENSEX baseline before {trade_date} under {PREV_DAY_DIR}"
    )


def _load_session(underlying: str, trade_date: str, columns=None) -> pd.DataFrame:
    try:
        frame = load_options_frame(
            underlying,
            trade_date,
            live_root=SHARED_LIVE_DIR,
            archive_root=SHARED_ARCHIVE_DIR,
            columns=columns,
        )
    except Exception as exc:
        raise SensexReplayInputError(
            f"Unable to load {underlying} shared data for {trade_date}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return frame


def _normalise_timestamps(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    if frame["timestamp"].dt.tz is None:
        frame["timestamp"] = frame["timestamp"].dt.tz_localize(IST)
    else:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert(IST)
    return frame


def _previous_close(trade_date: str) -> tuple[float, str]:
    for prior_date in _previous_weekdays(trade_date):
        try:
            frame = _load_session(SYMBOL, prior_date, columns=["timestamp", "spot"])
        except SensexReplayInputError:
            continue
        frame = _normalise_timestamps(frame)
        frame["spot"] = pd.to_numeric(frame["spot"], errors="coerce")
        spot = frame.dropna(subset=["spot"]).sort_values("timestamp")
        if not spot.empty:
            return float(spot.iloc[-1]["spot"]), prior_date
    raise SensexReplayInputError(f"No prior SENSEX close available before {trade_date}")


def build_alpha_bars(trade_date: str, config: dict | None = None) -> tuple[dict, pd.DataFrame, dict]:
    """Build exact-mark abs-denominator Alpha and executable quote book."""
    cfg = config or load_config()
    frame = _load_session(SYMBOL, trade_date)
    required = {"timestamp", "spot", "strike", "option_type", "expiry", "oi", "bid", "ask"}
    missing = required.difference(frame.columns)
    if missing:
        raise SensexReplayInputError(f"SENSEX shared data missing columns: {sorted(missing)}")
    frame = _normalise_timestamps(frame)
    frame["type"] = frame["option_type"].astype(str).str.lower()
    for column in ("spot", "strike", "oi", "bid", "ask"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["spot", "strike", "oi"])
    frame["expiry"] = frame["expiry"].astype(str)

    expiry_code = select_expiry_code(frame["expiry"].unique(), trade_date, cfg["expiry_mode"])
    if expiry_code is None:
        raise SensexReplayInputError(f"No nearest SENSEX expiry for {trade_date}")
    frame = frame[frame["expiry"] == str(expiry_code)].copy()
    if frame.empty:
        raise SensexReplayInputError(f"Selected SENSEX expiry {expiry_code} has no rows")

    baseline, baseline_date = _load_baseline(trade_date, str(expiry_code))
    prev_close, prev_close_date = _previous_close(trade_date)
    lower = prev_close - cfg["half_width"]
    upper = prev_close + cfg["half_width"]

    exact = frame[
        (frame["timestamp"].dt.hour * 60 + frame["timestamp"].dt.minute >= 9 * 60 + 15)
        & (frame["timestamp"].dt.hour * 60 + frame["timestamp"].dt.minute <= 15 * 60 + 25)
        & (frame["timestamp"].dt.minute % 5 == 0)
        & (frame["timestamp"].dt.second == 0)
    ].copy()
    if exact.empty:
        raise SensexReplayInputError(f"No exact five-minute SENSEX marks for {trade_date}")

    snapshots = (
        exact.sort_values("timestamp")
        .groupby(["timestamp", "strike", "type"], as_index=False)
        .last()
    )
    in_range = snapshots[
        (snapshots["strike"] >= lower) & (snapshots["strike"] <= upper)
    ].copy()
    if in_range.empty:
        raise SensexReplayInputError(
            f"No SENSEX strikes in prev-close range {lower:.2f}..{upper:.2f}"
        )
    merged = in_range.merge(
        baseline.rename(columns={"oi": "baseline_oi"}),
        on=["strike", "type"],
        how="left",
    )
    merged["baseline_oi"] = merged["baseline_oi"].fillna(0.0)
    merged["delta_oi"] = merged["oi"] - merged["baseline_oi"]

    rows = []
    for mark, group in merged.groupby("timestamp"):
        pe_delta = float(group.loc[group["type"] == "pe", "delta_oi"].sum())
        ce_delta = float(group.loc[group["type"] == "ce", "delta_oi"].sum())
        alpha = compute_abs_alpha(pe_delta, ce_delta)
        rows.append({
            "timestamp": mark,
            "spot": float(group["spot"].iloc[-1]),
            "alpha": round(alpha, 2),
            "pe_delta": pe_delta,
            "ce_delta": ce_delta,
        })
    bars = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    quotes: dict[tuple[str, int, str], dict] = {}
    quote_rows = (
        exact.sort_values("timestamp")
        .groupby(["timestamp", "strike", "type"], as_index=False)
        .last()
    )
    for row in quote_rows.itertuples(index=False):
        quotes[(pd.Timestamp(row.timestamp).isoformat(), int(row.strike), str(row.type))] = {
            "bid": _finite_positive(row.bid),
            "ask": _finite_positive(row.ask),
            "tradingsymbol": getattr(row, "tradingsymbol", None),
        }

    context = {
        "prev_close": prev_close,
        "prev_close_date": prev_close_date,
        "range_lower": lower,
        "range_upper": upper,
        "expiry_code": str(expiry_code),
        "baseline_date": baseline_date,
    }
    return context, bars, quotes


def _finite_positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def compute_abs_alpha(pe_delta: float, ce_delta: float) -> float:
    """Canonical bounded SENSEX-own Alpha formula."""
    denominator = abs(float(pe_delta)) + abs(float(ce_delta))
    if denominator == 0:
        return 0.0
    value = ((float(pe_delta) - float(ce_delta)) * 100.0) / denominator
    return max(-100.0, min(100.0, value))


def _quote(quotes: dict, timestamp, strike: int, option_type: str) -> dict:
    return quotes.get((pd.Timestamp(timestamp).isoformat(), int(strike), option_type), {})


def _spot_pnl(side: str, entry: float, exit_: float) -> float:
    return (exit_ - entry) if side == "CALL" else (entry - exit_)


def _price_trade(trade: dict, quotes: dict, lot_size: int) -> dict:
    option_type = "ce" if trade["side"] == "CALL" else "pe"
    entry_quote = _quote(quotes, trade["entry_ts"], trade["strike"], option_type)
    exit_quote = _quote(quotes, trade["exit_ts"], trade["strike"], option_type)
    entry_ask = entry_quote.get("ask")
    exit_bid = exit_quote.get("bid")
    entry_bid = entry_quote.get("bid")
    exit_ask = exit_quote.get("ask")
    quote_status = "priced"
    option_points = option_rupees = None
    if entry_ask is None:
        quote_status = "entry_ask_unavailable"
    elif entry_bid is not None and entry_ask < entry_bid:
        quote_status = "entry_market_crossed"
    elif exit_bid is None:
        quote_status = "exit_bid_unavailable"
    elif exit_ask is not None and exit_ask < exit_bid:
        quote_status = "exit_market_crossed"
    else:
        option_points = round(exit_bid - entry_ask, 2)
        option_rupees = round(option_points * lot_size, 2)
    trade.update({
        "tradingsymbol": entry_quote.get("tradingsymbol"),
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "exit_bid": exit_bid,
        "exit_ask": exit_ask,
        "option_pnl_pts": option_points,
        "option_gross_rs": option_rupees,
        "quote_status": quote_status,
    })
    return trade


def simulate(bars: pd.DataFrame, quotes: dict, context: dict, config: dict) -> list[dict]:
    threshold = config["entry_threshold"]
    target = config["target_abs"]
    eod_minutes = int(config["eod_exit"][:2]) * 60 + int(config["eod_exit"][3:])
    trades: list[dict] = []
    active: dict | None = None
    previous_alpha: float | None = None

    for row in bars.itertuples(index=False):
        mark = pd.Timestamp(row.timestamp)
        mark_minutes = mark.hour * 60 + mark.minute
        alpha = float(row.alpha)
        spot = float(row.spot)
        exited = False

        if active is not None:
            reason = None
            if abs(alpha) >= target:
                reason = "alpha_target"
            elif active["side"] == "PUT" and alpha <= 0:
                reason = "reversal_zero"
            elif active["side"] == "CALL" and alpha >= 0:
                reason = "reversal_zero"
            elif mark_minutes >= eod_minutes:
                reason = "eod_1525"
            if reason:
                active.update({
                    "status": "closed",
                    "exit_ts": mark.isoformat(),
                    "exit_alpha": alpha,
                    "exit_spot": spot,
                    "spot_pnl_pts": round(_spot_pnl(active["side"], active["entry_spot"], spot), 2),
                    "exit_reason": reason,
                })
                trades.append(_price_trade(active, quotes, config["lot_size"]))
                active = None
                exited = True

        if active is None and not exited and mark_minutes < eod_minutes and previous_alpha is not None:
            side = reason = None
            if previous_alpha <= threshold < alpha:
                side, reason = "PUT", "cross_up_plus_30"
            elif previous_alpha >= -threshold > alpha:
                side, reason = "CALL", "cross_down_minus_30"
            if side:
                strike = int(round(spot / 100.0) * 100)
                active = {
                    "status": "open",
                    "side": side,
                    "strike": strike,
                    "expiry_code": context["expiry_code"],
                    "entry_ts": mark.isoformat(),
                    "entry_alpha": alpha,
                    "entry_spot": spot,
                    "entry_reason": reason,
                }
        previous_alpha = alpha

    if active is not None:
        last = bars.iloc[-1]
        active.update({
            "status": "open",
            "exit_ts": pd.Timestamp(last["timestamp"]).isoformat(),
            "exit_alpha": float(last["alpha"]),
            "exit_spot": float(last["spot"]),
            "spot_pnl_pts": round(
                _spot_pnl(active["side"], active["entry_spot"], float(last["spot"])), 2
            ),
            "exit_reason": "holding",
        })
        trades.append(_price_trade(active, quotes, config["lot_size"]))
    return trades


def _session_over(trade_date: str, eod_exit: str) -> bool:
    now = datetime.now(IST)
    if trade_date < now.date().isoformat():
        return True
    if trade_date > now.date().isoformat():
        return False
    cutoff = datetime.strptime(eod_exit, "%H:%M").time()
    return now.time() >= cutoff


def _save(conn, trade_date: str, context: dict, bars: pd.DataFrame,
          trades: list[dict], config: dict, *, commit: bool = True) -> None:
    now = datetime.now(IST).isoformat()
    status = "open" if trades and trades[-1]["status"] == "open" else (
        "traded" if trades else "no_trade"
    )
    conn.execute("DELETE FROM sensex_alpha_trades WHERE trade_date=?", (trade_date,))
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO sensex_alpha_trades "
            "(trade_date,seq,status,side,strike,tradingsymbol,expiry_code,entry_ts,exit_ts,"
            "entry_alpha,exit_alpha,entry_spot,exit_spot,spot_pnl_pts,entry_bid,entry_ask,"
            "exit_bid,exit_ask,option_pnl_pts,option_gross_rs,quote_status,entry_reason,exit_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade_date, seq, trade["status"], trade["side"], trade["strike"],
                trade.get("tradingsymbol"), trade["expiry_code"], trade["entry_ts"],
                trade["exit_ts"], trade["entry_alpha"], trade["exit_alpha"],
                trade["entry_spot"], trade["exit_spot"], trade["spot_pnl_pts"],
                trade.get("entry_bid"), trade.get("entry_ask"), trade.get("exit_bid"),
                trade.get("exit_ask"), trade.get("option_pnl_pts"),
                trade.get("option_gross_rs"), trade["quote_status"],
                trade["entry_reason"], trade["exit_reason"],
            ),
        )
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    unavailable = len(trades) - len(priced)
    spot_total = round(sum(float(trade["spot_pnl_pts"]) for trade in trades), 2)
    option_total = round(sum(float(trade["option_gross_rs"]) for trade in priced), 2)
    latest = bars.iloc[-1]
    position_side = trades[-1]["side"] if status == "open" else None
    conn.execute(
        "INSERT INTO sensex_alpha_daily "
        "(trade_date,status,prev_close,range_lower,range_upper,latest_mark,latest_spot,"
        "latest_alpha,position_side,n_trades,spot_pnl_pts,option_gross_rs,"
        "option_priced_trades,option_unavailable_trades,expiry_code,baseline_date,"
        "strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,prev_close=excluded.prev_close,"
        "range_lower=excluded.range_lower,range_upper=excluded.range_upper,latest_mark=excluded.latest_mark,"
        "latest_spot=excluded.latest_spot,latest_alpha=excluded.latest_alpha,"
        "position_side=excluded.position_side,n_trades=excluded.n_trades,"
        "spot_pnl_pts=excluded.spot_pnl_pts,option_gross_rs=excluded.option_gross_rs,"
        "option_priced_trades=excluded.option_priced_trades,"
        "option_unavailable_trades=excluded.option_unavailable_trades,"
        "expiry_code=excluded.expiry_code,baseline_date=excluded.baseline_date,"
        "strategy_version=excluded.strategy_version,updated_at=excluded.updated_at",
        (
            trade_date, status, context["prev_close"], context["range_lower"],
            context["range_upper"], pd.Timestamp(latest["timestamp"]).isoformat(),
            float(latest["spot"]), float(latest["alpha"]), position_side, len(trades),
            spot_total, option_total, len(priced), unavailable, context["expiry_code"],
            context["baseline_date"], STRATEGY_VERSION, now,
        ),
    )
    if commit:
        conn.commit()


def run_day(trade_date: str | None = None, *, persist: bool = True,
            connection: sqlite3.Connection | None = None,
            commit: bool = True) -> dict:
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    config = load_config()
    context, bars, quotes = build_alpha_bars(trade_date, config)
    if len(bars) < 2:
        raise SensexReplayInputError(
            f"Only {len(bars)} completed SENSEX Alpha marks for {trade_date}"
        )
    if _session_over(trade_date, config["eod_exit"]):
        required_mark = pd.Timestamp(f"{trade_date} {config['eod_exit']}", tz=IST)
        if pd.Timestamp(bars.iloc[-1]["timestamp"]) < required_mark:
            raise SensexReplayInputError(
                f"Completed session {trade_date} lacks hard-EOD mark {config['eod_exit']}"
            )
    trades = simulate(bars, quotes, context, config)
    if persist:
        conn = connection or get_conn()
        if connection is None or commit:
            _ensure_tables(conn)
        _save(conn, trade_date, context, bars, trades, config, commit=commit)
    status = "open" if trades and trades[-1]["status"] == "open" else (
        "traded" if trades else "no_trade"
    )
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    return {
        "trade_date": trade_date,
        "status": status,
        "latest_alpha": float(bars.iloc[-1]["alpha"]),
        "n_trades": len(trades),
        "spot_pnl_pts": round(sum(t["spot_pnl_pts"] for t in trades), 2),
        "option_gross_rs": round(sum(t["option_gross_rs"] for t in priced), 2),
        "option_priced_trades": len(priced),
        "option_unavailable_trades": len(trades) - len(priced),
    }


if __name__ == "__main__":
    import sys
    print(run_day(sys.argv[1] if len(sys.argv) > 1 else None))
