"""Persistent daily PAPER tracker for the live Alpha champion (v2.11).

Replays the COMPLETED 5-min day through `live.engine.champion_sim` — a faithful,
self-contained port of the validated alphaIMB research champion rule stack
(Rule 1/2/3 + v7.6 ALPHA_STALL + v7.7 PC400-DN PUT filter + v7.7 PC400-DN CALL
trail + v7.8 PC50 denom guard + v7.9 D2 + v7.11 drift stop + PC400 spot trail +
wall rejection). The alpha series itself comes from the same alpha_hybrid
pipeline the bot uses (range from alphaIMB's locked hybrid_range_state, or the
backfill override) and is bit-identical to research. Executes on PAPER (no
broker, never places orders), prices fills off the shared-store option LTP,
deducts real charges, and persists a daily row + per-trade rows to labs.db.

Pre-2026-06-19 this ran only the bot's per-bar AlphaSignalEngine (Rules 1/2/3 +
v7.8 + v7.11 + a hand-rolled v22 trail); v7.6 / v7.7 / v7.9-D2 were never wired
into labs, so /labs/live diverged from research on PC400 gap-DN days. Routing
through champion_sim closes that gap.

Run EOD once per day (pa_paper_tracker.py, ~15:40 IST) or intraday (loop). Idempotent per date.

Known live-vs-research residual: PC400 non-carve-out days where research uses
Gemini-c2 alpha (e.g. 06-03) are NOT reproducible from the locked range file and
degrade to regime+std here — by design (see alpha_hybrid docstring). The tracker
faithfully reflects the live bot, which is the point.

Modeling notes (v1, documented for honesty):
- Primary option = nearest-expiry 200-point ITM strike (ATM is the nearest 50 to
  entry spot): CALL uses ATM-200 CE and PUT uses ATM+200 PE. For comparison, all
  four CONTRACT_VARIANTS use identical Alpha signals; only strike offset and
  expiry week differ.
- premium = the shared-store LTP at the exact entry/exit Alpha mark. The same
  strike and expiry are held through exit.
- pnl_pts (spot move) is also recorded — that's the strategy's validated metric.
- Charges via labs.engine.charges (round-trip, buy-to-open/sell-to-close).
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

from storage.db import get_conn
from labs.engine.charges import round_trip_charges
from live.engine import champion_inputs, champion_sim
from live.engine.alpha_hybrid import HYBRID_STATE_FILE, _read_locked_hybrid_state
from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from market_data.expiry import select_expiry_code
from market_data.shared_store import load_options_frame

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NIFTY"
LOT_SIZE = int(UNDERLYINGS.get("NIFTY", {}).get("lot_size", 65))
PAPER_LOTS = 1                       # tracker trades 1 lot (unit strategy view)
QTY = LOT_SIZE * PAPER_LOTS
ITM_DISTANCE = 200
STRATEGY_VERSION = "alpha_v2.11_itm200"

CONTRACT_VARIANTS = {
    "near_atm":    {"label": "ATM — This week",    "expiry_mode": "nearest_weekly", "strike_offset": 0},
    "near_itm200": {"label": "ITM 200 — This week", "expiry_mode": "nearest_weekly", "strike_offset": 200},
    "next_atm":    {"label": "ATM — Next week",    "expiry_mode": "next_weekly",    "strike_offset": 0},
    "next_itm200": {"label": "ITM 200 — Next week", "expiry_mode": "next_weekly",   "strike_offset": 200},
}
PRIMARY_VARIANT = "near_itm200"


class ReplayInputError(RuntimeError):
    """Historical replay inputs are missing or incomplete.

    This is distinct from a valid strategy day with zero trades. Callers must
    retain previously published rows when this error is raised.
    """


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
            context_json TEXT,
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
        CREATE TABLE IF NOT EXISTS paper_contract_daily (
            trade_date       TEXT,
            variant          TEXT,
            expiry_mode      TEXT,
            expiry_code      TEXT,
            strike_offset    INTEGER,
            status           TEXT,
            n_trades         INTEGER,
            gross_rs         REAL,
            charges_rs       REAL,
            net_rs           REAL,
            error            TEXT,
            strategy_version TEXT,
            updated_at       TEXT,
            PRIMARY KEY (trade_date, variant)
        );
        CREATE TABLE IF NOT EXISTS paper_contract_trades (
            trade_date    TEXT,
            seq           INTEGER,
            variant       TEXT,
            expiry_mode   TEXT,
            expiry_code   TEXT,
            side          TEXT,
            strike        INTEGER,
            entry_ts      TEXT,
            exit_ts       TEXT,
            entry_spot    REAL,
            exit_spot     REAL,
            entry_prem    REAL,
            exit_prem     REAL,
            gross_rs      REAL,
            charges_rs    REAL,
            net_rs        REAL,
            entry_rule    TEXT,
            exit_reason   TEXT,
            PRIMARY KEY (trade_date, seq, variant)
        );
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(paper_strategy_daily)")
    }
    if "context_json" not in columns:
        conn.execute(
            "ALTER TABLE paper_strategy_daily ADD COLUMN context_json TEXT"
        )
    conn.commit()


def _r50(x: float) -> int:
    return int(round(float(x) / 50.0) * 50)


def _session_over(trade_date: str) -> bool:
    """True once trade_date's session is finished — a past day, or today at/after
    15:30 IST. Until then an open position is HELD (marked to market), not squared
    off as a fake eod at the latest completed bar."""
    now = datetime.now(IST)
    today = now.date().isoformat()
    if trade_date < today:
        return True
    if trade_date == today:
        return (now.hour * 60 + now.minute) >= (15 * 60 + 30)
    return False


def _build_price_books(trade_date: str) -> dict:
    """Read the options CSV once and return per-expiry price books.

    Returns::
        {
            "nearest_weekly": {"expiry_code": str, "prices": {(ts_iso, strike, otype): ltp}},
            "next_weekly":    {"expiry_code": str, "prices": {...}},
        }
    Either value may be None when that expiry is absent in the data.

    Alpha is a point-in-time OI snapshot at 09:15, 09:20, … . Option prices
    must come from that same exact timestamp — never relabelled bucket rows,
    never a different expiry.
    """
    books: dict = {"nearest_weekly": None, "next_weekly": None}
    df = load_options_frame(
        SYMBOL,
        trade_date,
        live_root=SHARED_LIVE_DIR,
        archive_root=SHARED_ARCHIVE_DIR,
    )
    if "option_type" in df.columns:
        df["type"] = df["option_type"].astype(str).str.lower()
    if "ltp" not in df.columns or "type" not in df.columns:
        return books
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["ltp"] = pd.to_numeric(df["ltp"], errors="coerce")
    df = df.dropna(subset=["strike", "ltp"])
    if "expiry" not in df.columns:
        return books
    df["expiry"] = df["expiry"].astype(str)
    all_expiries = df["expiry"].dropna().unique()

    for mode in ("nearest_weekly", "next_weekly"):
        code = select_expiry_code(all_expiries, trade_date, mode)
        if code is None:
            continue
        sub = df[df["expiry"] == str(code)].copy()
        sub = sub[sub["timestamp"].dt.minute % 5 == 0].copy()
        prices: dict = {}
        for (mark, strike, typ), g in sub.groupby(["timestamp", "strike", "type"]):
            prices[(mark.isoformat(), int(strike), str(typ))] = float(g["ltp"].iloc[-1])
        books[mode] = {"expiry_code": str(code), "prices": prices}
    return books


def _premium(lookup: dict, bar_ts_iso: str, strike: int, otype: str) -> float | None:
    # champion_sim entry/exit ts is the exact 5-min OI mark; match directly.
    key = (pd.Timestamp(bar_ts_iso).isoformat(), int(strike), otype)
    return lookup.get(key)


def _resolve_day(trade_date: str, override: dict | None) -> dict | None:
    """Resolve a tradable range, a verified SKIP day, or missing state.

    A locked SKIP is a valid strategy result and must remain distinguishable
    from a missing/unverified range so the paper books can persist no-trade
    rows without hiding upstream range failures.
    """
    if override is not None:
        if override.get("skip") or override.get("bucket") == "SKIP":
            return {"bucket": "SKIP", "direction": override.get("direction")}
        return {"lower": float(override["lower"]), "upper": float(override["upper"]),
                "bucket": override["bucket"], "direction": override["direction"],
                "vix": override.get("vix"), "vix_source": "backfill_override",
                "previous_session_date": override.get("previous_session_date"),
                "prev_close": override.get("prev_close"),
                "prev_close_source": override.get("prev_close_source"),
                "open_spot": override.get("open_spot"),
                "open_spot_source": override.get("open_spot_source"),
                "validate_shared_context": not bool(
                    override.get("_trust_historical_context")
                ),
                "biggap": bool(override.get("pc400_v210_biggap"))}
    state = _read_locked_hybrid_state(trade_date)
    if state is None:
        try:
            raw_state = json.loads(
                HYBRID_STATE_FILE.read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError, TypeError):
            raw_state = None
        if (
            raw_state
            and raw_state.get("trade_date") == trade_date
            and raw_state.get("locked")
            and raw_state.get("verified_open_locked")
            and raw_state.get("bucket") == "SKIP"
        ):
            return {"bucket": "SKIP", "direction": raw_state.get("direction")}
        return None
    return {"lower": float(state["lower"]), "upper": float(state["upper"]),
            "bucket": state.get("bucket") or "PC50", "direction": state.get("direction"),
            "vix": state.get("vix_at_open"), "vix_source": "locked_hybrid_state",
            "previous_session_date": state.get("prev_close_ref_date"),
            "prev_close": state.get("prev_close"),
            "prev_close_source": state.get("prev_close_source"),
            "open_spot": state.get("verified_open"),
            "open_spot_source": state.get("verified_open_source"),
            "validate_shared_context": False,
            "biggap": bool(state.get("pc400_v210_biggap"))}


def _resolve_replay_context(trade_date: str, day: dict):
    ohlc = champion_sim.OHLC(champion_inputs.ohlc_by_minute(trade_date))
    try:
        context = champion_inputs.resolve_day_context(
            trade_date,
            ohlc,
            day["direction"],
            day["vix"],
            vix_source=day.get("vix_source", "supplied"),
            previous_session_date=day.get("previous_session_date"),
            prev_close=day.get("prev_close"),
            prev_close_source=day.get("prev_close_source"),
            open_spot=day.get("open_spot"),
            open_spot_source=day.get("open_spot_source"),
            validate_shared_context=day.get("validate_shared_context", True),
        )
    except champion_inputs.ContextInputError as exc:
        raise ReplayInputError(
            f"Invalid session context for {trade_date}: {exc}; "
            "existing rows retained"
        ) from exc
    range_source, use_abs = champion_inputs.alpha_source(
        day["bucket"],
        context.direction,
        context.vix_open,
        context.sgap,
        day["biggap"],
    )
    provenance = context.to_dict()
    provenance.update(
        {
            "range_lower": day["lower"],
            "range_upper": day["upper"],
            "tier": day["bucket"],
            "biggap": day["biggap"],
            "range_source": range_source,
            "alpha_formula": "abs_denom" if use_abs else "std",
        }
    )
    return ohlc, context, range_source, use_abs, provenance


def replay_champion_signals(
    trade_date: str,
    override: dict | None = None,
    *,
    enable_v211a: bool = False,
) -> dict:
    """Build v2.11 trades once, before any option-contract pricing.

    Cross-index paper books use the returned side and exact entry/exit marks,
    ensuring their signal layer is identical to the NIFTY champion.
    """
    day = _resolve_day(trade_date, override)
    if day is None or day.get("bucket") == "SKIP":
        if day is None:
            raise ReplayInputError(
                f"No locked range state is available for {trade_date}; existing rows retained"
            )
        return {
            "tier": "SKIP",
            "direction": day.get("direction"),
            "sim_trades": [],
            "session_done": _session_over(trade_date),
        }

    tier = day["bucket"]
    ohlc, context, range_source, use_abs, provenance = (
        _resolve_replay_context(trade_date, day)
    )
    try:
        _, adf, ce_map, pe_map = champion_inputs.build_sim_inputs(
            trade_date, day["lower"], day["upper"], use_abs,
            range_source=range_source,
        )
    except Exception as exc:
        raise ReplayInputError(
            f"Unable to build replay inputs for {trade_date}: "
            f"{type(exc).__name__}: {exc}; existing rows retained"
        ) from exc
    if adf is None or adf.empty:
        raise ReplayInputError(
            f"Replay input frame is empty for {trade_date}; existing rows retained"
        )
    if trade_date == datetime.now(IST).date().isoformat():
        cutoff = pd.Timestamp(datetime.now(IST)) - pd.Timedelta(minutes=5)
        adf = adf[adf["timestamp"] <= cutoff].reset_index(drop=True)
    if len(adf) < 2:
        raise ReplayInputError(
            f"Replay input frame has only {len(adf)} completed bars for {trade_date}; "
            "existing rows retained"
        )

    v211a_low_vix = (
        enable_v211a
        and context.vix_open is not None
        and float(context.vix_open) < champion_inputs.VIX_TRAIL_CUTOFF
    )
    _, sim_trades = champion_sim.simulate(
        adf, ce_map, pe_map, ohlc, trade_date, context.use_trail,
        context.sgap, tier, context.weekday, context.regime,
        day["lower"], day["upper"],
        enable_v211a_low_vix_dn_put_trail=v211a_low_vix,
    )
    return {
        "tier": tier,
        "direction": context.direction,
        "sim_trades": sim_trades,
        "session_done": _session_over(trade_date),
        "context": provenance,
        "v211a_low_vix_trail_enabled": v211a_low_vix,
    }


def run_day(
    trade_date: str | None = None,
    override: dict | None = None,
    *,
    persist: bool = True,
    require_all_variants: bool = False,
    connection: sqlite3.Connection | None = None,
    commit: bool = True,
) -> dict:
    """Replay one day on paper through the faithful research champion engine
    (live.engine.champion_sim — Rule 1/2/3 + v7.6/v7.7/v7.8/v7.9-D2/v7.11 + trail
    + wall rejection). `override` (backfill) supplies the champion range when the
    live hybrid_range_state isn't available: {lower, upper, bucket, direction,
    vix, pc400_v210_biggap, skip}.

    ``persist=False`` performs the full replay and pricing validation without
    changing existing result rows. Missing replay data raises
    ``ReplayInputError``; it is never converted into a no-trade result.
    """
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    conn = connection or get_conn()
    if connection is None:
        _ensure_tables(conn)

    day = _resolve_day(trade_date, override)
    if day is None or day.get("bucket") == "SKIP":
        if day is None:
            raise ReplayInputError(
                f"No locked range state is available for {trade_date}; existing rows retained"
            )
        if persist:
            _save_daily(conn, trade_date, status="no_trade", tier="SKIP",
                        gap_dir=day.get("direction"), trades=[], commit=commit)
            _save_all_comparison_variants(
                conn, trade_date, {}, [], True, commit=commit
            )
        return {"trade_date": trade_date, "status": "no_trade", "net_rs": 0.0, "n_trades": 0}

    tier = day["bucket"]
    # Resolve cell context (sgap needs 09:15 open + prev close) BEFORE building
    # inputs so the alpha source (regime vs gemini_c2) + formula are decided per
    # the Run F routing — see champion_inputs.alpha_source.
    ohlc, context, range_source, use_abs, provenance = (
        _resolve_replay_context(trade_date, day)
    )
    try:
        _, adf, ce_map, pe_map = champion_inputs.build_sim_inputs(
            trade_date, day["lower"], day["upper"], use_abs, range_source=range_source)
    except Exception as exc:
        raise ReplayInputError(
            f"Unable to build replay inputs for {trade_date}: "
            f"{type(exc).__name__}: {exc}; existing rows retained"
        ) from exc
    if adf is None or adf.empty:
        raise ReplayInputError(
            f"Replay input frame is empty for {trade_date}; existing rows retained"
        )

    # Completed-bar discipline for today's live run: drop the in-progress bucket.
    if trade_date == datetime.now(IST).date().isoformat():
        cutoff = pd.Timestamp(datetime.now(IST)) - pd.Timedelta(minutes=5)
        adf = adf[adf["timestamp"] <= cutoff].reset_index(drop=True)
    if len(adf) < 2:
        raise ReplayInputError(
            f"Replay input frame has only {len(adf)} completed bars for {trade_date}; "
            "existing rows retained"
        )

    _, sim_trades = champion_sim.simulate(
        adf, ce_map, pe_map, ohlc, trade_date, context.use_trail,
        context.sgap, tier, context.weekday, context.regime,
        day["lower"], day["upper"])

    # Build both expiry price books with a single CSV read.
    books = _build_price_books(trade_date)
    session_done = _session_over(trade_date)

    if require_all_variants:
        missing_modes = [
            mode for mode in ("nearest_weekly", "next_weekly") if books.get(mode) is None
        ]
        if missing_modes:
            raise ReplayInputError(
                f"Missing expiry price books for {trade_date}: {', '.join(missing_modes)}; "
                "existing rows retained"
            )

    # ── Primary (near_itm200): strict — raises on any missing quote ───────────
    primary_prices = (books.get("nearest_weekly") or {}).get("prices", {})
    trades = [_price_trade(primary_prices, t, ITM_DISTANCE) for t in sim_trades]

    status = "traded"
    if trades and trades[-1]["exit_reason"] == "EOD" and not session_done:
        # intraday: the final open position isn't squared off — mark to market
        # (net = "if closed now") and surface it as HOLDING, not a fake eod.
        trades[-1]["exit_reason"] = "holding"
        status = "open"
    elif trades and trades[-1]["exit_reason"] == "EOD":
        trades[-1]["exit_reason"] = "eod"

    if require_all_variants:
        # Exercise the canonical comparison persistence/pricing path in memory
        # before any production row can change.
        check = sqlite3.connect(":memory:")
        check.row_factory = sqlite3.Row
        _ensure_tables(check)
        _save_all_comparison_variants(check, trade_date, books, sim_trades, session_done)
        unavailable = check.execute(
            "SELECT variant, error FROM paper_contract_daily "
            "WHERE trade_date=? AND status='unavailable' ORDER BY variant",
            (trade_date,),
        ).fetchall()
        check.close()
        if unavailable:
            detail = "; ".join(f"{row['variant']}: {row['error']}" for row in unavailable)
            raise ReplayInputError(
                f"Comparison pricing incomplete for {trade_date}: {detail}; "
                "existing rows retained"
            )

    if persist:
        _save_daily(
            conn, trade_date, status=status, tier=tier,
            gap_dir=context.direction, trades=trades, context=provenance,
            commit=commit,
        )

        # Comparison variants use identical simulated trades and timestamps.
        _save_all_comparison_variants(
            conn, trade_date, books, sim_trades, session_done, commit=commit
        )

    net = round(sum(t["net_rs"] for t in trades), 2)
    return {"trade_date": trade_date, "status": status, "net_rs": net, "n_trades": len(trades)}


def _price_trade(prices: dict, t: dict, strike_offset: int = ITM_DISTANCE) -> dict:
    """Translate one champion_sim trade into a priced paper trade.

    Uses ``prices`` — a ``{(ts_iso, strike, otype): ltp}`` dict from one expiry
    book — and prices the option at ``strike_offset`` points ITM (offset 0 = ATM,
    200 = ITM 200). Raises ValueError if an exact entry or exit quote is missing.
    """
    side = "CALL" if t["pos"] == "call" else "PUT"
    atm = _r50(t["entry_spot"])
    strike = atm - strike_offset if side == "CALL" else atm + strike_offset
    otype = "ce" if side == "CALL" else "pe"
    ets = pd.Timestamp(t["entry_ts"]).isoformat()
    xts = pd.Timestamp(t["exit_ts"]).isoformat()
    ep = _premium(prices, ets, strike, otype)
    xp = _premium(prices, xts, strike, otype)
    if ep is None or xp is None:
        missing = []
        if ep is None:
            missing.append(f"entry {ets}")
        if xp is None:
            missing.append(f"exit {xts}")
        raise ValueError(
            f"Missing exact LTP for {otype.upper()} {strike}: "
            + ", ".join(missing)
        )
    gross_rs = (float(xp) - float(ep)) * QTY
    ch = round_trip_charges(ep, xp, QTY)
    return dict(
        side=side, strike=strike, entry_ts=ets, exit_ts=xts,
        entry_spot=round(t["entry_spot"], 1), exit_spot=round(t["exit_spot"], 1),
        entry_prem=round(ep, 2), exit_prem=round(xp, 2),
        pnl_pts=round(float(t["pnl"]), 1), gross_rs=round(gross_rs, 2),
        charges_rs=ch["total"], net_rs=round(gross_rs - ch["total"], 2),
        entry_rule=t["entry_rule"], exit_reason=t["reason"],
    )


def _save_daily(
    conn, trade_date, *, status, tier, gap_dir, trades, context=None, commit=True
) -> None:
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
        "pnl_pts, gross_rs, charges_rs, net_rs, strategy_version, context_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status, tier=excluded.tier, "
        "gap_dir=excluded.gap_dir, n_trades=excluded.n_trades, pnl_pts=excluded.pnl_pts, "
        "gross_rs=excluded.gross_rs, charges_rs=excluded.charges_rs, net_rs=excluded.net_rs, "
        "strategy_version=excluded.strategy_version, context_json=excluded.context_json, "
        "updated_at=excluded.updated_at",
        (trade_date, status, tier, gap_dir, len(trades), pnl_pts, gross, charges, net,
         STRATEGY_VERSION, json.dumps(context, sort_keys=True) if context else None, now))
    if commit:
        conn.commit()


def _insert_comparison_daily(conn, trade_date, variant, expiry_mode, expiry_code,
                             strike_offset, status, n_trades, gross_rs, charges_rs,
                             net_rs, error, now) -> None:
    conn.execute(
        "INSERT INTO paper_contract_daily "
        "(trade_date, variant, expiry_mode, expiry_code, strike_offset, status, n_trades, "
        "gross_rs, charges_rs, net_rs, error, strategy_version, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date, variant) DO UPDATE SET "
        "expiry_mode=excluded.expiry_mode, expiry_code=excluded.expiry_code, "
        "strike_offset=excluded.strike_offset, status=excluded.status, "
        "n_trades=excluded.n_trades, gross_rs=excluded.gross_rs, "
        "charges_rs=excluded.charges_rs, net_rs=excluded.net_rs, "
        "error=excluded.error, strategy_version=excluded.strategy_version, "
        "updated_at=excluded.updated_at",
        (trade_date, variant, expiry_mode, expiry_code, strike_offset, status, n_trades,
         gross_rs, charges_rs, net_rs, error, STRATEGY_VERSION, now))


def _save_all_comparison_variants(
    conn, trade_date: str, books: dict, sim_trades: list, session_done: bool,
    *, commit: bool = True,
) -> None:
    """Price all four CONTRACT_VARIANTS and persist to comparison tables.

    Soft failure: a missing expiry or missing quote marks only that variant
    as ``unavailable`` (net_rs=NULL); the other variants are unaffected.
    Idempotent: deletes existing rows for trade_date before re-inserting.
    """
    now = datetime.now(IST).isoformat()
    conn.execute("DELETE FROM paper_contract_trades WHERE trade_date = ?", (trade_date,))
    conn.execute("DELETE FROM paper_contract_daily  WHERE trade_date = ?", (trade_date,))

    for vkey, vdef in CONTRACT_VARIANTS.items():
        mode = vdef["expiry_mode"]
        offset = vdef["strike_offset"]
        book = books.get(mode)
        expiry_code = book["expiry_code"] if book else None

        if not sim_trades:
            _insert_comparison_daily(conn, trade_date, vkey, mode, expiry_code, offset,
                                     "no_trade", 0, 0.0, 0.0, 0.0, None, now)
            continue

        if book is None:
            _insert_comparison_daily(conn, trade_date, vkey, mode, None, offset,
                                     "unavailable", 0, None, None, None,
                                     "expiry not available in shared-store data", now)
            continue

        prices = book["prices"]
        variant_trades: list | None = []
        error_msg: str | None = None
        for t in sim_trades:
            try:
                variant_trades.append(_price_trade(prices, t, offset))
            except ValueError as exc:
                error_msg = str(exc)
                variant_trades = None
                break

        if variant_trades is None:
            _insert_comparison_daily(conn, trade_date, vkey, mode, expiry_code, offset,
                                     "unavailable", 0, None, None, None, error_msg, now)
            continue

        # Apply EOD/holding label consistently with the primary.
        vstatus = "priced"
        if variant_trades and variant_trades[-1]["exit_reason"] == "EOD":
            if not session_done:
                variant_trades[-1]["exit_reason"] = "holding"
                vstatus = "open"
            else:
                variant_trades[-1]["exit_reason"] = "eod"

        for seq, pt in enumerate(variant_trades, 1):
            conn.execute(
                "INSERT INTO paper_contract_trades "
                "(trade_date, seq, variant, expiry_mode, expiry_code, side, strike, "
                "entry_ts, exit_ts, entry_spot, exit_spot, entry_prem, exit_prem, "
                "gross_rs, charges_rs, net_rs, entry_rule, exit_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_date, seq, vkey, mode, expiry_code,
                 pt["side"], pt["strike"], pt["entry_ts"], pt["exit_ts"],
                 pt["entry_spot"], pt["exit_spot"], pt["entry_prem"], pt["exit_prem"],
                 pt["gross_rs"], pt["charges_rs"], pt["net_rs"],
                 pt["entry_rule"], pt["exit_reason"]))

        gross = round(sum(pt["gross_rs"] for pt in variant_trades), 2)
        charges = round(sum(pt["charges_rs"] for pt in variant_trades), 2)
        net = round(gross - charges, 2)
        _insert_comparison_daily(conn, trade_date, vkey, mode, expiry_code, offset,
                                 vstatus, len(variant_trades), gross, charges, net, None, now)

    if commit:
        conn.commit()


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else None
    print(run_day(d))
