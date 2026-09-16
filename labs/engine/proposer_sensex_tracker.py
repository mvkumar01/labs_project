"""SENSEX Proposer paper book.

Paper only. This module never calls a broker order API.

Rule set ("current", in force since 8 Sep 2026); the supporting analysis lives in
alphaIMB `research/experiments/2026-09-09_proposer_v1_reverse_engineering`:

  side        the Market Predictor's 09:00 regime row - bullish buys calls,
              bearish or risk_off buys puts; a neutral regime falls back to
              spot vs the 50-period five-minute SMA at the open
  risk_off    once the Predictor prints RISK_OFF the session is puts-only
  entries     five-minute checkpoints 09:20-15:20, one position at a time:
              a Strong Bull/Bear print above 45% confidence sets the side,
              otherwise the day side entered after RSI(3) has touched its
              20/80 band within the last three bars
  contract    nearest weekly expiry, rounded ATM -/+200 points ITM, 25 lots
  exits       2.5% daily target on the premium paid for the day's FIRST trade
              (ends the session), +40 spot points, -30% premium floor,
              micro-trend reversal, opposite strong print (signal flip), 15:25

Execution is the conservative labs paper model: buy at the exact-mark ask, sell
at the exact-mark bid, plus SENSEX charges. Pramanaa books at LTP, so both
bases are recorded and the LTP figures are what compare to its ledger.

Fidelity limits, measured on 22 Jun - 11 Sep and documented in the research
folder: the micro-trend exit is only partly recoverable from one-minute data,
and entry decisions here resolve to one minute while the bot polls in seconds.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.charges import sensex_round_trip_charges
from market_data.expiry import select_expiry_code
from market_data.shared_store import load_options_frame
from storage.db import get_conn

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "SENSEX"
LOT_SIZE = int(UNDERLYINGS[SYMBOL]["lot_size"])
STRIKE_STEP = int(UNDERLYINGS[SYMBOL]["strike_step"])
LOTS = 25
QTY = LOT_SIZE * LOTS                      # 500, matching the 8-11 Sep ledger
ITM_POINTS = 200
LATCH_BARS = 3
STRONG_CONF = 45.0
STRONG_MAX_AGE_MIN = 6
PREDICTOR_LAG_MIN = 1
DAILY_TARGET_RATE = 0.025
SPOT_TARGET_PTS = 40.0
LOSS_FLOOR = 0.30
FIRST_ENTRY, LAST_ENTRY, EOD = "09:20", "15:20", "15:25"
WARMUP_SESSIONS = 4                        # 50 five-minute bars need prior days
STRATEGY_VERSION = "proposer_sensex_current_v1"


class ProposerInputError(RuntimeError):
    """Required market data or Predictor rows are unavailable."""


# ------------------------------------------------------------------ schema ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS proposer_daily (
            trade_date              TEXT PRIMARY KEY,
            status                  TEXT NOT NULL,
            expiry_code             TEXT,
            regime_open             TEXT,
            side_day                TEXT,
            side_source             TEXT,
            risk_off_from           TEXT,
            n_trades                INTEGER NOT NULL,
            priced_trades           INTEGER NOT NULL,
            first_trade_premium_rs  REAL,
            day_target_rs           REAL,
            day_done_by_target      INTEGER NOT NULL DEFAULT 0,
            gross_rs                REAL,
            charges_rs              REAL,
            net_rs                  REAL,
            gross_ltp_rs            REAL,
            lot_size                INTEGER NOT NULL,
            lots                    INTEGER NOT NULL,
            qty                     INTEGER NOT NULL,
            strategy_version        TEXT NOT NULL,
            error                   TEXT,
            updated_at              TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proposer_trades (
            trade_date       TEXT NOT NULL,
            seq              INTEGER NOT NULL,
            signal           TEXT NOT NULL,
            side             TEXT NOT NULL,
            strike           INTEGER NOT NULL,
            tradingsymbol    TEXT,
            expiry_code      TEXT,
            entry_ts         TEXT NOT NULL,
            exit_ts          TEXT NOT NULL,
            entry_spot       REAL,
            exit_spot        REAL,
            rsi3             REAL,
            entry_ask        REAL,
            exit_bid         REAL,
            entry_ltp        REAL,
            exit_ltp         REAL,
            option_pnl_pts   REAL,
            gross_rs         REAL,
            charges_rs       REAL,
            net_rs           REAL,
            gross_ltp_rs     REAL,
            quote_status     TEXT NOT NULL,
            exit_rule        TEXT NOT NULL,
            PRIMARY KEY (trade_date, seq)
        );
        CREATE TABLE IF NOT EXISTS proposer_predictor_rows (
            trade_date   TEXT NOT NULL,
            ts           TEXT NOT NULL,
            kind         TEXT NOT NULL,
            label        TEXT NOT NULL,
            conf         REAL,
            microtrend   TEXT,
            mom5         TEXT,
            PRIMARY KEY (ts, kind)
        );
        """
    )
    conn.commit()


def seed_predictor_rows(frame: pd.DataFrame, *, connection: sqlite3.Connection | None = None,
                        commit: bool = True) -> int:
    """Insert Predictor rows (columns: trade_date, ts, kind, label, conf,
    microtrend, mom5).  Used by the backfill from the captured history and by
    the live Predictor writer."""
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        rows = [(str(r.trade_date), str(r.ts), str(r.kind), str(r.label),
                 None if pd.isna(r.conf) else float(r.conf),
                 str(getattr(r, "microtrend", "") or ""), str(getattr(r, "mom5", "") or ""))
                for r in frame.itertuples(index=False)]
        conn.executemany(
            "INSERT INTO proposer_predictor_rows "
            "(trade_date,ts,kind,label,conf,microtrend,mom5) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(ts,kind) DO UPDATE SET label=excluded.label,conf=excluded.conf,"
            "microtrend=excluded.microtrend,mom5=excluded.mom5,trade_date=excluded.trade_date",
            rows,
        )
        if commit:
            conn.commit()
        return len(rows)
    finally:
        if own:
            conn.close()


def load_predictor_rows(trade_date: str, conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        "SELECT trade_date,ts,kind,label,conf,microtrend,mom5 FROM proposer_predictor_rows "
        "WHERE trade_date=? ORDER BY ts",
        conn, params=(trade_date,),
    )
    if frame.empty:
        raise ProposerInputError(
            f"No Predictor rows stored for {trade_date}; the book never guesses a regime")
    frame["ts"] = pd.to_datetime(frame["ts"])
    return frame


# ------------------------------------------------------------------- data ---
def _positive(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _session_frame(trade_date: str) -> pd.DataFrame:
    frame = load_options_frame(SYMBOL, trade_date, live_root=SHARED_LIVE_DIR,
                               archive_root=SHARED_ARCHIVE_DIR)
    required = {"timestamp", "spot", "strike", "option_type", "expiry", "bid", "ask", "ltp"}
    missing = required.difference(frame.columns)
    if missing:
        raise ProposerInputError(f"SENSEX quotes missing columns: {sorted(missing)}")
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    if frame["timestamp"].dt.tz is not None:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    frame["option_type"] = frame["option_type"].astype(str).str.upper()
    frame["expiry"] = frame["expiry"].astype(str).str.upper()
    for column in ("spot", "strike", "bid", "ask", "ltp"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["timestamp", "spot", "strike"])


def _spot_minutes(frame: pd.DataFrame) -> pd.Series:
    return frame.groupby("timestamp")["spot"].median().sort_index()


def _five_minute_bars(spot: pd.Series) -> pd.DataFrame:
    out = []
    for day, part in spot.groupby(spot.index.normalize()):
        bars = part.resample("5min", origin=day + pd.Timedelta("9h15m"),
                             closed="left", label="right").last().dropna()
        out.append(bars)
    bars = pd.concat(out).rename("close").to_frame()
    delta = bars["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 3, adjust=False, min_periods=3).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 3, adjust=False, min_periods=3).mean()
    rs = gain / loss.replace(0.0, pd.NA)
    bars["rsi3"] = (100 - 100 / (1 + rs)).mask((loss == 0) & (gain > 0), 100.0) \
                                         .mask((gain == 0) & (loss > 0), 0.0)
    bars["sma50"] = bars["close"].rolling(50, min_periods=50).mean()
    index = range(len(bars))
    for name, cond in (("lt20", bars["rsi3"] < 20), ("gt80", bars["rsi3"] > 80)):
        last = pd.Series([i if c else None for i, c in zip(index, cond)],
                         index=bars.index, dtype="float64").ffill()
        bars[f"since_{name}"] = pd.Series(list(index), index=bars.index) - last
    return bars


def _warmup_spot(trade_date: str) -> pd.Series:
    """RSI3 and the 50-bar SMA need bars from prior sessions to be meaningful."""
    series = []
    day = date.fromisoformat(trade_date)
    found = 0
    probe = day - timedelta(days=1)
    while found < WARMUP_SESSIONS and (day - probe).days <= 12:
        if probe.weekday() < 5:
            try:
                series.append(_spot_minutes(_session_frame(probe.isoformat())))
                found += 1
            except Exception:                      # noqa: BLE001 - warmup is best effort
                pass
        probe -= timedelta(days=1)
    return pd.concat(series).sort_index() if series else pd.Series(dtype="float64")


# ------------------------------------------------------------------ engine ---
def _quotes_by_contract(frame: pd.DataFrame, expiry: str) -> dict:
    book = {}
    part = frame[frame["expiry"] == expiry]
    for (side, strike), g in part.groupby(["option_type", "strike"]):
        g = g.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        book[(side, int(strike))] = g.set_index("timestamp")[["bid", "ask", "ltp", "tradingsymbol"]] \
            if "tradingsymbol" in g.columns else g.set_index("timestamp")[["bid", "ask", "ltp"]]
    return book


def _mark(book: dict, side: str, strike: int, ts: pd.Timestamp) -> dict | None:
    g = book.get((side, int(strike)))
    if g is None or g.empty:
        return None
    upto = g.index[g.index <= ts]
    if len(upto) == 0 or (ts - upto[-1]) > pd.Timedelta("2min"):
        return None
    row = g.loc[upto[-1]]
    return {"bid": _positive(row.get("bid")), "ask": _positive(row.get("ask")),
            "ltp": _positive(row.get("ltp")), "tradingsymbol": row.get("tradingsymbol"),
            "ts": upto[-1]}


def _latest(rows: pd.DataFrame, ts: pd.Timestamp, max_age_min: int | None = None):
    visible = rows[rows["ts"] + pd.Timedelta(minutes=PREDICTOR_LAG_MIN) <= ts]
    if visible.empty:
        return None
    row = visible.iloc[-1]
    if max_age_min is not None and ts - row["ts"] > pd.Timedelta(minutes=max_age_min):
        return None
    return row


def _strong_side(row) -> str | None:
    if row is None or row["conf"] is None or not (float(row["conf"]) > STRONG_CONF):
        return None
    return {"Strong Bull": "CE", "Strong Bear": "PE"}.get(str(row["label"]))


def run_day(trade_date: str | None = None, *, persist: bool = True,
            connection: sqlite3.Connection | None = None, commit: bool = True) -> dict:
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    if date.fromisoformat(trade_date).weekday() >= 5:
        raise ProposerInputError(f"{trade_date} is not a trading weekday")
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        predictor = load_predictor_rows(trade_date, conn)
        frame = _session_frame(trade_date)
        expiry = select_expiry_code(frame["expiry"].unique(), trade_date, "nearest_weekly")
        if expiry is None:
            raise ProposerInputError(f"No nearest SENSEX expiry for {trade_date}")
        book = _quotes_by_contract(frame, str(expiry))
        spot_today = _spot_minutes(frame)
        warmup = _warmup_spot(trade_date)
        history = [s for s in (warmup, spot_today) if not s.empty]
        bars = _five_minute_bars(pd.concat(history).sort_index())
        day = pd.Timestamp(trade_date)
        bars = bars[bars.index.normalize() == day]
        if bars.empty:
            raise ProposerInputError(f"No five-minute bars for {trade_date}")

        five = predictor[predictor["kind"] == "5class"].reset_index(drop=True)
        regime = predictor[predictor["kind"] == "regime"].sort_values("ts")
        opening = regime[regime["ts"] <= day + pd.Timedelta("9h20m")]
        regime_open = str(opening["label"].iloc[0]).lower() if len(opening) else "none"
        if regime_open == "bullish":
            side_day, side_source = "CE", "regime_0900"
        elif regime_open in ("bearish", "risk_off"):
            side_day, side_source = "PE", "regime_0900"
        else:
            sma = bars["sma50"].iloc[0]
            if pd.isna(sma):
                side_day, side_source = None, "unavailable"
            else:
                side_day = "CE" if bars["close"].iloc[0] > sma else "PE"
                side_source = "open_sma50"
        risk_off = regime[regime["label"].str.lower() == "risk_off"]["ts"]
        risk_off_from = (risk_off.min() + pd.Timedelta(minutes=PREDICTOR_LAG_MIN)) \
            if len(risk_off) else None

        t0 = day + pd.Timedelta(FIRST_ENTRY + ":00")
        t_last = day + pd.Timedelta(LAST_ENTRY + ":00")
        t_eod = day + pd.Timedelta(EOD + ":00")
        checkpoints = set(bars.index[(bars.index >= t0) & (bars.index <= t_last)])
        position, trades = None, []
        realized_ltp, first_premium, done, last_exit = 0.0, None, False, None

        for minute in pd.date_range(t0, t_eod, freq="1min"):
            spot = spot_today.asof(minute)
            row5 = _latest(five, minute)

            if position is not None:
                mark = _mark(book, position["side"], position["strike"], minute)
                ltp = mark["ltp"] if mark and mark["ltp"] else position["last_ltp"]
                position["last_ltp"] = ltp
                later = minute > position["entry_ts"]
                sign = 1 if position["side"] == "CE" else -1
                unrealized = (ltp - position["entry_ltp"]) * QTY
                rule = None
                if later and first_premium and \
                        realized_ltp + unrealized >= DAILY_TARGET_RATE * first_premium:
                    rule = "daily_target"
                if rule is None and later and (spot - position["entry_spot"]) * sign >= SPOT_TARGET_PTS:
                    rule = "spot_target"
                if rule is None and later and ltp <= (1 - LOSS_FLOOR) * position["entry_ltp"]:
                    rule = "loss_floor"
                if rule is None and row5 is not None and \
                        str(row5["microtrend"]) == ("D" if sign == 1 else "U"):
                    rule = "microtrend_reversal"
                if rule is None and row5 is not None and \
                        row5["ts"] + pd.Timedelta(minutes=PREDICTOR_LAG_MIN) > position["entry_ts"]:
                    flip = _strong_side(row5)
                    if flip and flip != position["side"]:
                        rule = "signal_flip"
                if rule is None and minute >= t_eod:
                    rule = "eod"
                if rule:
                    trades.append(_close(position, minute, mark, ltp, spot, rule))
                    realized_ltp += trades[-1]["gross_ltp_rs"] or 0.0
                    position, last_exit = None, minute
                    if rule == "daily_target":
                        done = True

            if position is not None or done or minute not in checkpoints or minute == last_exit:
                continue
            bar = bars.loc[minute]
            latched = min([v for v in (bar["since_lt20"], bar["since_gt80"]) if pd.notna(v)],
                          default=None)
            latched = latched is not None and latched <= LATCH_BARS
            strong = _strong_side(_latest(five, minute, STRONG_MAX_AGE_MIN))
            side = signal = None
            if risk_off_from is not None and minute >= risk_off_from:
                if latched:
                    side, signal = "PE", "risk_off"
            elif strong:
                side = strong
                signal = "5class_strong_bull" if side == "CE" else "5class_strong_bear"
            elif side_day and latched:
                side = side_day
                oversold = bar["since_lt20"] if pd.notna(bar["since_lt20"]) else math.inf
                overbought = bar["since_gt80"] if pd.notna(bar["since_gt80"]) else math.inf
                signal = {("CE", True): "bullish", ("CE", False): "drift_up",
                          ("PE", False): "bearish", ("PE", True): "drift_down"}[
                    (side, bool(oversold <= overbought))]
            if side is None or pd.isna(spot):
                continue
            atm = int(round(float(spot) / STRIKE_STEP) * STRIKE_STEP)
            strike = atm - ITM_POINTS if side == "CE" else atm + ITM_POINTS
            mark = _mark(book, side, strike, minute)
            if mark is None or mark["ltp"] is None:
                continue
            position = {"trade_date": trade_date, "side": side, "strike": strike,
                        "expiry_code": str(expiry), "signal": signal, "entry_ts": minute,
                        "entry_spot": float(spot), "entry_ask": mark["ask"],
                        "entry_ltp": mark["ltp"], "last_ltp": mark["ltp"],
                        "tradingsymbol": mark.get("tradingsymbol"),
                        "rsi3": None if pd.isna(bar["rsi3"]) else round(float(bar["rsi3"]), 2)}
            if first_premium is None:
                first_premium = mark["ltp"] * QTY

        result = _summarise(trade_date, str(expiry), regime_open, side_day, side_source,
                            risk_off_from, first_premium, done, trades)
        if persist:
            _persist(conn, result, trades, commit=commit)
        return result
    finally:
        if own:
            conn.close()


def _close(position: dict, minute, mark, ltp, spot, rule: str) -> dict:
    exit_bid = mark["bid"] if mark else None
    entry_ask = position["entry_ask"]
    quote_status = "priced"
    points = gross = charges = net = None
    if entry_ask is None:
        quote_status = "entry_ask_unavailable"
    elif exit_bid is None:
        quote_status = "exit_bid_unavailable"
    else:
        points = round(exit_bid - entry_ask, 2)
        gross = round(points * QTY, 2)
        charges = round(sensex_round_trip_charges(entry_ask, exit_bid, QTY)["total"], 2)
        net = round(gross - charges, 2)
    return {
        "trade_date": position["trade_date"], "signal": position["signal"],
        "side": position["side"], "strike": position["strike"],
        "tradingsymbol": position.get("tradingsymbol"), "expiry_code": position["expiry_code"],
        "entry_ts": position["entry_ts"].isoformat(), "exit_ts": minute.isoformat(),
        "entry_spot": position["entry_spot"],
        "exit_spot": None if pd.isna(spot) else float(spot), "rsi3": position["rsi3"],
        "entry_ask": entry_ask, "exit_bid": exit_bid,
        "entry_ltp": position["entry_ltp"], "exit_ltp": ltp,
        "option_pnl_pts": points, "gross_rs": gross, "charges_rs": charges, "net_rs": net,
        "gross_ltp_rs": round((ltp - position["entry_ltp"]) * QTY, 2),
        "quote_status": quote_status, "exit_rule": rule,
    }


def _summarise(trade_date, expiry, regime_open, side_day, side_source, risk_off_from,
               first_premium, done, trades) -> dict:
    priced = [t for t in trades if t["quote_status"] == "priced"]
    return {
        "trade_date": trade_date, "status": "closed" if trades else "no_trade",
        "expiry_code": expiry, "regime_open": regime_open, "side_day": side_day,
        "side_source": side_source,
        "risk_off_from": risk_off_from.isoformat() if risk_off_from is not None else None,
        "n_trades": len(trades), "priced_trades": len(priced),
        "first_trade_premium_rs": round(first_premium, 2) if first_premium else None,
        "day_target_rs": round(DAILY_TARGET_RATE * first_premium, 2) if first_premium else None,
        "day_done_by_target": bool(done),
        "gross_rs": round(sum(t["gross_rs"] or 0 for t in priced), 2),
        "charges_rs": round(sum(t["charges_rs"] or 0 for t in priced), 2),
        "net_rs": round(sum(t["net_rs"] or 0 for t in priced), 2),
        "gross_ltp_rs": round(sum(t["gross_ltp_rs"] or 0 for t in trades), 2),
        "lot_size": LOT_SIZE, "lots": LOTS, "qty": QTY,
        "strategy_version": STRATEGY_VERSION, "trades": trades,
    }


def _persist(conn: sqlite3.Connection, result: dict, trades: list[dict], *, commit: bool) -> None:
    conn.execute("DELETE FROM proposer_trades WHERE trade_date=?", (result["trade_date"],))
    for seq, t in enumerate(trades, start=1):
        conn.execute(
            "INSERT INTO proposer_trades (trade_date,seq,signal,side,strike,tradingsymbol,"
            "expiry_code,entry_ts,exit_ts,entry_spot,exit_spot,rsi3,entry_ask,exit_bid,"
            "entry_ltp,exit_ltp,option_pnl_pts,gross_rs,charges_rs,net_rs,gross_ltp_rs,"
            "quote_status,exit_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (result["trade_date"], seq, t["signal"], t["side"], t["strike"], t["tradingsymbol"],
             t["expiry_code"], t["entry_ts"], t["exit_ts"], t["entry_spot"], t["exit_spot"],
             t["rsi3"], t["entry_ask"], t["exit_bid"], t["entry_ltp"], t["exit_ltp"],
             t["option_pnl_pts"], t["gross_rs"], t["charges_rs"], t["net_rs"],
             t["gross_ltp_rs"], t["quote_status"], t["exit_rule"]),
        )
    conn.execute(
        "INSERT INTO proposer_daily (trade_date,status,expiry_code,regime_open,side_day,"
        "side_source,risk_off_from,n_trades,priced_trades,first_trade_premium_rs,day_target_rs,"
        "day_done_by_target,gross_rs,charges_rs,net_rs,gross_ltp_rs,lot_size,lots,qty,"
        "strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,"
        "expiry_code=excluded.expiry_code,regime_open=excluded.regime_open,"
        "side_day=excluded.side_day,side_source=excluded.side_source,"
        "risk_off_from=excluded.risk_off_from,n_trades=excluded.n_trades,"
        "priced_trades=excluded.priced_trades,"
        "first_trade_premium_rs=excluded.first_trade_premium_rs,"
        "day_target_rs=excluded.day_target_rs,day_done_by_target=excluded.day_done_by_target,"
        "gross_rs=excluded.gross_rs,charges_rs=excluded.charges_rs,net_rs=excluded.net_rs,"
        "gross_ltp_rs=excluded.gross_ltp_rs,strategy_version=excluded.strategy_version,"
        "error=NULL,updated_at=excluded.updated_at",
        (result["trade_date"], result["status"], result["expiry_code"], result["regime_open"],
         result["side_day"], result["side_source"], result["risk_off_from"], result["n_trades"],
         result["priced_trades"], result["first_trade_premium_rs"], result["day_target_rs"],
         int(result["day_done_by_target"]), result["gross_rs"], result["charges_rs"],
         result["net_rs"], result["gross_ltp_rs"], LOT_SIZE, LOTS, QTY,
         STRATEGY_VERSION, datetime.now(IST).isoformat()),
    )
    if commit:
        conn.commit()


def record_unavailable(trade_date: str, error: str, *,
                       connection: sqlite3.Connection | None = None) -> None:
    """Persist an auditable no-result day without inventing a trade."""
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("DELETE FROM proposer_trades WHERE trade_date=?", (trade_date,))
        conn.execute(
            "INSERT INTO proposer_daily (trade_date,status,n_trades,priced_trades,"
            "day_done_by_target,gross_rs,charges_rs,net_rs,gross_ltp_rs,lot_size,lots,qty,"
            "strategy_version,error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,n_trades=0,"
            "priced_trades=0,gross_rs=0,charges_rs=0,net_rs=0,gross_ltp_rs=0,"
            "error=excluded.error,updated_at=excluded.updated_at",
            (trade_date, "unavailable", 0, 0, 0, 0, 0, 0, 0, LOT_SIZE, LOTS, QTY,
             STRATEGY_VERSION, str(error)[:500], datetime.now(IST).isoformat()),
        )
        conn.commit()
    finally:
        if own:
            conn.close()
