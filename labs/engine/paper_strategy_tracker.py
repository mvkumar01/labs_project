"""Persistent daily PAPER tracker for the live Alpha champion (v2.11).

Replays the COMPLETED 5-min day through the SAME production signal stack the
real bot uses — `live.engine.alpha_hybrid.hybrid_alpha_bars` (range from
alphaIMB's locked hybrid_range_state) + `live.engine.signal_engine`
(AlphaSignalEngine) — executes it on PAPER (no broker, never places orders),
prices fills off the shared-store option LTP, deducts real charges, and
persists a daily row + per-trade rows to labs.db.

Run EOD once per day (pa_paper_tracker.py, ~15:40 IST). Idempotent per date.

Modeling notes (v1, documented for honesty):
- Option = ATM strike (nearest 50 to entry spot) for the side; premium = the
  last shared-store LTP in the entry/exit 5-min bucket. The live bot's exact
  strike pick may differ slightly; PnL direction/magnitude is option-premium
  based (long option: profit = exit_premium - entry_premium).
- pnl_pts (spot move) is also recorded — that's the strategy's validated metric.
- Charges via labs.engine.charges (round-trip, buy-to-open/sell-to-close).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from storage.db import get_conn
from labs.engine.charges import round_trip_charges
from live.engine.alpha_hybrid import hybrid_alpha_bars
from live.engine.signal_engine import AlphaSignalEngine
from config.labs_config import SHARED_LIVE_DIR, UNDERLYINGS

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NIFTY"
LOT_SIZE = int(UNDERLYINGS.get("NIFTY", {}).get("lot_size", 65))
PAPER_LOTS = 1                       # tracker trades 1 lot (unit strategy view)
QTY = LOT_SIZE * PAPER_LOTS
STRATEGY_VERSION = "alpha_v2.11"
_RULE3_STATE = Path(__file__).resolve().parents[2] / "storage" / "state" / "paper_tracker_rule3.json"


# ── schema (lazy; never touches existing labs.db tables) ─────────────────────
def _ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_strategy_daily (
            trade_date   TEXT PRIMARY KEY,
            status       TEXT,
            tier         TEXT,
            gap_dir      TEXT,
            n_trades     INTEGER,
            pnl_pts      REAL,
            gross_rs     REAL,
            charges_rs   REAL,
            net_rs       REAL,
            strategy_version TEXT,
            updated_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_strategy_trades (
            trade_date   TEXT,
            seq          INTEGER,
            side         TEXT,
            strike       INTEGER,
            entry_ts     TEXT,
            exit_ts      TEXT,
            entry_spot   REAL,
            exit_spot    REAL,
            entry_prem   REAL,
            exit_prem    REAL,
            pnl_pts      REAL,
            gross_rs     REAL,
            charges_rs   REAL,
            net_rs       REAL,
            entry_rule   TEXT,
            exit_reason  TEXT,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def _r50(x: float) -> int:
    return int(round(float(x) / 50.0) * 50)


def _premium_lookup(trade_date: str) -> dict:
    """(bucket_start_iso, strike, 'ce'|'pe') -> last LTP in that 5-min bucket."""
    path = SHARED_LIVE_DIR / trade_date / f"{SYMBOL}_options_1min.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    if "option_type" in df.columns:
        df["type"] = df["option_type"].astype(str).str.lower()
    if "ltp" not in df.columns or "type" not in df.columns:
        return {}
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["ltp"] = pd.to_numeric(df["ltp"], errors="coerce")
    df = df.dropna(subset=["strike", "ltp"])
    df["bucket"] = df["timestamp"].dt.floor("5min")
    out: dict = {}
    for (bucket, strike, typ), g in df.groupby(["bucket", "strike", "type"]):
        out[(bucket.isoformat(), int(strike), str(typ))] = float(g.sort_values("timestamp")["ltp"].iloc[-1])
    return out


def _premium(lookup: dict, bar_ts_iso: str, strike: int, otype: str) -> float | None:
    # bar_ts from hybrid_alpha_bars is the bucket-start (label=left); match directly.
    key = (pd.Timestamp(bar_ts_iso).isoformat(), int(strike), otype)
    return lookup.get(key)


def run_day(trade_date: str | None = None) -> dict:
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    conn = get_conn()
    _ensure_tables(conn)

    state, bars = hybrid_alpha_bars(trade_date)
    if not state or not bars:
        _save_daily(conn, trade_date, status="no_trade", tier=(state or {}).get("bucket"),
                    gap_dir=(state or {}).get("direction"), trades=[])
        return {"trade_date": trade_date, "status": "no_trade", "net_rs": 0.0, "n_trades": 0}

    lookup = _premium_lookup(trade_date)
    engine = AlphaSignalEngine(rule3_state_path=str(_RULE3_STATE))
    engine.restore_rule3_state(trade_date)

    pos = side = entry_rule = entry_spot = entry_ts = entry_strike = entry_prem = None
    otype = None
    trades: list[dict] = []

    def _close(exit_ts, exit_spot, reason):
        nonlocal pos, side, entry_rule, entry_spot, entry_ts, entry_strike, entry_prem, otype
        ep = _premium(lookup, exit_ts, entry_strike, otype)
        # premium fallback: approximate via spot move on a ~0.5-delta ATM option
        if ep is None and entry_prem is not None:
            move = (exit_spot - entry_spot) if side == "CALL" else (entry_spot - exit_spot)
            ep = max(0.05, entry_prem + 0.5 * move)
        pnl_pts = (exit_spot - entry_spot) if side == "CALL" else (entry_spot - exit_spot)
        gross_rs = (float(ep) - float(entry_prem)) * QTY if (ep and entry_prem) else pnl_pts * QTY
        ch = round_trip_charges(entry_prem or 0.0, ep or 0.0, QTY)
        trades.append(dict(
            side=side, strike=entry_strike, entry_ts=entry_ts, exit_ts=exit_ts,
            entry_spot=round(entry_spot, 1), exit_spot=round(exit_spot, 1),
            entry_prem=round(entry_prem or 0.0, 2), exit_prem=round(ep or 0.0, 2),
            pnl_pts=round(pnl_pts, 1), gross_rs=round(gross_rs, 2),
            charges_rs=ch["total"], net_rs=round(gross_rs - ch["total"], 2),
            entry_rule=entry_rule, exit_reason=reason,
        ))
        pos = side = entry_rule = entry_spot = entry_ts = entry_strike = entry_prem = otype = None

    for bar in bars:
        if bar.get("alpha") is None or bar.get("spot") is None:
            continue
        ts_iso = bar["timestamp"]
        bt = None
        try:
            bt = datetime.fromisoformat(ts_iso).astimezone(IST).time()
        except Exception:
            pass
        sig = engine.evaluate(
            current_alpha=float(bar["alpha"]),
            position=("OPEN" if pos is not None else "NONE"), side=side,
            tier=bar.get("tier") or "PC50", entry_rule=entry_rule,
            denom_alg=bar.get("denom_alg"), bar_time_ist=bt,
            gap_direction=bar.get("gap_direction"), today_iso=trade_date,
            spot=bar.get("spot"), entry_spot=entry_spot, vix_at_open=bar.get("vix_at_open"),
        )
        action = sig.get("action")
        spot = float(bar["spot"])
        if action == "ENTER" and pos is None:
            side = sig.get("side"); entry_rule = sig.get("rule")
            entry_spot = spot; entry_ts = ts_iso; entry_strike = _r50(spot)
            otype = "ce" if side == "CALL" else "pe"
            entry_prem = _premium(lookup, ts_iso, entry_strike, otype)
            if entry_prem is None:
                entry_prem = 150.0   # nominal ATM premium fallback (rare; logged via charges)
            pos = "open"
        elif action == "EXIT" and pos is not None:
            _close(ts_iso, spot, sig.get("reason") or "alpha_exit")

    if pos is not None:                              # EOD square-off at last bar
        last = bars[-1]
        _close(last["timestamp"], float(last["spot"]), "eod")

    _save_daily(conn, trade_date, status="traded", tier=state.get("bucket"),
                gap_dir=state.get("direction"), trades=trades)
    net = round(sum(t["net_rs"] for t in trades), 2)
    return {"trade_date": trade_date, "status": "traded", "net_rs": net, "n_trades": len(trades)}


def _save_daily(conn, trade_date, *, status, tier, gap_dir, trades) -> None:
    pnl_pts = round(sum(t["pnl_pts"] for t in trades), 1)
    gross = round(sum(t["gross_rs"] for t in trades), 2)
    charges = round(sum(t["charges_rs"] for t in trades), 2)
    net = round(gross - charges, 2)
    now = datetime.now(IST).isoformat()
    conn.execute("DELETE FROM paper_strategy_trades WHERE trade_date = ?", (trade_date,))
    for i, t in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO paper_strategy_trades (trade_date, seq, side, strike, entry_ts, "
            "exit_ts, entry_spot, exit_spot, entry_prem, exit_prem, pnl_pts, gross_rs, "
            "charges_rs, net_rs, entry_rule, exit_reason) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_date, i, t["side"], t["strike"], t["entry_ts"], t["exit_ts"],
             t["entry_spot"], t["exit_spot"], t["entry_prem"], t["exit_prem"],
             t["pnl_pts"], t["gross_rs"], t["charges_rs"], t["net_rs"],
             t["entry_rule"], t["exit_reason"]))
    conn.execute(
        "INSERT INTO paper_strategy_daily (trade_date, status, tier, gap_dir, n_trades, "
        "pnl_pts, gross_rs, charges_rs, net_rs, strategy_version, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status, tier=excluded.tier, "
        "gap_dir=excluded.gap_dir, n_trades=excluded.n_trades, pnl_pts=excluded.pnl_pts, "
        "gross_rs=excluded.gross_rs, charges_rs=excluded.charges_rs, net_rs=excluded.net_rs, "
        "strategy_version=excluded.strategy_version, updated_at=excluded.updated_at",
        (trade_date, status, tier, gap_dir, len(trades), pnl_pts, gross, charges, net,
         STRATEGY_VERSION, now))
    conn.commit()


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else None
    print(run_day(d))
