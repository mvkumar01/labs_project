"""
live_runner.py — always-on poll loop; the ONLY order-placing owner.

MULTI-USER (spec §7): the loop iterates over ALL active broker connections
returned by ``live_service.active_connections()`` — every ``(user_id, conn_id)``
whose mode != DISARMED. Each connection is processed INDEPENDENTLY with its own
mode, gates, reconciliation, daily-loss, kill switch, EOD square-off,
idempotency ledger, and DB-backed trade state. An exception in one connection
is caught and logged and NEVER aborts the loop for the other connections, so
one user's failure can never affect another user.

The runner is broker-abstracted via live.brokers.* and routes every order
intent through the single chokepoint live.live_executor.place_idempotent.

Isolation (spec §1.4): imports ONLY live.* + neutral infra (storage.live_db,
config.labs_config). NEVER imports labs.engine.* / labs.services.*, and NEVER
imports a broker SDK directly — only the adapter classes from live.brokers
(whose SDK imports are deferred into connect()).

DRY-RUN ONLY (Phase 0): with mode=DRY_RUN (default after arm_dry_run) no broker
order is placed; even in LIVE_ARMED every adapter's place_order/exit_all raises
NotImplementedError until a deliberate Phase-1 enablement commit.
"""
import logging
import re
import sys
import time
from csv import DictReader
from dataclasses import dataclass
from datetime import date as dt_date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.labs_config import EOD_CUTOFF, MARKET_OPEN, SHARED_LIVE_DIR, STATE_DIR, UNDERLYINGS
from storage.live_db import init_live_db
from live import live_service as svc
from live import live_executor as ex
from live.brokers.base import Position
from live.brokers.angel import AngelAdapter
from live.notify import notify_telegram
from live.brokers.zerodha import ZerodhaAdapter
from live.engine.signal_engine import AlphaSignalEngine, v711_drift_update
from live.engine import champion_decider, champion_inputs
from live.engine.r2_book import (
    r2_alpha_bars, r2_signal, r2_vix_tp_exit, latest_spot_1min as r2_latest_spot,
)
from live.engine.order_manager import SourcePos, reconcile as om_reconcile, plan_orders
from market_data.expiry import select_symbol_for_expiry
log = logging.getLogger("live.runner")

POLL_INTERVAL = 2          # seconds
LOT_SIZE = 65              # NIFTY lot size
ITM_DISTANCE = 200         # ITM option distance
EOD_EXIT_TIME = dtime(*[int(x) for x in EOD_CUTOFF.split(":")])  # 15:25 IST
MARKET_OPEN_TIME = dtime(*[int(x) for x in MARKET_OPEN.split(":")])
_OWNER_STALE_S = 30        # a runner_owner heartbeat older than this is stale
PC400_TRAIL_ARM_PNL = 40.0
PC400_TRAIL_DRAWDOWN = 20.0
PC400_TRAIL_VIX_CUTOFF = 17.0
_SPOT_TRAIL_CACHE = {}
IST = timezone(timedelta(hours=5, minutes=30))
UNDERLYING = "NIFTY"

_ADAPTERS = {"angel": AngelAdapter, "zerodha": ZerodhaAdapter}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_ist_iso() -> str:
    return datetime.now(IST).date().isoformat()


def _now_ist() -> datetime:
    return datetime.now(IST)


# ══════════════════════════════════════════════════════════════════════════
# Rails — per-(user, conn): single-flight claim, daily-loss, kill, EOD, seq
# ══════════════════════════════════════════════════════════════════════════
def claim_runner_owner(user_id: str, conn_id: str, task_id: str, conn=None) -> bool:
    """Per-conn single-flight. Claim this connection for THIS runner process and
    refresh the heartbeat. Returns True if we own it (free, ours, or the prior
    owner's heartbeat went stale), else False so another live process keeps it.
    Heartbeat + owner are stored in this conn's live_config['runner_owner']."""
    owner = svc.get_config(user_id, conn_id, "runner_owner", conn) or ""
    if owner:
        prev_task, _, prev_hb = owner.partition("@")
        if prev_task and prev_task != task_id:
            try:
                hb = datetime.fromisoformat(prev_hb)
                age = (datetime.now(timezone.utc) - hb).total_seconds()
                if age < _OWNER_STALE_S:
                    return False  # someone else owns it and is alive
            except Exception:
                pass  # unparseable heartbeat -> treat as stale, reclaim
    svc.set_config(user_id, conn_id, "runner_owner", f"{task_id}@{_now_iso()}", conn)
    return True


def is_killed(user_id: str, conn_id: str, conn=None) -> bool:
    return svc.is_kill_switch_on(user_id, conn_id, conn)


def check_daily_loss(user_id: str, conn_id: str, conn=None,
                     dry_run: bool | None = None) -> bool:
    """True if THIS conn is still within its daily-loss cap and not halted
    (i.e. trading may continue). Mirrors the spec gate-5 semantics.

    `dry_run` selects which realized bucket is checked: dry losses must never
    halt live trading and live losses must never halt a dry test. When None,
    the conn's current mode decides."""
    day = svc.get_day_pnl(user_id, conn_id, conn=conn)
    cap = svc.get_daily_loss_cap(user_id, conn_id, conn)
    if dry_run is None:
        dry_run = svc.get_mode(user_id, conn_id, conn) == "DRY_RUN"
    bucket = "realized_pnl_dry" if dry_run else "realized_pnl"
    realized = float(day.get(bucket) or 0.0)
    halted = int(day.get("halted") or 0)
    return realized > -abs(cap) and halted == 0


def eod_watchdog(now_t: dtime) -> bool:
    """Independent EOD square-off trigger — True once at/after the cutoff.
    Runs every poll, independent of the signal cycle."""
    return now_t >= EOD_EXIT_TIME


def market_session_available(now: datetime) -> bool:
    """Allow processing only from weekday market open onward.

    Post-cutoff processing remains available for the EOD watchdog, while
    weekends and pre-open hours cannot consume stale alpha.
    """
    return now.weekday() < 5 and now.time() >= MARKET_OPEN_TIME


def _bar_timestamp_now() -> str:
    """Signal/idempotency bucket. Use the current completed minute in IST so a
    restart or repeated poll in the same minute reuses the same intent key."""
    now = _now_ist().replace(second=0, microsecond=0)
    return now.isoformat()


def next_intent_seq(user_id: str, conn_id: str, trade_date: str, conn=None) -> int:
    """Monotonic per-(conn, date) intent counter. Resets when the IST date
    rolls over."""
    last_date = svc.get_config(user_id, conn_id, "intent_seq_date", conn)
    if last_date != trade_date:
        svc.set_config(user_id, conn_id, "intent_seq", 0, conn)
        svc.set_config(user_id, conn_id, "intent_seq_date", trade_date, conn)
    seq = svc.get_config_int(user_id, conn_id, "intent_seq", conn) + 1
    svc.set_config(user_id, conn_id, "intent_seq", seq, conn)
    return seq


# ══════════════════════════════════════════════════════════════════════════
# Contract selection
# ══════════════════════════════════════════════════════════════════════════
def resolve_itm_option(adapter, side: str, trade_date: str | None = None,
                       distance: int = ITM_DISTANCE,
                       reference_spot: float | None = None) -> str:
    """Resolve a real tradingsymbol from the shared live option-chain CSV.

    This keeps live trading aligned with the collector's own market-data source
    of truth instead of guessing broker symbol formats. `distance` is points
    in-the-money (negative = out-of-the-money) — the funds-aware fallback steps
    it down when the account cannot cover the ITM200 premium.
    """
    # Data policy: strike selection uses Kite -> 1-min CSV, never the execution
    # broker (Angel is orders/positions only). No spot -> skip this entry.
    spot = _as_float(reference_spot) if reference_spot is not None else _fast_spot()
    if spot is None:
        raise RuntimeError("no spot for strike selection (kite + shared store dark)")
    step = UNDERLYINGS[UNDERLYING]["strike_step"]
    atm = round(spot / step) * step
    strike = atm - distance if side == "CALL" else atm + distance
    opt_type = "CE" if side == "CALL" else "PE"
    trade_date = trade_date or _today_ist_iso()
    path = SHARED_LIVE_DIR / trade_date / f"{UNDERLYING}_options_1min.csv"
    if not path.exists():
        raise RuntimeError(f"options CSV missing for {trade_date}: {path}")

    exact: set[str] = set()
    nearest: tuple[int, set[str]] | None = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = DictReader(handle)
        for row in reader:
            if (row.get("option_type") or "").strip().upper() != opt_type:
                continue
            symbol = (row.get("tradingsymbol") or "").strip()
            if not symbol:
                continue
            try:
                row_strike = int(float(row.get("strike") or 0))
            except (TypeError, ValueError):
                continue
            if row_strike == strike:
                exact.add(symbol)
                continue
            gap = abs(row_strike - strike)
            if nearest is None or gap < nearest[0]:
                nearest = (gap, {symbol})
            elif gap == nearest[0]:
                nearest[1].add(symbol)

    candidates = exact or (nearest[1] if nearest else set())
    if not candidates:
        raise RuntimeError(f"no tradingsymbol found for {UNDERLYING} {strike}{opt_type}")
    symbol = select_symbol_for_expiry(
        candidates,
        UNDERLYING,
        trade_date,
        "nearest_weekly",
    )
    if symbol is None:
        raise RuntimeError(
            f"no unexpired nearest contract found for {UNDERLYING} {strike}{opt_type}"
        )
    return symbol


def _order_applied(status: str, *, dry_run: bool) -> bool:
    if dry_run:
        return status == "DRY_RUN"
    return status.upper() in {"COMPLETE", "COMPLETED", "FILLED", "EXECUTED"}


# Marketable-limit buffer (D1). Entries/exits are placed as LIMIT at the current
# LTP, which can sit UNFILLED when the premium ticks away in the seconds after
# placement (2026-07-07 incident: a BUY limit @204.20 lagged the rising premium,
# filled LATE and untracked, so entry_spot never armed and the exit booked from a
# 0 cost basis -> phantom +11.5k). Crossing the spread by a small buffer makes the
# order marketable so it fills inside the post-placement confirmation-poll window.
# Applied LIVE-only (see _route_order) so dry-run pricing — and therefore
# completed-day replays and tests — stay byte-identical.
MARKETABLE_BUFFER_PCT = 0.006   # 0.6% — enough to cross a NIFTY option spread
OPTION_TICK = 0.05


def _marketable_limit(txn: str, price):
    """Nudge a LIMIT price across the spread: BUY pays up, SELL gives up, both
    by MARKETABLE_BUFFER_PCT and rounded to the option tick. No-op on bad price."""
    if price is None or price <= 0:
        return price
    buf = price * MARKETABLE_BUFFER_PCT
    px = price + buf if txn == "BUY" else max(OPTION_TICK, price - buf)
    return round(round(px / OPTION_TICK) * OPTION_TICK, 2)


def _order_accepted(result, *, dry_run: bool) -> bool:
    """Entry-recording gate (D2) — broader than _order_applied.

    A live entry must be persisted as OPEN the moment the broker ACCEPTS it —
    filled OR still working (open / trigger pending) — so entry_spot arms the
    spot-SL and the position has a real cost basis even if the fill lags the
    confirmation poll. EXITS keep the strict _order_applied (state is released
    only on a confirmed fill). If a working entry ultimately never fills, the
    broker-vs-DB reconcile (broker flat while DB open) clears the state next
    cycle, so this can never leave a phantom OPEN."""
    if dry_run:
        return result.status == "DRY_RUN"
    if _order_applied(result.status, dry_run=False):
        return True
    return bool(getattr(result, "broker_order_id", None)) and str(
        result.status or "").upper() not in {
        "REJECTED", "CANCELLED", "CANCELED", "FAILED", "GATE_BLOCKED",
        "NO_LONG_POSITION", "EXIT_QTY_EXCEEDS_POSITION", "PENDING",
    }


def _record_exit_result(user_id: str, conn_id: str, position: dict, *, exit_price: float,
                        qty: int, reason: str, dry_run: bool) -> None:
    entry_price = float(position.get("entry_price") or 0.0)
    qty = abs(int(qty or 0))
    pnl_info = svc.calc_net_option_pnl(entry_price, float(exit_price), qty)
    gross_pnl = float(pnl_info["gross_pnl"])
    net_pnl = float(pnl_info["net_pnl"])
    charges_total = float(pnl_info["charges"]["total_charges"])
    now_iso = _now_iso()
    svc.record_trade(
        user_id,
        conn_id,
        side=position.get("side"),
        symbol=position.get("symbol"),
        entry_price=entry_price,
        exit_price=float(exit_price),
        qty=qty,
        pnl=gross_pnl,
        entry_time=position.get("entry_time"),
        exit_time=now_iso,
        reason=reason,
        strategy=svc.get_config(user_id, conn_id, "strategy_version"),
        dry_run=1 if dry_run else 0,
    )
    # PnL buckets are segregated: a dry-run exit must never move the LIVE
    # realized number (and vice versa) — display AND daily-loss gating both
    # read the bucket matching the trade's own mode.
    svc.add_day_pnl(user_id, conn_id, net_pnl, dry_run=dry_run)
    if not check_daily_loss(user_id, conn_id, dry_run=dry_run):
        svc.set_day_halted(user_id, conn_id, 1)
    msg = (
        f"EXIT {position.get('symbol')} @ {float(exit_price)} | reason={reason} "
        f"| qty={qty} | gross={gross_pnl} | charges={charges_total} | net={net_pnl}"
    )
    if dry_run:
        msg += " [DRY-RUN]"
    notify_telegram(msg)


# ══════════════════════════════════════════════════════════════════════════
# Startup reconciliation (spec §8) — per-conn, block-on-mismatch
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class ReconcileResult:
    ok: bool
    db_symbol: str
    db_qty: int
    broker_symbol: str
    broker_qty: int
    message: str


def reconcile_on_startup(adapter, user_id: str, conn_id: str, conn=None) -> ReconcileResult:
    """Compare THIS conn's DB trade-state vs adapter.get_position().
       both flat                       -> ok
       both OPEN, same symbol & |qty|  -> ok (adopt broker truth)
       any disagreement                -> ok=False (block new entries for conn)
    On ok=False sets this conn's live_config['reconcile_blocked']='1' + message;
    EXIT of a known position remains allowed."""
    st = svc.get_trade_state(user_id, conn_id, conn=conn)
    db_open = st.get("position") == "OPEN"
    db_symbol = st.get("symbol")
    db_qty = svc.get_lots(user_id, conn_id, conn) * LOT_SIZE if db_open else 0

    try:
        pos: Position = adapter.get_position()
    except Exception as e:
        msg = f"broker position read failed: {type(e).__name__}"
        svc.set_config(user_id, conn_id, "reconcile_blocked", "1", conn)
        svc.set_config(user_id, conn_id, "reconcile_message", msg, conn)
        return ReconcileResult(False, db_symbol, db_qty, None, 0, msg)

    broker_open = pos.qty > 0
    if not db_open and not broker_open:
        ok, msg = True, "both flat"
    elif (db_open and broker_open and pos.symbol == db_symbol
          and pos.qty == abs(db_qty)):
        ok, msg = True, "both open — agree (broker truth adopted)"
    else:
        ok = False
        msg = (f"MISMATCH db={db_symbol}/{db_qty} "
               f"broker={pos.symbol}/{pos.qty} — new entries blocked")

    svc.set_config(user_id, conn_id, "reconcile_blocked", "0" if ok else "1", conn)
    svc.set_config(user_id, conn_id, "reconcile_message", "" if ok else msg, conn)
    return ReconcileResult(ok, db_symbol, db_qty, pos.symbol, pos.qty, msg)


# ══════════════════════════════════════════════════════════════════════════
# Signal contract (spec §5.2).
# ══════════════════════════════════════════════════════════════════════════
def get_latest_alpha():
    """Read-only latest locked hybrid alpha bar from shared market data."""
    try:
        from live.engine.alpha_hybrid import latest_hybrid_alpha

        return latest_hybrid_alpha()
    except Exception as e:
        log.warning("latest hybrid alpha unavailable: %s", type(e).__name__)
        return None


def get_latest_spot():
    """Read-only latest 1-minute NIFTY spot from the shared market-data CSV."""
    try:
        from live.engine.alpha_hybrid import latest_spot_1min

        return latest_spot_1min()
    except Exception as e:
        log.warning("latest spot unavailable: %s", type(e).__name__)
        return None


def get_kite_spot():
    """NIFTY index LTP from the labs Zerodha (Kite) data session — the same feed
    the collector uses. It is a DATA read: no static IP, and it does NOT touch
    the Angel execution broker's rate budget. Returns None on any failure so the
    caller can fall back to the 1-min snapshot."""
    try:
        from auth.session_manager import get_kite

        data = get_kite().ltp("NSE:NIFTY 50")
        return float(data["NSE:NIFTY 50"]["last_price"])
    except Exception as e:
        log.warning("kite spot read failed: %s", type(e).__name__)
        return None


def _fast_spot():
    """Fresh spot for non-replay consumers: Kite first, then shared 1-minute.

    Canonical v2.12 stop/recovery no longer uses this tick path; it consumes the
    paper replay's completed one-minute OHLC. This helper remains for legacy
    strike selection and never reads from the Angel execution broker.
    """
    s = get_kite_spot()
    if s is not None and s > 0:
        _log_spot_sample("kite", s)
        return s
    s = get_latest_spot()
    _log_spot_sample("csv1m", s)
    return s


def get_kite_ltp(symbol: str):
    """Option-premium LTP from the labs Kite data session. The collector's
    tradingsymbols ARE Zerodha NFO symbols, so Kite prices them directly.
    Returns None on any failure."""
    try:
        from auth.session_manager import get_kite

        key = f"NFO:{symbol}"
        return float(get_kite().ltp(key)[key]["last_price"])
    except Exception as e:
        log.warning("kite ltp read failed %s: %s", symbol, type(e).__name__)
        return None


_ANGEL_SYMBOL_RE = re.compile(
    r"^NIFTY(\d{2})([A-Z]{3})(\d{2})(\d{4,5})(CE|PE)$")
_MONTH_NUM = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_kite_symbol_cache: dict = {}


def kite_symbol_for(broker_symbol: str, trade_date: str | None = None) -> str:
    """Map an execution-broker (Angel ddMMMyy) NIFTY option symbol to the Kite
    tradingsymbol with the same strike/type/expiry DATE, via today's collector
    chain. Pure string/CSV work — no broker API (2026-07-08: every live exit's
    Kite pricing KeyError'd because current_symbol comes from the Angel book,
    e.g. NIFTY14JUL2624400PE vs Kite's NIFTY2671424400PE). Returns the input
    unchanged when it already looks like a Kite symbol or no mapping exists."""
    m = _ANGEL_SYMBOL_RE.match(str(broker_symbol).strip().upper())
    if not m:
        return broker_symbol            # already Kite-format (or unknown)
    dd, mon, yy, strike, otype = m.groups()
    month = _MONTH_NUM.get(mon)
    if month is None:
        return broker_symbol
    target = dt_date(2000 + int(yy), month, int(dd))
    trade_date = trade_date or _today_ist_iso()
    key = (trade_date, broker_symbol)
    hit = _kite_symbol_cache.get(key)
    if hit is not None:
        return hit
    from market_data.expiry import expiry_code_from_symbol, expiry_sort_date

    path = SHARED_LIVE_DIR / trade_date / f"{UNDERLYING}_options_1min.csv"
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in DictReader(handle):
                sym = (row.get("tradingsymbol") or "").strip()
                if not sym or not sym.endswith(otype):
                    continue
                try:
                    if int(float(row.get("strike") or 0)) != int(strike):
                        continue
                except (TypeError, ValueError):
                    continue
                code = expiry_code_from_symbol(sym, UNDERLYING)
                if code and expiry_sort_date(code) == target:
                    _kite_symbol_cache[key] = sym
                    return sym
    except OSError:
        pass
    return broker_symbol


def _fast_ltp(_adapter, symbol: str):
    """Order-pricing premium from Kite only, outside the static-IP proxy.

    Angel is execution/position state only. If Kite pricing is unavailable the
    intent fails closed and retries; it never fetches market data from Angel.
    Position symbols read back from the Angel book are transparently remapped
    to their Kite tradingsymbol (kite_symbol_for) before pricing.
    """
    v = get_kite_ltp(symbol)
    if v is None or v <= 0:
        alt = kite_symbol_for(symbol)
        if alt != symbol:
            v = get_kite_ltp(alt)
    if v is not None and v > 0:
        return v
    raise RuntimeError(f"no Kite LTP for {symbol} — order intent deferred")


# ── Funds-aware strike fallback ────────────────────────────────────────────
# A long option entry needs the full premium (~price x qty). With no funds
# check the ITM200 order would just be RMS-rejected and the signal sat out.
# Instead, step the strike 50 pts cheaper at a time (CALL: higher strike,
# PUT: lower) until the premium fits available funds — down to OTM100 at most
# (beyond that the delta no longer resembles the strategy the edge was
# measured on). Funds are read once per entry attempt (order-lifecycle, not
# polling); if the read fails the ITM200 attempt proceeds — the broker RMS
# stays the final authority.
AFFORDABILITY_BUFFER = 1.03      # headroom for the marketable limit + charges
MIN_STRIKE_DISTANCE = -100       # never cheaper than OTM100


def resolve_affordable_option(adapter, side: str, qty: int,
                              trade_date: str | None = None,
                              reference_spot: float | None = None):
    """Return (symbol, kite_ltp) for the deepest affordable strike, starting at
    ITM200. Raises (entry skipped) when even OTM100 does not fit."""
    try:
        funds = adapter.available_funds()
    except Exception as e:
        log.warning("funds read failed (%s) — proceeding at ITM200, broker "
                    "RMS is the final gate", type(e).__name__)
        funds = None
    distance = ITM_DISTANCE
    while distance >= MIN_STRIKE_DISTANCE:
        resolver_kwargs = {"distance": distance}
        if reference_spot is not None:
            resolver_kwargs["reference_spot"] = reference_spot
        symbol = resolve_itm_option(
            adapter, side, trade_date, **resolver_kwargs
        )
        price = _fast_ltp(adapter, symbol)          # strict Kite (entry pricing)
        if funds is None or funds >= price * qty * AFFORDABILITY_BUFFER:
            if distance != ITM_DISTANCE:
                msg = (f"⚠️ funds ₹{funds:,.0f} < ITM200 need — strike downgraded "
                       f"to {'ITM' if distance > 0 else 'OTM'}{abs(distance)} "
                       f"{symbol} @ {price}")
                log.warning(msg)
                notify_telegram(msg)
            return symbol, price
        distance -= 50
    raise RuntimeError(
        f"funds {funds} cannot cover even OTM100 x{qty} — entry skipped")


def _log_spot_sample(source: str, value) -> None:
    """Forward-capture every per-poll spot sample to logs/spot2s_DATE.csv so
    tick-vs-1min stop decisions can be replayed on real data later. Zero extra
    API cost (only samples already fetched are logged); must never raise."""
    try:
        now = _now_ist()
        path = Path(__file__).resolve().parent.parent / "logs" / (
            f"spot2s_{now.strftime('%Y-%m-%d')}.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        header = not path.exists()
        with path.open("a", encoding="utf-8") as fh:
            if header:
                fh.write("ts,source,spot\n")
            fh.write(f"{now.isoformat()},{source},"
                     f"{'' if value is None else value}\n")
    except Exception:
        pass


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ist_date_of(value) -> str | None:
    """IST calendar date (YYYY-MM-DD) of an ISO timestamp, or None."""
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).date().isoformat()
    except Exception:
        return None


def _to_ist_minute_naive(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST).replace(second=0, microsecond=0, tzinfo=None)
    except Exception:
        return None


def _csv_ts_to_ist_naive(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(second=0, microsecond=0)
        return dt.astimezone(IST).replace(second=0, microsecond=0, tzinfo=None)
    except Exception:
        return None


def get_spot_trail_snapshot(state: dict) -> dict:
    """Latest spot plus restart-safe favorable peak since entry.

    The PC400 trail must survive restarts and mid-day deploys. Rebuild the
    favorable spot excursion from the shared CSV keyed by file mtime, then
    combine it with the persisted peak_pnl in evaluate_pc400_spot_trail().
    """
    latest = get_latest_spot()
    entry_dt = _to_ist_minute_naive(state.get("entry_time"))
    entry_spot = _as_float(state.get("entry_spot"))
    side = (state.get("side") or "").upper()
    if entry_dt is None or entry_spot is None or side not in {"CALL", "PUT"}:
        return {"spot": latest, "peak_pnl": None}

    path = SHARED_LIVE_DIR / entry_dt.date().isoformat() / f"{UNDERLYING}_options_1min.csv"
    try:
        st = path.stat()
    except OSError:
        return {"spot": latest, "peak_pnl": None}

    key = (str(path), st.st_mtime_ns, st.st_size, entry_dt.isoformat(), side, entry_spot)
    cached = _SPOT_TRAIL_CACHE.get("entry")
    if cached and cached[0] == key:
        return cached[1]

    latest_csv_spot = None
    best_spot = None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = DictReader(handle)
            last_ts = None
            last_spot = None
            for row in reader:
                ts = _csv_ts_to_ist_naive(row.get("timestamp"))
                spot = _as_float(row.get("spot"))
                if ts is None or spot is None or ts < entry_dt:
                    continue
                # The CSV has one row per option contract per minute. Spot is
                # duplicated, so process each timestamp once using its last row.
                if last_ts is not None and ts != last_ts:
                    latest_csv_spot = last_spot
                    if side == "CALL":
                        best_spot = last_spot if best_spot is None else max(best_spot, last_spot)
                    else:
                        best_spot = last_spot if best_spot is None else min(best_spot, last_spot)
                last_ts = ts
                last_spot = spot
            if last_ts is not None:
                latest_csv_spot = last_spot
                if side == "CALL":
                    best_spot = last_spot if best_spot is None else max(best_spot, last_spot)
                else:
                    best_spot = last_spot if best_spot is None else min(best_spot, last_spot)
    except Exception as e:
        log.warning("spot trail snapshot unavailable: %s", type(e).__name__)
        return {"spot": latest, "peak_pnl": None}

    if best_spot is None:
        result = {"spot": latest, "peak_pnl": None}
    else:
        peak = best_spot - entry_spot if side == "CALL" else entry_spot - best_spot
        result = {"spot": latest_csv_spot if latest_csv_spot is not None else latest,
                  "peak_pnl": peak}
    _SPOT_TRAIL_CACHE["entry"] = (key, result)
    return result


def _pc400_trail_uses_spot(tier: str | None, side: str | None,
                           gap_direction: str | None, vix_at_open) -> bool:
    """Bot A/v22 PC400 trail-cell selector, broker-neutral."""
    if (tier or "").upper() not in {"PC400", "PC800"}:
        return False
    side = (side or "").upper()
    gap_direction = (gap_direction or "").upper()
    vix = _as_float(vix_at_open)
    vix_low = vix is None or vix < PC400_TRAIL_VIX_CUTOFF
    return (
        vix_low
        or (side == "PUT" and gap_direction == "UP")
        or (side == "CALL" and gap_direction == "DOWN")
    )


def evaluate_pc400_spot_trail(state: dict, alpha_bar: dict | None,
                              spot) -> dict | None:
    """Evaluate the Bot A/v22 PC400 spot trail for an open live position.

    Returns a small decision dict and the updated per-trade peak_pnl. The
    caller persists peak_pnl even when the trail is not armed/fired so a
    restart keeps the same trailing state.
    """
    if not alpha_bar or state.get("position") != "OPEN":
        return None
    side = (state.get("side") or "").upper()
    if side not in {"CALL", "PUT"}:
        return None
    entry_spot = _as_float(state.get("entry_spot"))
    observed_peak = None
    if isinstance(spot, dict):
        observed_peak = _as_float(spot.get("peak_pnl"))
        current_spot = _as_float(spot.get("spot"))
    else:
        current_spot = _as_float(spot)
    if entry_spot is None or current_spot is None:
        return None

    pnl = current_spot - entry_spot if side == "CALL" else entry_spot - current_spot
    prior_peak = _as_float(state.get("peak_pnl")) or 0.0
    peak_pnl = max(prior_peak, pnl, observed_peak or 0.0)

    tier = alpha_bar.get("tier") or alpha_bar.get("bucket")
    use_trail = _pc400_trail_uses_spot(
        tier, side, alpha_bar.get("gap_direction"), alpha_bar.get("vix_at_open")
    )

    stop = None
    should_exit = False
    if use_trail and peak_pnl >= PC400_TRAIL_ARM_PNL:
        if side == "CALL":
            stop = entry_spot + (peak_pnl - PC400_TRAIL_DRAWDOWN)
            should_exit = current_spot <= stop
        else:
            stop = entry_spot - (peak_pnl - PC400_TRAIL_DRAWDOWN)
            should_exit = current_spot >= stop

    return {
        "exit": should_exit,
        "reason": "v22_trail" if should_exit else None,
        "peak_pnl": peak_pnl,
        "pnl": pnl,
        "stop": stop,
        "use_trail": use_trail,
    }


def _engine_for(conn_id: str, signal_engines: dict) -> AlphaSignalEngine:
    engine = signal_engines.get(conn_id)
    if engine is not None:
        return engine
    safe = "".join(ch if ch.isalnum() else "_" for ch in conn_id)
    path = STATE_DIR / f"rule3_state_{safe}.json"
    engine = AlphaSignalEngine(rule3_state_path=path)
    engine.restore_rule3_state(_today_ist_iso())
    signal_engines[conn_id] = engine
    return engine


def evaluate_signal(engine: AlphaSignalEngine, alpha_bar: dict | None,
                    position: str, side: str | None, entry_rule=None,
                    entry_spot=None) -> dict:
    """Live signal contract:
       {"action": "ENTER"|"EXIT"|"HOLD", "side": "CALL"|"PUT"|None,
        "reason": str, "rule": str|None}

    `entry_spot` (the alpha-signal trigger spot captured at entry, spec §15)
    is threaded through together with the bar spot + locked VIX so the
    engine's v2.9.1 PC50 gap-UP spot exit overlay can evaluate. All three
    are optional; when absent the overlay is inert (Run F behaviour)."""
    if not alpha_bar or alpha_bar.get("alpha") is None:
        return {"action": "HOLD", "side": None, "reason": "no_alpha", "rule": None}

    ts = alpha_bar.get("timestamp")
    bar_time_ist = None
    if ts:
        try:
            bar_time_ist = datetime.fromisoformat(ts).astimezone(IST).time()
        except Exception:
            bar_time_ist = None

    return engine.evaluate(
        current_alpha=float(alpha_bar["alpha"]),
        position=position,
        side=side,
        tier=alpha_bar.get("tier") or alpha_bar.get("bucket") or "PC50",
        entry_rule=entry_rule,
        denom_alg=alpha_bar.get("denom_alg"),
        bar_time_ist=bar_time_ist,
        gap_direction=alpha_bar.get("gap_direction"),
        today_iso=alpha_bar.get("trade_date") or _today_ist_iso(),
        spot=alpha_bar.get("spot"),
        entry_spot=entry_spot,
        vix_at_open=alpha_bar.get("vix_at_open"),
    )


# ══════════════════════════════════════════════════════════════════════════
# Order routing — every order goes through the live_executor chokepoint
# ══════════════════════════════════════════════════════════════════════════
def _route_order(adapter, user_id, conn_id, *, action, side, symbol, qty, price,
                 dry_run, entry_rule="none", conn=None):
    # D1: make the LIMIT marketable so it fills inside the confirmation-poll
    # window instead of lagging the premium. LIVE only — dry-run price is left
    # untouched to keep replay/tests byte-identical.
    if not dry_run:
        price = _marketable_limit("BUY" if action == "ENTER" else "SELL", price)
    trade_date = _today_ist_iso()
    seq = next_intent_seq(user_id, conn_id, trade_date, conn)
    strategy_version = svc.get_config(user_id, conn_id, "strategy_version", conn)
    bar_ts = _bar_timestamp_now()
    idem_key = ex.build_idem_key(
        conn_id=conn_id, trade_date=trade_date, strategy_version=strategy_version,
        bar_timestamp=bar_ts, action=action, side=side or "none",
        entry_rule=entry_rule, symbol=symbol,
    )
    return ex.place_idempotent(
        adapter, user_id=user_id, conn_id=conn_id, idem_key=idem_key,
        side=side or "", symbol=symbol, qty=qty, price=price, action=action,
        dry_run=dry_run, trade_date=trade_date, strategy_version=strategy_version,
        bar_timestamp=bar_ts, entry_rule=entry_rule, intent_seq=seq, conn=conn,
    )


# ══════════════════════════════════════════════════════════════════════════
# Adapter factory — build a per-conn adapter from the stored connection + creds
# ══════════════════════════════════════════════════════════════════════════
def _build_adapter(user_id: str, conn_id: str, adapter_factory=None):
    """Construct (not connect) a broker adapter for this connection. Creds are
    decrypted in-memory only. `adapter_factory` is injectable for tests."""
    row = svc.get_connection(user_id, conn_id)
    broker = (row.get("broker") or "").lower()
    cls = (adapter_factory or _ADAPTERS.get(broker))
    if cls is None:
        log.warning("no adapter for broker=%s conn=%s", broker, conn_id)
        return None
    try:
        creds = svc.load_credentials(user_id, conn_id)  # in-memory; never logged
    except Exception as e:
        log.warning("cred load failed conn=%s: %s", conn_id, type(e).__name__)
        creds = {}
    return cls(user_id=user_id, conn_id=conn_id, creds=creds)


def _connect_adapter(user_id: str, conn_id: str, adapter, row: dict) -> bool:
    """Connect/reconnect one adapter and persist the real broker status."""
    broker = (row.get("broker") or "").lower()
    try:
        adapter.connect()
        if not adapter.is_connected():
            raise RuntimeError("broker auth ping failed")
        svc.upsert_connection(
            user_id,
            conn_id,
            broker=broker,
            account_label=row.get("account_label"),
            account_ref=adapter.account_ref(),
            status="connected",
        )
        try:
            funds = adapter.available_funds()
            svc.update_connection_funds(
                user_id, conn_id, funds,
                "" if funds is not None else "funds_unavailable",
            )
        except Exception as funds_exc:
            svc.update_connection_funds(
                user_id, conn_id, None, type(funds_exc).__name__)
        return True
    except Exception as e:
        svc.upsert_connection(
            user_id,
            conn_id,
            broker=broker,
            account_label=row.get("account_label"),
            account_ref=row.get("account_ref"),
            status="disconnected",
        )
        svc.update_connection_funds(user_id, conn_id, None, type(e).__name__)
        log.warning("adapter.connect failed conn=%s: %s", conn_id, type(e).__name__)
        return False


def _ensure_connected_adapter(user_id: str, conn_id: str, *, adapters: dict,
                              adapter_factory=None):
    """Return a connected adapter, reconnecting stale cached sessions."""
    row = svc.get_connection(user_id, conn_id)
    adapter = adapters.get(conn_id)
    if adapter is not None:
        try:
            if adapter.is_connected():
                return adapter
            log.warning("adapter auth ping failed conn=%s; reconnecting", conn_id)
        except Exception as e:
            log.warning("adapter auth ping errored conn=%s: %s",
                        conn_id, type(e).__name__)
        adapters.pop(conn_id, None)

    adapter = _build_adapter(user_id, conn_id, adapter_factory)
    if adapter is None:
        return None
    if not _connect_adapter(user_id, conn_id, adapter, row):
        adapters.pop(conn_id, None)
        return None
    adapters[conn_id] = adapter
    return adapter


# ══════════════════════════════════════════════════════════════════════════
# Order-manager mode (Alpha v2.10+) — net main + R2 onto ONE broker account.
# Pure netting/reconcile lives in order_manager.py (unit-tested); this is the
# broker/DB wiring around it. Fail-closed: any reconcile mismatch or partial
# fill blocks and defers to next cycle. Per-source ledger = live_source_ledger.
# ══════════════════════════════════════════════════════════════════════════
_OM_HOLD = object()   # sentinel: source keeps its current position (no order)


def _om_ledger_to_sourcepos(rows: dict) -> dict:
    out = {}
    for src, r in rows.items():
        out[src] = SourcePos(
            source=src, symbol=r["symbol"], side=r["side"], qty=int(r["qty"] or 0),
            entry_price=float(r["entry_price"] or 0), entry_spot=r.get("entry_spot"),
            entry_rule=r.get("entry_rule"))
    return out


def _om_enabled_sources(user_id, conn_id) -> list:
    srcs = ["main"]
    if svc.get_config_int(user_id, conn_id, "om_r2_enabled") == 1:
        srcs.append("r2")
    return srcs


def _om_desired_main(user_id, conn_id, adapter, pos, signal_engines, blocked):
    bar = get_latest_alpha()
    if bar is None or bar.get("alpha") is None:
        return _OM_HOLD
    engine = _engine_for(conn_id + "::main", signal_engines)
    sig = evaluate_signal(engine, bar, "OPEN" if pos else "NONE",
                          pos.side.lower() if pos else None,
                          entry_rule=pos.entry_rule if pos else None,
                          entry_spot=pos.entry_spot if pos else None)
    if (sig["action"] == "ENTER" and pos is None and not blocked
            and check_daily_loss(user_id, conn_id)):
        side = sig["side"]
        qty = svc.get_lots(user_id, conn_id) * LOT_SIZE
        symbol, entry_price = resolve_affordable_option(
            adapter, side, qty, trade_date=_today_ist_iso())
        return SourcePos("main", symbol, side, qty,
                         entry_price=entry_price,
                         entry_spot=bar.get("spot"), entry_rule=sig.get("rule") or "main")
    if sig["action"] == "EXIT" and pos is not None:
        return None
    return _OM_HOLD


def _om_desired_r2(user_id, conn_id, adapter, pos, blocked):
    _, bars = r2_alpha_bars()
    if not bars or bars[-1].get("alpha") is None:
        return _OM_HOLD
    bar = bars[-1]
    cur = float(bar["alpha"])
    prev = bars[-2]["alpha"] if len(bars) >= 2 else None
    if pos is not None:                          # VIX-scaled spot TP first (no SL)
        spot = r2_latest_spot() or bar.get("spot")
        if pos.entry_spot is not None and spot is not None and r2_vix_tp_exit(
                pos.side.lower(), float(pos.entry_spot), float(spot), bar.get("vix_at_open")):
            return None
    sig = r2_signal(prev, cur, "OPEN" if pos else "NONE", pos.side.lower() if pos else None)
    if (sig["action"] == "ENTER" and pos is None and not blocked
            and check_daily_loss(user_id, conn_id)):
        side = sig["side"]
        qty = svc.get_lots(user_id, conn_id) * LOT_SIZE
        symbol, entry_price = resolve_affordable_option(
            adapter, side, qty, trade_date=_today_ist_iso())
        return SourcePos("r2", symbol, side, qty,
                         entry_price=entry_price,
                         entry_spot=bar.get("spot"), entry_rule="r2")
    if sig["action"] == "EXIT" and pos is not None:
        return None
    return _OM_HOLD


def _process_om(user_id: str, conn_id: str, *, adapter, dry_run: bool,
                signal_engines: dict, alpha_seen: dict) -> None:
    # 1) Reconcile broker net book vs per-source ledger (fail-closed).
    try:
        broker_net = adapter.get_net_book()
    except Exception as e:
        log.warning("om get_net_book failed conn=%s: %s", conn_id, type(e).__name__)
        return
    rows = svc.get_ledger(user_id, conn_id)
    ledger = _om_ledger_to_sourcepos(rows)
    ok, msg = om_reconcile(ledger, broker_net)
    if not ok:
        if svc.get_config(user_id, conn_id, "reconcile_message") != msg:
            log.warning("om reconcile block conn=%s %s", conn_id, msg)
            svc.set_config(user_id, conn_id, "reconcile_blocked", "1")
            svc.set_config(user_id, conn_id, "reconcile_message", msg)
            notify_telegram(f"⚠️ OM blocked: {msg}")
        return
    svc.set_config(user_id, conn_id, "reconcile_blocked", "0")
    svc.set_config(user_id, conn_id, "reconcile_message", "")

    # 2) EOD watchdog — flatten ALL sources, then disarm a flat LIVE conn.
    if eod_watchdog(_now_ist().time()):
        desired = {src: None for src in ledger}     # flatten everything held
        if desired:
            _om_execute(user_id, conn_id, adapter, ledger, desired, broker_net,
                        dry_run, exit_reason="eod")
        if not svc.get_ledger(user_id, conn_id) and ex.get_mode(user_id, conn_id) == ex.Mode.LIVE_ARMED:
            try:
                ex.disarm(user_id, conn_id)
                notify_telegram("🔒 EOD: OM disarmed (flat)")
            except Exception as e:
                log.warning("om EOD disarm failed conn=%s: %s", conn_id, type(e).__name__)
        return

    blocked = svc.get_config_int(user_id, conn_id, "reconcile_blocked") == 1

    # 3) Build each enabled source's desired target.
    desired: dict = {}
    for src in _om_enabled_sources(user_id, conn_id):
        pos = ledger.get(src)
        if src == "r2":
            tgt = _om_desired_r2(user_id, conn_id, adapter, pos, blocked)
        else:
            tgt = _om_desired_main(user_id, conn_id, adapter, pos, signal_engines, blocked)
        if tgt is not _OM_HOLD:
            desired[src] = tgt
    if not desired:
        return

    _om_execute(user_id, conn_id, adapter, ledger, desired, broker_net, dry_run)


def _om_execute(user_id, conn_id, adapter, ledger, desired, broker_net, dry_run,
                exit_reason="signal_exit"):
    """Plan netted orders, place them, and commit the ledger + per-source
    trades ONLY if every order applied (fail-closed)."""
    orders, _ = plan_orders(ledger, desired, broker_net)
    fills: dict = {}
    all_ok = True
    for o in orders:
        side = "CALL" if o.symbol.endswith("CE") else "PUT"
        action = "ENTER" if o.txn == "BUY" else "EXIT"
        # Kite-first pricing; the broker fallback is exit-only (a Kite outage
        # must never block a flatten, but must never price an optional entry).
        price = _fast_ltp(adapter, o.symbol)
        result = _route_order(adapter, user_id, conn_id, action=action, side=side,
                              symbol=o.symbol, qty=o.qty, price=price, dry_run=dry_run,
                              entry_rule="om")
        if _order_applied(result.status, dry_run=dry_run):
            fills[o.symbol] = result.avg_fill_price or price
        else:
            all_ok = False
            log.warning("om order NOT applied conn=%s %s %s x%s",
                        conn_id, o.txn, o.symbol, o.qty)
    if not all_ok:
        svc.set_config(user_id, conn_id, "reconcile_blocked", "1")
        svc.set_config(user_id, conn_id, "reconcile_message",
                       "OM partial/failed fill — blocked; reconcile next cycle")
        notify_telegram("⚠️ OM partial fill — blocked, will reconcile")
        return

    # Commit ledger + record per-source trades.
    for src, tgt in desired.items():
        prev = ledger.get(src)
        if tgt is None and prev is not None:                 # source exited
            exit_price = fills.get(prev.symbol) or _fast_ltp(adapter, prev.symbol)
            _record_exit_result(
                user_id, conn_id,
                {"side": prev.side, "symbol": prev.symbol,
                 "entry_price": prev.entry_price, "qty": prev.qty,
                 "entry_rule": prev.entry_rule, "virtual": 1 if dry_run else 0},
                exit_price=exit_price, qty=prev.qty, reason=f"{src}:{exit_reason}",
                dry_run=dry_run)
            svc.clear_source_pos(user_id, conn_id, src)
            notify_telegram(f"🔴 OM EXIT {src} {prev.symbol} @ {exit_price}"
                            + (" [DRY]" if dry_run else ""))
        elif isinstance(tgt, SourcePos):                     # source entered
            ep = fills.get(tgt.symbol) or tgt.entry_price
            svc.set_source_pos(user_id, conn_id, src, symbol=tgt.symbol, side=tgt.side,
                               qty=tgt.qty, entry_price=ep, entry_spot=tgt.entry_spot,
                               entry_rule=tgt.entry_rule, virtual=1 if dry_run else 0)
            notify_telegram(f"🟢 OM ENTER {src} {tgt.side} {tgt.symbol} @ {ep}"
                            + (" [DRY]" if dry_run else ""))


# ══════════════════════════════════════════════════════════════════════════
# R2 consistency book — signal path (Alpha v2.10). Reuses every shared helper
# (_route_order / _record_exit_result / resolve_itm_option / check_daily_loss).
# Entries: simple ±25 alpha crossover on the R2 wall-range. Exits: VIX-scaled
# spot TP (checked FIRST, no spot SL) then alpha SL(0)/TP(±100). Sizing: this
# connection's own (small) lots. Position/reconcile/EOD are handled by the
# shared code in process_connection before this is called.
# ══════════════════════════════════════════════════════════════════════════
def _process_r2_signal(user_id: str, conn_id: str, *, adapter, st: dict,
                       current_open: bool, current_symbol, current_side,
                       current_qty: int, dry_run: bool, alpha_seen: dict) -> None:
    blocked = svc.get_config_int(user_id, conn_id, "reconcile_blocked") == 1
    state_r2, bars = r2_alpha_bars()
    if not bars:
        return
    alpha_bar = bars[-1]
    cur_alpha = alpha_bar.get("alpha")
    if cur_alpha is None:
        return
    prev_alpha = bars[-2]["alpha"] if len(bars) >= 2 else None
    trade_date = _today_ist_iso()
    side_l = (current_side or "").lower()

    # 1) VIX-scaled spot TP — checked FIRST, no spot SL (per-cycle, like the
    #    main book's PC400 trail). Uses the captured entry_spot + fresh 1-min spot.
    if current_open:
        entry_spot = _as_float(st.get("entry_spot"))
        spot = r2_latest_spot() or alpha_bar.get("spot")
        if entry_spot is not None and spot is not None:
            reason = r2_vix_tp_exit(side_l, entry_spot, float(spot),
                                    alpha_bar.get("vix_at_open"))
            if reason:
                exit_price = _fast_ltp(adapter, current_symbol)
                result = _route_order(adapter, user_id, conn_id, action="EXIT",
                                      side=current_side, symbol=current_symbol,
                                      qty=current_qty, price=exit_price, dry_run=dry_run)
                if _order_applied(result.status, dry_run=dry_run):
                    _record_exit_result(user_id, conn_id, st,
                                        exit_price=result.avg_fill_price or exit_price,
                                        qty=current_qty, reason=reason, dry_run=dry_run)
                    svc.reset_trade_state(user_id, conn_id)
                return

    # De-dup: one evaluation per completed R2 bar.
    alpha_key = ("R2", alpha_bar.get("timestamp"), cur_alpha)
    if alpha_seen.get(conn_id) == alpha_key:
        return
    alpha_seen[conn_id] = alpha_key

    sig = r2_signal(prev_alpha, float(cur_alpha),
                    "OPEN" if current_open else "NONE",
                    side_l if current_open else None)
    log.info("r2 signal conn=%s ts=%s alpha=%s prev=%s pos=%s sig=%s",
             conn_id, alpha_bar.get("timestamp"), cur_alpha, prev_alpha,
             "OPEN" if current_open else "NONE", sig)

    if (sig["action"] == "ENTER" and not current_open and not blocked
            and check_daily_loss(user_id, conn_id)):
        side = sig["side"]
        qty = svc.get_lots(user_id, conn_id) * LOT_SIZE
        symbol, price = resolve_affordable_option(
            adapter, side, qty, trade_date=trade_date)
        result = _route_order(adapter, user_id, conn_id, action="ENTER", side=side,
                              symbol=symbol, qty=qty, price=price, dry_run=dry_run,
                              entry_rule="r2")
        if _order_applied(result.status, dry_run=dry_run):
            state_symbol = (result.raw or {}).get("broker_symbol") or symbol
            st.update({"position": "OPEN", "side": side, "symbol": state_symbol,
                       "entry_price": result.avg_fill_price or price,
                       "entry_time": _now_iso(), "qty": qty,
                       "virtual": 1 if dry_run else 0, "entry_rule": "r2",
                       "entry_spot": alpha_bar.get("spot")})
            svc.save_trade_state(user_id, conn_id, st)
            msg = (f"🟢 R2 ENTER {side} {state_symbol} @ {result.avg_fill_price or price} "
                   f"| spot={alpha_bar.get('spot')} | alpha={cur_alpha}")
            notify_telegram(msg + (" [DRY-RUN]" if dry_run else ""))

    elif sig["action"] == "EXIT" and current_open:
        exit_price = _fast_ltp(adapter, current_symbol)
        result = _route_order(adapter, user_id, conn_id, action="EXIT", side=current_side,
                              symbol=current_symbol, qty=current_qty,
                              price=exit_price, dry_run=dry_run)
        if _order_applied(result.status, dry_run=dry_run):
            _record_exit_result(user_id, conn_id, st,
                                exit_price=result.avg_fill_price or exit_price,
                                qty=current_qty, reason=sig.get("reason") or "r2_signal_exit",
                                dry_run=dry_run)
            svc.reset_trade_state(user_id, conn_id)


# ══════════════════════════════════════════════════════════════════════════
# Per-connection cycle
# ══════════════════════════════════════════════════════════════════════════
def process_connection(user_id: str, conn_id: str, *, adapters: dict,
                       reconciled: set, task_id: str, signal_engines: dict,
                       alpha_seen: dict, adapter_factory=None) -> None:
    """One poll-cycle for a single live connection.
    All reads/writes are scoped to (user_id, conn_id) — never another user's."""
    # Single-flight: only the owning runner process drives this connection.
    if not claim_runner_owner(user_id, conn_id, task_id):
        return

    mode = ex.get_mode(user_id, conn_id)
    if mode == ex.Mode.DISARMED:
        return  # idle for this conn — evaluate nothing, place nothing

    if not market_session_available(_now_ist()):
        return

    dry_run = mode == ex.Mode.DRY_RUN

    # Build + connect at boot, then verify cached sessions each cycle.
    adapter = _ensure_connected_adapter(
        user_id, conn_id, adapters=adapters, adapter_factory=adapter_factory)
    if adapter is None:
        return

    # ── Order-manager mode: multi-source netting on ONE account ───────────
    # Diverges here (BEFORE the single-position reconcile/guards, which assume
    # one position). Default exec_mode="single" -> never taken. OM does its own
    # net-book reconcile + per-source ledger.
    if svc.get_exec_mode(user_id, conn_id) == "order_manager":
        if is_killed(user_id, conn_id):
            return
        _process_om(user_id, conn_id, adapter=adapter, dry_run=dry_run,
                    signal_engines=signal_engines, alpha_seen=alpha_seen)
        return

    # Reconcile once per boot per conn/mode, before any signal.
    reconcile_key = (conn_id, mode.value)
    if reconcile_key not in reconciled:
        if dry_run:
            svc.set_config(user_id, conn_id, "reconcile_blocked", "0")
            svc.set_config(user_id, conn_id, "reconcile_message", "")
            reconciled.add(reconcile_key)
            log.info("reconcile conn=%s skipped in DRY_RUN", conn_id)
        else:
            rec = reconcile_on_startup(adapter, user_id, conn_id)
            reconciled.add(reconcile_key)
            log.info("reconcile conn=%s ok=%s msg=%s", conn_id, rec.ok, rec.message)

    if is_killed(user_id, conn_id):
        return  # hard halt of new activity for this conn

    now_t = _now_ist().time()

    st = svc.get_trade_state(user_id, conn_id)

    # ── Stale-position guard (2026-06-12 incident) ────────────────────────
    # An OPEN trade state from a PREVIOUS IST date must never be acted on:
    # positions are intraday (MIS/INTRADAY) — by the next morning the broker
    # auto-squared it or the user exited manually. Acting on it would either
    # attempt a naked SELL or fabricate PnL from a day-old entry price.
    # Reset WITHOUT recording any trade.
    if st.get("position") == "OPEN":
        entry_date = _ist_date_of(st.get("entry_time"))
        if entry_date is not None and entry_date != _today_ist_iso():
            log.warning(
                "stale OPEN state from %s cleared (no trade recorded) | conn=%s sym=%s",
                entry_date, conn_id, st.get("symbol"),
            )
            svc.reset_trade_state(user_id, conn_id)
            svc.set_config(
                user_id, conn_id, "reconcile_message",
                f"stale {entry_date} position state cleared at startup — no PnL recorded",
            )
            notify_telegram(
                f"⚠️ Cleared stale {entry_date} position state for {st.get('symbol')} "
                f"(no trade recorded — verify at broker)"
            )
            st = svc.get_trade_state(user_id, conn_id)

    # ── Mode-isolation guard ──────────────────────────────────────────────
    # A position entered in LIVE mode (virtual=0) must never be exited by the
    # DRY path (it would fabricate dry PnL while real money still sits at the
    # broker), and a dry position (virtual=1) means nothing to the LIVE path.
    if dry_run and st.get("position") == "OPEN" and not int(st.get("virtual") or 0):
        msg = ("DRY mode found a LIVE-entered position state "
               f"({st.get('symbol')}) — automation paused; re-arm LIVE to manage "
               "it or resolve at the broker")
        if svc.get_config(user_id, conn_id, "reconcile_message") != msg:
            log.warning("mode mismatch | conn=%s %s", conn_id, msg)
            svc.set_config(user_id, conn_id, "reconcile_message", msg)
            notify_telegram(f"⚠️ {msg}")
        return
    if not dry_run and st.get("position") == "OPEN" and int(st.get("virtual") or 0):
        log.warning(
            "LIVE mode clearing leftover DRY position state | conn=%s sym=%s",
            conn_id, st.get("symbol"),
        )
        svc.reset_trade_state(user_id, conn_id)
        st = svc.get_trade_state(user_id, conn_id)

    try:
        pos = adapter.get_position()
    except Exception as e:
        log.warning("get_position failed conn=%s: %s", conn_id, type(e).__name__)
        return
    broker_open = pos.qty > 0
    db_open = st.get("position") == "OPEN"

    if not dry_run and db_open:
        db_symbol = str(st.get("symbol") or "").strip().upper()
        broker_symbol = str(pos.symbol or "").strip().upper()
        db_qty = int(st.get("qty") or 0) or svc.get_lots(user_id, conn_id) * LOT_SIZE
        if int(pos.qty or 0) < 0:
            msg = (
                f"MISMATCH db={db_symbol}/{db_qty} "
                f"broker={broker_symbol}/{pos.qty}; short position detected, automation blocked"
            )
            log.warning("live state mismatch | conn=%s %s", conn_id, msg)
            prior_msg = svc.get_config(user_id, conn_id, "reconcile_message")
            svc.set_config(user_id, conn_id, "reconcile_blocked", "1")
            svc.set_config(user_id, conn_id, "reconcile_message", msg)
            if prior_msg != msg:
                notify_telegram(f"LIVE automation blocked: {msg}")
            return
        if not broker_open:
            log.warning(
                "live state stale: DB open but broker has no matching long; "
                "resetting DB state | conn=%s db=%s/%s broker=%s/%s",
                conn_id, db_symbol, db_qty, broker_symbol, pos.qty,
            )
            svc.reset_trade_state(user_id, conn_id)
            svc.set_config(user_id, conn_id, "reconcile_blocked", "0")
            svc.set_config(
                user_id,
                conn_id,
                "reconcile_message",
                "DB open state cleared because broker is flat/not long",
            )
            st = svc.get_trade_state(user_id, conn_id)
            db_open = False
        elif broker_symbol != db_symbol or int(pos.qty or 0) != abs(db_qty):
            msg = (
                f"MISMATCH db={db_symbol}/{db_qty} "
                f"broker={broker_symbol}/{pos.qty}; automation blocked"
            )
            log.warning("live state mismatch | conn=%s %s", conn_id, msg)
            prior_msg = svc.get_config(user_id, conn_id, "reconcile_message")
            svc.set_config(user_id, conn_id, "reconcile_blocked", "1")
            svc.set_config(user_id, conn_id, "reconcile_message", msg)
            if prior_msg != msg:
                notify_telegram(f"LIVE automation blocked: {msg}")
            return

    current_open = db_open if dry_run else broker_open
    current_symbol = st.get("symbol") if dry_run and db_open else pos.symbol
    current_side = st.get("side") if dry_run and db_open else pos.side
    current_qty = (
        int(st.get("qty") or 0) or svc.get_lots(user_id, conn_id) * LOT_SIZE
        if dry_run and db_open
        else abs(pos.qty)
    )

    # ── EOD watchdog (independent of signal cycle) ────────────────────────
    # At/after the cutoff: square off any open position, then AUTO-DISARM a
    # LIVE_ARMED connection (a live bot must never stay armed overnight).
    # DRY_RUN stays armed — only real-money arming is turned off. No new
    # entries are ever evaluated past the cutoff.
    if eod_watchdog(now_t):
        squared_off = not current_open
        if current_open:
            exit_price = _fast_ltp(adapter, current_symbol)
            result = _route_order(adapter, user_id, conn_id, action="EXIT", side=current_side,
                                  symbol=current_symbol, qty=current_qty, price=exit_price,
                                  dry_run=dry_run)
            if _order_applied(result.status, dry_run=dry_run):
                _record_exit_result(
                    user_id,
                    conn_id,
                    st,
                    exit_price=result.avg_fill_price or exit_price,
                    qty=current_qty,
                    reason="eod",
                    dry_run=dry_run,
                )
                svc.reset_trade_state(user_id, conn_id)
                squared_off = True
        if squared_off and mode == ex.Mode.LIVE_ARMED:
            try:
                ex.disarm(user_id, conn_id)
                log.info("EOD auto-disarm | conn=%s", conn_id)
                notify_telegram("🔒 EOD: LIVE disarmed (flat) — re-arm tomorrow to trade")
            except Exception as e:
                log.warning("EOD disarm failed conn=%s: %s", conn_id, type(e).__name__)
        return

    # ── Alpha v2.10: R2 consistency book runs a different signal path ─────
    # All the shared per-conn machinery above (claim/mode/connect/reconcile/
    # stale+mode guards/position read/EOD square-off) applies to R2 too — only
    # the SIGNAL+EXIT logic differs. With book_role="main" (default) nothing
    # below changes: byte-for-byte today's behaviour.
    if svc.get_book_role(user_id, conn_id) == "r2":
        _process_r2_signal(
            user_id, conn_id, adapter=adapter, st=st, current_open=current_open,
            current_symbol=current_symbol, current_side=current_side,
            current_qty=current_qty, dry_run=dry_run, alpha_seen=alpha_seen,
        )
        return

    blocked = svc.get_config_int(user_id, conn_id, "reconcile_blocked") == 1
    alpha_bar = get_latest_alpha()
    if alpha_bar is None:
        return

    # decision_engine: "champion_replay" routes the single book through the
    # replay-to-now champion engine (live.engine.champion_decider) — the SAME
    # source of truth the paper tracker and research use (Rule 1/2/3 + v7.6/v7.7/
    # v7.8/v7.9-D2/v7.11 + trail + wall). Default "signal_engine" = legacy path.
    # In champion mode the trail + v7.11 exits live INSIDE the replay, so the
    # standalone trail/drift blocks below are skipped to avoid double exits.
    use_champion = svc.get_config(user_id, conn_id, "decision_engine") == "champion_replay"
    v212_recovery = svc.get_config(user_id, conn_id, "strategy_version") == "v2.12"

    # v2.12 decisions are canonical (the recovery-enabled replay below); the
    # tick overlay further down only ACCELERATES the stop's execution.
    # Bot A/v22 PC400 trail is a per-cycle spot exit, not an alpha-bar exit.
    # Run it before alpha_seen de-duplication so repeated polls can arm/fire it.
    if current_open and not use_champion:
        trail = evaluate_pc400_spot_trail(st, alpha_bar, get_spot_trail_snapshot(st))
        if trail is not None:
            prior_peak = _as_float(st.get("peak_pnl")) or 0.0
            if trail["peak_pnl"] > prior_peak:
                st["peak_pnl"] = trail["peak_pnl"]
                svc.save_trade_state(user_id, conn_id, st)
            if trail["exit"]:
                log.info(
                    "spot trail exit conn=%s side=%s spot_pnl=%.2f peak_pnl=%.2f stop=%s",
                    conn_id, current_side, trail["pnl"], trail["peak_pnl"], trail["stop"],
                )
                exit_price = _fast_ltp(adapter, current_symbol)
                result = _route_order(
                    adapter, user_id, conn_id, action="EXIT", side=current_side,
                    symbol=current_symbol, qty=current_qty, price=exit_price,
                    dry_run=dry_run,
                )
                if _order_applied(result.status, dry_run=dry_run):
                    _record_exit_result(
                        user_id,
                        conn_id,
                        st,
                        exit_price=result.avg_fill_price or exit_price,
                        qty=current_qty,
                        reason=trail["reason"],
                        dry_run=dry_run,
                    )
                    svc.reset_trade_state(user_id, conn_id)
                return

    # v7.11 PC400 gap-DN PUT drift-protective stop. Ported 2026-06-19 — it was
    # defined (v711_drift_update) but never wired into the live path, so the bot
    # diverged from the validated research sim (which exits the drifted PUT at
    # break-even instead of riding it to the alpha SL). Per-trade state lives in
    # the trade-state dict; compares the completed bar spot to the protective stop.
    if current_open and (current_side or "").upper() == "PUT" and not use_champion:
        _tier = (alpha_bar.get("tier") or alpha_bar.get("bucket") or "").upper()
        _gap = (alpha_bar.get("gap_direction") or "").upper()
        _es = _as_float(st.get("entry_spot"))
        _ca = _as_float(alpha_bar.get("alpha"))
        _cs = _as_float(alpha_bar.get("spot"))
        if _tier in ("PC400", "PC800") and _gap == "DOWN" and _es is not None and _ca is not None:
            dd = v711_drift_update(
                st.get("drift_min_alpha"), bool(st.get("drift_confirmation_reached")),
                bool(st.get("drift_protective_armed")), st.get("drift_protective_stop"),
                _ca, _es)
            if any(st.get(k) != v for k, v in dd.items()):
                st.update(dd)
                svc.save_trade_state(user_id, conn_id, st)
            if (dd["drift_protective_armed"] and dd["drift_protective_stop"] is not None
                    and _cs is not None and _cs >= dd["drift_protective_stop"]):
                exit_price = _fast_ltp(adapter, current_symbol)
                result = _route_order(adapter, user_id, conn_id, action="EXIT",
                                      side=current_side, symbol=current_symbol,
                                      qty=current_qty, price=exit_price, dry_run=dry_run)
                if _order_applied(result.status, dry_run=dry_run):
                    _record_exit_result(
                        user_id, conn_id, st,
                        exit_price=result.avg_fill_price or exit_price,
                        qty=current_qty, reason="v711_drift_stop", dry_run=dry_run)
                    svc.reset_trade_state(user_id, conn_id)
                return

    # ── v2.12 tick-stop overlay (fast out, patient back in) ──────────────
    # The decision stream stays canonical (the recovery-enabled replay below);
    # this overlay only ACCELERATES the entry-spot stop's execution. When the
    # live Kite spot crosses the replay's anchored barrier intra-candle, exit
    # at market NOW instead of waiting for the candle to complete (up to ~59s
    # earlier — the tail-cutting that is v2.12's measured edge). The champion
    # cursor is deliberately NOT advanced here: the replay books the same stop
    # at the candle close, and the already-flat acknowledgement in the champion
    # branch consumes that event, so live and paper record the SAME canonical
    # segment. Re-entry is never taken here — it stays canonical. A missing
    # tick spot or anchor skips silently (never a spurious stop).
    if current_open and use_champion and v212_recovery:
        _anchor = _as_float(st.get("entry_spot"))
        _tick = _as_float(_fast_spot())
        _side_u = (current_side or "").upper()
        if _anchor is not None and _tick is not None and (
                (_side_u == "CALL" and _tick <= _anchor)
                or (_side_u == "PUT" and _tick >= _anchor)):
            exit_price = _fast_ltp(adapter, current_symbol)
            result = _route_order(
                adapter, user_id, conn_id, action="EXIT", side=current_side,
                symbol=current_symbol, qty=current_qty, price=exit_price,
                dry_run=dry_run)
            if _order_applied(result.status, dry_run=dry_run):
                _record_exit_result(
                    user_id, conn_id, st,
                    exit_price=result.avg_fill_price or exit_price,
                    qty=current_qty, reason="ENTRY_SPOT_SL_TICK",
                    dry_run=dry_run)
                # reset preserves the champion cursor, so the replay's own
                # stop event acks while flat instead of re-exiting.
                svc.reset_trade_state(user_id, conn_id)
                log.info(
                    "v2.12 tick stop conn=%s side=%s anchor=%.2f spot=%.2f",
                    conn_id, _side_u, _anchor, _tick)
            return

    trade_date = _today_ist_iso()
    alpha_key = (alpha_bar.get("timestamp"), alpha_bar.get("alpha"))
    if use_champion and v212_recovery:
        # Re-evaluate when either an exact-mark alpha arrives or a new completed
        # one-minute OHLC candle arrives. This is the paper replay's event clock.
        alpha_key += (champion_inputs.latest_completed_ohlc_minute(trade_date),)
    if alpha_seen.get(conn_id) == alpha_key:
        return
    alpha_seen[conn_id] = alpha_key

    if use_champion:
        # v2.12 uses the exact same replay flags as paper. The persisted closed
        # count below prevents a same-side stop/re-entry from collapsing to HOLD.
        # At the boundary immediately after a completed trigger candle, Kite
        # historical OHLC does not yet contain the new candle. The current Kite
        # index spot is the live executable proxy for that next-candle open.
        live_execution_spot = _fast_spot() if v212_recovery else None
        target = champion_decider.champion_target(
            trade_date, now_ist=_now_ist(),
            enable_entry_spot_recovery=v212_recovery,
            live_execution_spot=live_execution_spot)
        target_closed_count = int((target or {}).get("n_closed") or 0)
        target_event_id = (target or {}).get("last_closed_event_id")
        if v212_recovery and st.get("champion_trade_date") != trade_date:
            # First observation of a date adopts the canonical replay cursor;
            # historical events cannot safely be sent to a broker after startup.
            st.update({
                "champion_trade_date": trade_date,
                "champion_closed_count": target_closed_count,
                "champion_last_event_id": target_event_id,
            })
            svc.save_trade_state(user_id, conn_id, st)
        closed_count_seen = int(st.get("champion_closed_count") or 0)
        if v212_recovery:
            sig = champion_decider.reconcile_replay_event(
                target, current_side if current_open else None,
                closed_count_seen=closed_count_seen,
            )
        else:
            sig = champion_decider.reconcile(
                target, current_side if current_open else None
            )
        replay_event_pending = (
            v212_recovery and target_closed_count > closed_count_seen
        )
        # If already flat, acknowledge the closed segment now; the final replay
        # target below may immediately request its canonical recovery re-entry.
        if replay_event_pending and not current_open:
            st.update({
                "champion_closed_count": target_closed_count,
                "champion_last_event_id": target_event_id,
            })
            svc.save_trade_state(user_id, conn_id, st)
        champ_entry_spot = (target or {}).get("entry_spot")
        # Anchor self-heal: the replay is canonical for the stop barrier. If a
        # late-arriving candle revises the replay's anchored entry_spot, the
        # persisted anchor (which drives the tick overlay above) follows it —
        # 2026-07-08: a 3-pt live-vs-paper anchor gap held a stop 8 minutes
        # past paper's exit and cost -29 premium points.
        if (v212_recovery and current_open and champ_entry_spot is not None
                and (target or {}).get("position") == (current_side or "").upper()):
            _canon_anchor = _as_float(champ_entry_spot)
            if (_canon_anchor is not None
                    and _as_float(st.get("entry_spot")) != _canon_anchor):
                st["entry_spot"] = _canon_anchor
                svc.save_trade_state(user_id, conn_id, st)
                log.info("v2.12 anchor synced to replay conn=%s entry_spot=%.2f",
                         conn_id, _canon_anchor)
        log.info("champion conn=%s ts=%s pos=%s target=%s sig=%s",
                 conn_id, alpha_bar.get("timestamp"),
                 "OPEN" if current_open else "NONE", target, sig)
    else:
        replay_event_pending = False
        target_closed_count = 0
        target_event_id = None
        engine = _engine_for(conn_id, signal_engines)
        sig = evaluate_signal(
            engine,
            alpha_bar,
            "OPEN" if current_open else "NONE",
            current_side,
            entry_rule=st.get("entry_rule"),
            entry_spot=st.get("entry_spot"),
        )
        champ_entry_spot = None
        log.info("signal conn=%s ts=%s tier=%s alpha=%s pos=%s sig=%s",
                 conn_id, alpha_bar.get("timestamp"), alpha_bar.get("tier"),
                 alpha_bar.get("alpha"), "OPEN" if current_open else "NONE", sig)

    if (sig["action"] == "ENTER" and not current_open and not blocked
            and check_daily_loss(user_id, conn_id)):
        side = sig["side"]
        qty = svc.get_lots(user_id, conn_id) * LOT_SIZE
        symbol, price = resolve_affordable_option(
            adapter, side, qty, trade_date=trade_date,
            reference_spot=champ_entry_spot if use_champion else None)
        if use_champion and v212_recovery:
            # Unknown/rejected outcomes must be retried idempotently next poll.
            alpha_seen.pop(conn_id, None)
        result = _route_order(adapter, user_id, conn_id, action="ENTER", side=side,
                              symbol=symbol, qty=qty, price=price, dry_run=dry_run,
                              entry_rule=sig.get("rule") or "none")
        if _order_accepted(result, dry_run=dry_run):  # D2: record working fills too
            state_symbol = (result.raw or {}).get("broker_symbol") or symbol
            st.update({"position": "OPEN", "side": side, "symbol": state_symbol,
                       "entry_price": result.avg_fill_price or price, "entry_time": _now_iso(),
                       "qty": qty,
                       "virtual": 1 if dry_run else 0,
                       "entry_rule": sig.get("rule"),
                       "entry_spot": champ_entry_spot if use_champion else alpha_bar.get("spot"),
                       "drift_min_alpha": None, "drift_confirmation_reached": False,
                       "drift_protective_armed": False, "drift_protective_stop": None})
            svc.save_trade_state(user_id, conn_id, st)
            entry_price = result.avg_fill_price or price
            msg = (
                f"🟢 ENTER {side} {state_symbol} @ {entry_price} | rule={sig.get('rule')} "
                f"| spot={alpha_bar.get('spot')} | alpha={alpha_bar.get('alpha')}"
            )
            if dry_run:
                msg += " [DRY-RUN]"
            notify_telegram(msg)

    elif sig["action"] == "EXIT" and current_open:
        exit_price = _fast_ltp(adapter, current_symbol)
        if use_champion and v212_recovery:
            # On success this also permits the canonical recovery ENTER on the
            # next poll, without waiting for another minute or alpha mark.
            alpha_seen.pop(conn_id, None)
        result = _route_order(adapter, user_id, conn_id, action="EXIT", side=current_side,
                              symbol=current_symbol, qty=current_qty,
                              price=exit_price, dry_run=dry_run)
        if _order_applied(result.status, dry_run=dry_run):
            if replay_event_pending:
                st.update({
                    "champion_closed_count": target_closed_count,
                    "champion_last_event_id": target_event_id,
                })
                svc.save_trade_state(user_id, conn_id, st)
            _record_exit_result(
                user_id,
                conn_id,
                st,
                exit_price=result.avg_fill_price or exit_price,
                qty=current_qty,
                reason=sig.get("reason") or "signal_exit",
                dry_run=dry_run,
            )
            svc.reset_trade_state(user_id, conn_id)


# ══════════════════════════════════════════════════════════════════════════
# Main loop — iterate ALL active connections
# ══════════════════════════════════════════════════════════════════════════
def run(task_id: str = "live_runner", max_cycles: int = None,
        adapter_factory=None) -> None:
    """PA always-on entry. Boots the live schema, then loops forever (or
    `max_cycles` for tests), each pass iterating EVERY active connection and
    processing it independently. `adapter_factory` is injectable for tests."""
    init_live_db()
    log.info("live_runner boot | task=%s", task_id)

    adapters: dict = {}     # conn_id -> live adapter (built once, reused)
    reconciled: set = set()  # conn_ids reconciled this boot
    signal_engines: dict = {}
    alpha_seen: dict = {}
    cycles = 0
    while True:
        try:
            for (user_id, conn_id) in svc.active_connections():
                try:
                    process_connection(
                        user_id, conn_id, adapters=adapters,
                        reconciled=reconciled, task_id=task_id,
                        signal_engines=signal_engines, alpha_seen=alpha_seen,
                        adapter_factory=adapter_factory,
                    )
                except Exception as e:
                    # One connection's failure NEVER aborts the others.
                    log.error("conn %s cycle error: %s", conn_id, e)
        except Exception as e:
            log.error("runner loop error: %s", e)

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            log.info("live_runner stopping after %d cycles", cycles)
            return
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
