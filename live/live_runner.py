"""
live_runner.py — always-on poll loop; the ONLY order-placing owner.

MULTI-USER (spec §7): the loop iterates over ALL active broker connections
returned by ``live_service.active_connections()`` — every ``(user_id, conn_id)``
whose mode != DISARMED. Each connection is processed INDEPENDENTLY with its own
mode, gates, reconciliation, daily-loss, kill switch, EOD square-off,
idempotency ledger, and DB-backed trade state. An exception in one connection
is caught and logged and NEVER aborts the loop for the other connections, so
one user's failure can never affect another user.

Adapted from Bot A's runner.py + execution.py reconciliation / trade-state /
per-tier counter patterns, broker-abstracted via live.brokers.* and routed
through the single chokepoint live.live_executor.place_idempotent.

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
from live.brokers.zerodha import ZerodhaAdapter
from live.engine.signal_engine import AlphaSignalEngine
log = logging.getLogger("live.runner")

POLL_INTERVAL = 2          # seconds (mirrors Bot A runner)
LOT_SIZE = 65              # NIFTY (Bot A ZERODHA constant)
ITM_DISTANCE = 200         # Bot A _resolve_itm_option distance
EOD_EXIT_TIME = dtime(*[int(x) for x in EOD_CUTOFF.split(":")])  # 15:25 IST
_OWNER_STALE_S = 30        # a runner_owner heartbeat older than this is stale
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
    Runs every poll, independent of the signal cycle (hardens Bot A EOD)."""
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
# Contract selection (Bot A _resolve_itm_option, broker-abstracted)
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
    pnl = (float(exit_price) - entry_price) * qty
    now_iso = _now_iso()
    svc.record_trade(
        user_id,
        conn_id,
        side=position.get("side"),
        symbol=position.get("symbol"),
        entry_price=entry_price,
        exit_price=float(exit_price),
        qty=qty,
        pnl=pnl,
        entry_time=position.get("entry_time"),
        exit_time=now_iso,
        reason=reason,
        dry_run=1 if dry_run else 0,
    )
    svc.add_day_pnl(user_id, conn_id, pnl)
    if not check_daily_loss(user_id, conn_id):
        svc.set_day_halted(user_id, conn_id, 1)


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

    broker_open = pos.qty != 0
    if not db_open and not broker_open:
        ok, msg = True, "both flat"
    elif (db_open and broker_open and pos.symbol == db_symbol
          and abs(pos.qty) == abs(db_qty)):
        ok, msg = True, "both open — agree (broker truth adopted)"
    else:
        ok = False
        msg = (f"MISMATCH db={db_symbol}/{db_qty} "
               f"broker={pos.symbol}/{pos.qty} — new entries blocked")

    svc.set_config(user_id, conn_id, "reconcile_blocked", "0" if ok else "1", conn)
    svc.set_config(user_id, conn_id, "reconcile_message", "" if ok else msg, conn)
    return ReconcileResult(ok, db_symbol, db_qty, pos.symbol, pos.qty, msg)


# ══════════════════════════════════════════════════════════════════════════
# Signal — Bot A evaluate() contract (spec §5.2).
# ══════════════════════════════════════════════════════════════════════════
def get_latest_alpha():
    """Read-only latest locked hybrid alpha bar from shared market data."""
    try:
        from live.engine.alpha_hybrid import latest_hybrid_alpha

        return latest_hybrid_alpha()
    except Exception as e:
        log.warning("latest hybrid alpha unavailable: %s", type(e).__name__)
        return None


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
                    position: str, side: str | None, entry_rule=None) -> dict:
    """Bot A signal contract:
       {"action": "ENTER"|"EXIT"|"HOLD", "side": "CALL"|"PUT"|None,
        "reason": str, "rule": str|None}"""
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


# ══════════════════════════════════════════════════════════════════════════
# Per-connection cycle
# ══════════════════════════════════════════════════════════════════════════
def process_connection(user_id: str, conn_id: str, *, adapters: dict,
                       reconciled: set, task_id: str, signal_engines: dict,
                       alpha_seen: dict, adapter_factory=None) -> None:
    """One poll-cycle for a single connection (mirrors Bot A runner.main body).
    All reads/writes are scoped to (user_id, conn_id) — never another user's."""
    # Single-flight: only the owning runner process drives this connection.
    if not claim_runner_owner(user_id, conn_id, task_id):
        return

    mode = ex.get_mode(user_id, conn_id)
    if mode == ex.Mode.DISARMED:
        return  # idle for this conn — evaluate nothing, place nothing

    dry_run = mode == ex.Mode.DRY_RUN

    # Build + connect the adapter once per boot, reuse thereafter.
    adapter = adapters.get(conn_id)
    if adapter is None:
        adapter = _build_adapter(user_id, conn_id, adapter_factory)
        if adapter is None:
            return
        row = svc.get_connection(user_id, conn_id)
        try:
            adapter.connect()
            svc.upsert_connection(
                user_id,
                conn_id,
                broker=(row.get("broker") or "").lower(),
                account_label=row.get("account_label"),
                account_ref=adapter.account_ref(),
                status="connected",
            )
        except Exception as e:
            svc.upsert_connection(
                user_id,
                conn_id,
                broker=(row.get("broker") or "").lower(),
                account_label=row.get("account_label"),
                account_ref=row.get("account_ref"),
                status="disconnected",
            )
            log.warning("adapter.connect failed conn=%s: %s", conn_id, type(e).__name__)
            return
        adapters[conn_id] = adapter

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
    broker_open = pos.qty != 0
    db_open = st.get("position") == "OPEN"
    current_open = db_open if dry_run else broker_open
    current_symbol = st.get("symbol") if dry_run and db_open else pos.symbol
    current_side = st.get("side") if dry_run and db_open else pos.side
    current_qty = (
        svc.get_lots(user_id, conn_id) * LOT_SIZE
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
                       "entry_rule": sig.get("rule"), "entry_spot": alpha_bar.get("spot")})
            svc.save_trade_state(user_id, conn_id, st)

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
