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
import sys
import time
from csv import DictReader
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.labs_config import EOD_CUTOFF, SHARED_LIVE_DIR, STATE_DIR, UNDERLYINGS
from storage.live_db import init_live_db
from live import live_service as svc
from live import live_executor as ex
from live.brokers.base import Position
from live.brokers.angel import AngelAdapter
from live.notify import notify_telegram
from live.brokers.zerodha import ZerodhaAdapter
from live.engine.signal_engine import AlphaSignalEngine
log = logging.getLogger("live.runner")

POLL_INTERVAL = 2          # seconds
LOT_SIZE = 65              # NIFTY lot size
ITM_DISTANCE = 200         # ITM option distance
EOD_EXIT_TIME = dtime(*[int(x) for x in EOD_CUTOFF.split(":")])  # 15:25 IST
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


def check_daily_loss(user_id: str, conn_id: str, conn=None) -> bool:
    """True if THIS conn is still within its daily-loss cap and not halted
    (i.e. trading may continue). Mirrors the spec gate-5 semantics."""
    day = svc.get_day_pnl(user_id, conn_id, conn=conn)
    cap = svc.get_daily_loss_cap(user_id, conn_id, conn)
    realized = float(day.get("realized_pnl") or 0.0)
    halted = int(day.get("halted") or 0)
    return realized > -abs(cap) and halted == 0


def eod_watchdog(now_t: dtime) -> bool:
    """Independent EOD square-off trigger — True once at/after the cutoff.
    Runs every poll, independent of the signal cycle."""
    return now_t >= EOD_EXIT_TIME


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
def resolve_itm_option(adapter, side: str, trade_date: str | None = None) -> str:
    """Resolve a real tradingsymbol from the shared live option-chain CSV.

    This keeps live trading aligned with the collector's own market-data source
    of truth instead of guessing broker symbol formats.
    """
    spot = adapter.get_spot()
    step = UNDERLYINGS[UNDERLYING]["strike_step"]
    atm = round(spot / step) * step
    strike = atm - ITM_DISTANCE if side == "CALL" else atm + ITM_DISTANCE
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
    return min(candidates, key=lambda s: (len(s), s))


def _order_applied(status: str, *, dry_run: bool) -> bool:
    if dry_run:
        return status == "DRY_RUN"
    return status.upper() in {"COMPLETE", "COMPLETED", "FILLED", "EXECUTED"}


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
        dry_run=1 if dry_run else 0,
    )
    svc.add_day_pnl(user_id, conn_id, net_pnl)
    if not check_daily_loss(user_id, conn_id):
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


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
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

    dry_run = mode == ex.Mode.DRY_RUN

    # Build + connect at boot, then verify cached sessions each cycle.
    adapter = _ensure_connected_adapter(
        user_id, conn_id, adapters=adapters, adapter_factory=adapter_factory)
    if adapter is None:
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

    # EOD forced square-off (independent of signal cycle).
    if current_open and eod_watchdog(now_t):
        exit_price = adapter.get_ltp(current_symbol)
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
        return

    blocked = svc.get_config_int(user_id, conn_id, "reconcile_blocked") == 1
    alpha_bar = get_latest_alpha()
    if alpha_bar is None:
        return

    # Bot A/v22 PC400 trail is a per-cycle spot exit, not an alpha-bar exit.
    # Run it before alpha_seen de-duplication so repeated polls can arm/fire it.
    if current_open:
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
                exit_price = adapter.get_ltp(current_symbol)
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

    alpha_key = (alpha_bar.get("timestamp"), alpha_bar.get("alpha"))
    if alpha_seen.get(conn_id) == alpha_key:
        return
    alpha_seen[conn_id] = alpha_key

    engine = _engine_for(conn_id, signal_engines)
    sig = evaluate_signal(
        engine,
        alpha_bar,
        "OPEN" if current_open else "NONE",
        current_side,
        entry_rule=st.get("entry_rule"),
        entry_spot=st.get("entry_spot"),
    )
    log.info("signal conn=%s ts=%s tier=%s alpha=%s pos=%s sig=%s",
             conn_id, alpha_bar.get("timestamp"), alpha_bar.get("tier"),
             alpha_bar.get("alpha"), "OPEN" if current_open else "NONE", sig)

    if (sig["action"] == "ENTER" and not current_open and not blocked
            and check_daily_loss(user_id, conn_id)):
        side = sig["side"]
        symbol = resolve_itm_option(adapter, side, trade_date=_today_ist_iso())
        qty = svc.get_lots(user_id, conn_id) * LOT_SIZE
        price = adapter.get_ltp(symbol)
        result = _route_order(adapter, user_id, conn_id, action="ENTER", side=side,
                              symbol=symbol, qty=qty, price=price, dry_run=dry_run,
                              entry_rule=sig.get("rule") or "none")
        if _order_applied(result.status, dry_run=dry_run):
            state_symbol = (result.raw or {}).get("broker_symbol") or symbol
            st.update({"position": "OPEN", "side": side, "symbol": state_symbol,
                       "entry_price": result.avg_fill_price or price, "entry_time": _now_iso(),
                       "qty": qty,
                       "virtual": 1 if dry_run else 0,
                       "entry_rule": sig.get("rule"), "entry_spot": alpha_bar.get("spot")})
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
        exit_price = adapter.get_ltp(current_symbol)
        result = _route_order(adapter, user_id, conn_id, action="EXIT", side=current_side,
                              symbol=current_symbol, qty=current_qty,
                              price=exit_price, dry_run=dry_run)
        if _order_applied(result.status, dry_run=dry_run):
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
