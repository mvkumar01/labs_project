"""Run the original V3 paper position alongside the opposite option."""

from dataclasses import asdict
from datetime import datetime
import json

from labs.engine.expiry_strangle_tracker import expiry_date_of
from live import live_service as svc
from live.engine import proposer_engine as pe
from live.engine import proposer_strike as strikes


SHADOW_CAPITAL_RS = 1_200_000
ENGINE_FIELDS = (
    "regime_stale", "regime_entry_used", "entry_decisive", "skip_cd_arm",
    "prev_open", "last_acted_asof", "day_done", "last_entry_capital",
    "bank_target_rs", "day_real_at_latch",
)


def _load(engine, raw, trade_date):
    previous = json.loads(raw) if raw else {}
    if previous.get("date") != trade_date:
        return {"date": trade_date, "book_gross": previous.get("book_gross", 0.0),
                "day_gross": 0.0, "shadow": None, "exit_reason": None}
    previous.pop("stopped", None)  # Clear latches left by the first Reverse release.
    for name, value in previous["engine"].items():
        if name in ENGINE_FIELDS:
            setattr(engine, name, value)
    cooldown = previous["cooldown"]
    engine.cooldown_until = datetime.fromisoformat(cooldown) if cooldown else None
    return previous


def _store(engine, state, user_id, conn_id, key, raw):
    state["engine"] = {name: getattr(engine, name) for name in ENGINE_FIELDS}
    state["cooldown"] = engine.cooldown_until.isoformat() if engine.cooldown_until else None
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"))
    if encoded != raw:
        svc.set_config(user_id, conn_id, key, encoded)


def _shadow_entry(engine, signal, snap, now, spot, chain, quote, lot_size):
    frame = chain()
    if frame.empty:
        return None
    expiry = expiry_date_of(frame["expiry"].iloc[0], "NIFTY")
    if expiry is None:
        return None
    symbol, _, side, _ = strikes.delta_itm_symbol_nifty(frame, signal.side, spot, now.date(), expiry)
    ask = (quote(symbol) or {}).get("ask") if symbol else None
    if not ask or ask <= 0:
        return None
    qty = int(SHADOW_CAPITAL_RS // (ask * lot_size)) * lot_size
    if qty < lot_size:
        return None
    engine.mark_entry(snap)
    engine.last_entry_capital = ask * qty
    return {"symbol": symbol, "position": asdict(pe.Position(side, ask, spot, qty))}


def _shadow_signal(engine, state, now, snap, spot, day_move, exits_ready, actual, chain, quote,
                   lot_size, entry_cutoff, eod):
    shadow = state["shadow"]
    engine.set_book(day_realized=state["day_gross"], book_net=state["book_gross"])
    if shadow:
        position = pe.Position(**shadow["position"])
        market = quote(shadow["symbol"]) or {}
        bid, ask = market.get("bid"), market.get("ask")
        mid = (bid + ask) / 2 if bid and ask and ask >= bid > 0 else None
        if now.time() >= eod:
            reason = "eod"
        elif state["exit_reason"]:
            reason = state["exit_reason"]
        elif snap and spot and exits_ready:
            decision = engine.evaluate(now, snap, position, option_ltp=mid, spot=spot)
            reason = decision.reason if decision.action == "EXIT" else None
        else:
            reason = None
        if reason:
            state["exit_reason"] = reason
            if bid and bid > 0:
                gross = (bid - position.entry_price) * position.qty
                state["day_gross"] += gross
                state["book_gross"] += gross
                state["shadow"] = None
                engine.set_book(day_realized=state["day_gross"], book_net=state["book_gross"])
                if snap:
                    engine.evaluate(now, snap, pe.Position(), option_ltp=None, spot=spot)
        return pe.Signal("HOLD")

    if actual.open or now.time() >= entry_cutoff or not (snap and spot and exits_ready):
        return pe.Signal("HOLD")
    signal = engine.evaluate(now, snap, pe.Position(), option_ltp=None, spot=spot, day_move=day_move)
    if signal.action != "ENTER":
        return signal
    state["shadow"] = _shadow_entry(engine, signal, snap, now, spot, chain, quote, lot_size)
    if not state["shadow"]:
        return pe.Signal("HOLD", reason="reverse_shadow_quote_missing")
    state["exit_reason"] = None
    side = "PUT" if signal.side == "CALL" else "CALL"
    return pe.Signal("ENTER", side, signal.reason)


def evaluate(engine, *, user_id, conn_id, dry_run, now, snap, spot, day_move, exits_ready,
             actual, chain, quote, lot_size, entry_cutoff, eod):
    trade_date = now.date().isoformat()
    key = "proposer_v3_reverse_shadow_" + ("dry" if dry_run else "live")
    raw = svc.get_config(user_id, conn_id, key)
    state = _load(engine, raw, trade_date)
    signal = _shadow_signal(engine, state, now, snap, spot, day_move, exits_ready, actual,
                            chain, quote, lot_size, entry_cutoff, eod)
    _store(engine, state, user_id, conn_id, key, raw)
    if actual.open:
        side = "CALL" if actual.side == "CE" else "PUT"
        if now.time() >= eod:
            return pe.Signal("EXIT", side, "eod")
        if state["exit_reason"]:
            return pe.Signal("EXIT", side, state["exit_reason"])
        if not state["shadow"]:
            return pe.Signal("EXIT", side, "reverse_shadow_missing")
    return signal
