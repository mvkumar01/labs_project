"""Per-connection GOLD CCI V2 execution through the common gated order ledger.

Uses GOLD (1 kg), NRML, fixed lots and IOC orders. The 3% daily goal is frozen
from a fresh broker margin quote at the first fill; actual fill prices determine
net P&L. Pending intents and processed fills survive a process restart.
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import time, timezone, timedelta

import pandas as pd

from live import gold_v2_contract as contract
from live import gold_runner as market
from live import crudem_runner as cr
from live import live_service as svc
from live import live_executor as ex
from live.engine import gold_cci_v2_engine as engine
from live.brokers.gold_zerodha import GoldZerodhaAdapter
from labs.engine.charges import mcx_futures_round_trip_charges
from storage.live_db import get_live_conn

log = logging.getLogger(__name__)
_runtime = {}
FINAL = {"COMPLETE", "COMPLETED", "FILLED", "EXECUTED", "CANCELLED", "CANCELED", "REJECTED"}
IST = timezone(timedelta(hours=5, minutes=30))


def stamp(now):
    return now.replace(tzinfo=IST).isoformat()


def ensure_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS live_gold_v2_state (user_id TEXT NOT NULL, "
                 "conn_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(user_id,conn_id))")
    conn.commit()


def load(conn, user, cid):
    row = conn.execute("SELECT payload FROM live_gold_v2_state WHERE user_id=? AND conn_id=?", (user, cid)).fetchone()
    return json.loads(row[0]) if row else {"position": None, "pending": None, "days": {}, "seq": 0}


def save(conn, user, cid, state):
    """Caller commits state, ledger accounting and the dashboard mirror together."""
    conn.execute("INSERT INTO live_gold_v2_state VALUES(?,?,?) ON CONFLICT(user_id,conn_id) "
                 "DO UPDATE SET payload=excluded.payload", (user, cid, json.dumps(state)))
    p = state.get("position") or state.get("pending")
    conn.execute("INSERT INTO live_trade_state(conn_id,user_id,position,side,symbol,entry_price,qty,"
        "entry_time,virtual,entry_rule,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(conn_id) "
        "DO UPDATE SET position=excluded.position,side=excluded.side,symbol=excluded.symbol,"
        "entry_price=excluded.entry_price,qty=excluded.qty,entry_time=excluded.entry_time,"
        "virtual=excluded.virtual,entry_rule=excluded.entry_rule,updated_at=excluded.updated_at",
        (cid, user, "OPEN" if p else "NONE", "SHORT" if p else None, p.get("symbol") if p else None,
         p.get("entry") if p else None, int(p["qty"]) * 100 if p else None,
         p.get("entry_time", p.get("created")) if p else None, int(p.get("dry", True)) if p else 0,
         contract.VERSION if p else None, ex._now_iso()))


def pnl(entry, exit_price, qty):
    units = int(qty) * 100  # broker GOLD quantity is contracts, quote/P&L is per 10 g
    fees = mcx_futures_round_trip_charges(exit_price, entry, units)
    gross = round((entry - exit_price) * units, 2)
    return gross, fees, round(gross - fees["total"], 2)


def open_mtm(user, cid, conn=None):
    own = conn is None
    conn = conn or get_live_conn()
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_gold_v2_state'").fetchone()
        p = load(conn, user, cid).get("position") if exists else None
        if not p:
            return {"open": False}
        price = p.get("last_price", p["entry"])
        gross, fees, net = pnl(p["entry"], price, p["qty"])
        return dict(open=True, side="SHORT", symbol=p["symbol"], qty=p["qty"] * 100,
            entry_price=p["entry"], entry_time=p["entry_time"], entry_rule=contract.VERSION,
            dry_run=int(p["dry"]), ltp_available=True, latest_price=price,
            latest_time=p.get("last_time", p["entry_time"]), gross_pnl=gross,
            charges={**fees, "total_charges": fees["total"]}, charges_total=fees["total"], net_pnl=net,
            net_points=p["entry"] - price)
    finally:
        if own:
            conn.close()


def pending_fire(frame, cutoff):
    """Evaluate completed bars and the researched gate/cooldown, never replay fills."""
    cutoff = pd.Timestamp(cutoff).tz_localize(None).floor("min")
    f = frame[frame.ts < cutoff].sort_values("ts")
    if f.empty or f.ts.iloc[-1] != cutoff - pd.Timedelta(minutes=1):
        return None
    px = float(f.close.iloc[-1])
    probe = pd.concat([f, pd.DataFrame([dict(ts=cutoff, open=px, high=px, low=px, close=px, volume=0)])], ignore_index=True)
    v1 = engine.v1
    g = v1.base.build_bars(probe, (cutoff + pd.Timedelta(minutes=1)).to_pydatetime())
    eligible = v1.base.entry_ok(g) & g["day_ok"][g["day_id"]]
    eligible[-1] = False
    states = v1.states(g, v1.values(g))
    states["v2_entry_filter"] = engine.entry_filter(g)["allow"]
    rule = engine.SimpleNamespace(triggers=v1.RULE.triggers, gates=v1.RULE.gates + ("v2_entry_filter",))
    for i, outcome in v1.base.member_events(rule, states, eligible):
        if outcome == "fire" and pd.Timestamp(g["ts"][i]) == cutoff - pd.Timedelta(minutes=1):
            return str(pd.Timestamp(g["ts"][i]))
    return None


def day_key(now, dry):
    return now.date().isoformat() + (":dry" if dry else ":live")


def entry_limit(adapter, intent, now=None):
    """Recheck the signal deadline after slow entry gates, immediately before send."""
    current = pd.Timestamp(now if now is not None else market.now_ist()).tz_localize(None)
    deadline = pd.Timestamp(intent["created"]).tz_localize(None).floor("min") + pd.Timedelta(seconds=20)
    return adapter.limit(intent["symbol"]) if current <= deadline else None


def reason_for(p, day, price, now):
    if p.get("exit_reason"):
        return p["exit_reason"]
    if now.date().isoformat() != p["trade_date"] or now.time() >= time(23, 29):
        return "eod"
    if price >= p["stop"]:
        return "stop at entry" if p["moved"] else "stop"
    if day["net"] + pnl(p["entry"], price, p["qty"])[2] >= day["target"]:
        return "daily_target"
    if price <= p["target"]:
        return "target"
    return None


def apply_fill(conn, user, cid, s, intent, filled, avg, now):
    """One terminal order's actual fill, atomically with state and daily totals."""
    if filled < 0 or filled > intent["qty"] or (filled and (not avg or not math.isfinite(avg))):
        raise RuntimeError("Inconsistent GOLD order fill; reconcile before proceeding")
    if filled:
        key = intent["day"]
        if intent["action"] == "ENTER":
            margin = intent["margin"] * filled / intent["qty"]
            day = s["days"].setdefault(key, dict(capital=margin, target=margin * .03, net=0.0, hit=False))
            dist, stop, target = engine.v1.levels(avg)
            s["position"] = dict(symbol=intent["symbol"], qty=filled, entry=avg,
                entry_time=stamp(now), trade_date=now.date().isoformat(), dry=intent["dry"],
                day=key, stop=math.ceil(stop), target=math.floor(target), dist=dist, best=avg,
                moved=False, minute=now.replace(second=0, microsecond=0).isoformat())
        else:
            p = s["position"]
            if filled > p["qty"]:
                raise RuntimeError("GOLD exit exceeds owned quantity")
            gross, fees, net = pnl(p["entry"], avg, filled)
            conn.execute("INSERT INTO live_trades(trade_id,user_id,conn_id,side,symbol,entry_price,exit_price,"
                "qty,pnl,gross_pnl,charges_total,charges_json,net_pnl,entry_time,exit_time,reason,strategy,dry_run) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, user, cid, "SHORT", p["symbol"], p["entry"], avg, filled * 100,
                 net, gross, fees["total"], json.dumps(fees), net, p["entry_time"], stamp(now),
                 intent["reason"], contract.VERSION, int(p["dry"])))
            daily = s["days"][p["day"]]
            daily["net"] = round(daily["net"] + net, 2)
            daily["hit"] = daily["net"] >= daily["target"]
            bucket, count = ("realized_pnl_dry", "trade_count_dry") if p["dry"] else ("realized_pnl", "trade_count")
            conn.execute(f"INSERT INTO live_day_pnl(trade_date,user_id,conn_id,realized_pnl,trade_count,"
                f"realized_pnl_dry,trade_count_dry,halted) VALUES(?,?,?,?,?,?,?,0) "
                f"ON CONFLICT(trade_date,conn_id) DO UPDATE SET "
                f"{bucket}=COALESCE({bucket},0)+excluded.{bucket},{count}=COALESCE({count},0)+1",
                (p["trade_date"], user, cid, 0 if p["dry"] else net, 0 if p["dry"] else 1,
                 net if p["dry"] else 0, 1 if p["dry"] else 0))
            p["qty"] -= filled
            if not p["qty"]:
                s["position"] = None
    s["pending"] = None
    save(conn, user, cid, s)
    conn.commit()


def settle(conn, user, cid, s, adapter, now):
    """Never resend a pending intent with another key or assume ACK means filled."""
    intent = s["pending"]
    row = svc.get_order_ledger(intent["key"], conn)
    if not row:
        if intent["action"] == "ENTER":
            deadline = pd.Timestamp(intent["created"]).tz_localize(None).floor("min") + pd.Timedelta(seconds=20)
            if pd.Timestamp(now) > deadline:
                apply_fill(conn, user, cid, s, intent, 0, None, now)
                return
        # Reuses the committed intent after a crash before ledger insertion.
        ex.place_idempotent(adapter, user_id=user, conn_id=cid, idem_key=intent["key"],
            side="SHORT", symbol=intent["symbol"], qty=intent["qty"], price=intent["price"],
            action=intent["action"], dry_run=intent["dry"], trade_date=now.date().isoformat(),
            strategy_version=contract.VERSION, bar_timestamp=intent["created"],
            entry_rule=contract.VERSION, price_fn=(lambda: entry_limit(adapter, intent))
            if intent["action"] == "ENTER" else None, conn=conn)
        row = svc.get_order_ledger(intent["key"], conn)
    if intent["dry"] and row.get("status") == "DRY_RUN":
        apply_fill(conn, user, cid, s, intent, intent["qty"], float(row["avg_fill_price"]), now)
        return
    if row.get("broker_order_id"):
        snap = adapter.get_order_status(row["broker_order_id"])
        status, filled, avg = ex._order_fields(snap)
        if status not in FINAL:
            return
        if status in ex._FILLED_STATUSES and filled != intent["qty"]:
            raise RuntimeError("Complete GOLD order has inconsistent filled quantity")
        svc.update_order_ledger(intent["key"], status=status, avg_fill_price=avg, conn=conn)
        apply_fill(conn, user, cid, s, intent, filled, avg, now)
    elif str(row.get("status") or "").upper() in {"GATE_BLOCKED", "PRICE_SKIP", "NO_SHORT_POSITION"}:
        # Definitively not sent. Entry signal is consumed; exits stay latched for retry.
        apply_fill(conn, user, cid, s, intent, 0, None, now)
    # PENDING / FAILED without an order ID is uncertain. Keep it blocked for reconciliation.
    elif not row.get("broker_order_id"):
        svc.set_config(user, cid, "reconcile_blocked", "1", conn)
        svc.set_config(user, cid, "reconcile_message", "Gold order outcome is uncertain; reconcile the broker order book before continuing.", conn)


def queue(conn, user, cid, s, *, action, symbol, qty, price, dry, now, margin=0, reason=""):
    s["seq"] += 1
    key = ex.build_idem_key(conn_id=cid, trade_date=now.date().isoformat(), strategy_version=contract.VERSION,
        bar_timestamp=now.isoformat(), action=action, side="SHORT", entry_rule=str(s["seq"]), symbol=symbol)
    s["pending"] = dict(key=key, action=action, symbol=symbol, qty=qty, price=price, dry=dry,
        created=stamp(now), margin=margin, reason=reason, day=day_key(now, dry))
    save(conn, user, cid, s)
    conn.commit()


def process(user, cid, *, adapters, now=None, series=None):
    live_clock = now is None
    now = now or market.now_ist()
    # The MCX runner uses naive IST for candle boundaries throughout.
    now = pd.Timestamp(now).tz_localize(None).to_pydatetime()
    mode = ex.get_mode(user, cid)
    if mode == ex.Mode.DISARMED:
        return
    row = svc.get_connection(user, cid)
    if row.get("broker") != "zerodha":
        return
    cache_key = "gold:" + cid
    adapter = adapters.get(cache_key)
    creds = svc.load_credentials(user, cid)
    if adapter is None or getattr(adapter, "_creds", None) != creds:
        adapter = GoldZerodhaAdapter(user_id=user, conn_id=cid, creds=creds)
        adapter.connect()
        adapters[cache_key] = adapter
    conn = get_live_conn()
    try:
        ensure_schema(conn)
        s = load(conn, user, cid)
        if s.get("pending"):
            # Persisted mode wins for an owned trade; mode switches never simulate a real exit.
            if bool(s["pending"]["dry"]) != (mode == ex.Mode.DRY_RUN):
                return
            settle(conn, user, cid, s, adapter, now)
            return
        p = s.get("position")
        if p:
            if bool(p["dry"]) != (mode == ex.Mode.DRY_RUN):
                return
            if not p["dry"]:
                actual = adapter.get_position()
                if actual.symbol != p["symbol"] or actual.qty != -p["qty"]:
                    raise RuntimeError("GOLD broker position differs from bot; reconcile before orders")
            price = adapter.get_ltp(p["symbol"])
            if not math.isfinite(price) or price <= 0:
                return
            minute = now.replace(second=0, microsecond=0).isoformat()
            if minute != p["minute"]:
                if not p["moved"] and p["entry"] - p["best"] >= p["dist"]:
                    p["stop"], p["moved"] = p["entry"], True
                p["minute"] = minute
            p["best"] = min(p["best"], price)
            p["last_price"], p["last_time"] = price, stamp(now)
            reason = reason_for(p, s["days"][p["day"]], price, now)
            if reason:
                p["exit_reason"] = reason
                queue(conn, user, cid, s, action="EXIT", symbol=p["symbol"], qty=p["qty"],
                    price=adapter.limit(p["symbol"], buy=True), dry=p["dry"], now=now, reason=reason)
                settle(conn, user, cid, s, adapter, now)
            else:
                save(conn, user, cid, s)
                conn.commit()
            return
        if now.weekday() >= 5 or not time(9, 1) <= now.time() < time(23, 29):
            return
        dry = mode == ex.Mode.DRY_RUN
        day = s["days"].get(day_key(now, dry))
        if day and day["hit"]:
            return
        if not dry and not ex.all_passed([ex.gate_daily_loss_ok(adapter, user, cid, conn),
                                          ex.gate_kill_switch_clear(user, cid, conn)]):
            return
        runtime = _runtime.setdefault(cid, {})
        cutoff = now.replace(second=0, microsecond=0)
        if s.get("last_minute") == cutoff.isoformat():
            return
        # Contract and margin quotes are prepared before evaluating this minute's signal.
        if runtime.get("date") != now.date():
            runtime.update(date=now.date(), contract=adapter.contract(now.date()), margin_at=None)
        symbol = runtime["contract"]["tradingsymbol"]
        lots = svc.get_lots(user, cid)
        if (runtime.get("margin_at") is None or (now - runtime["margin_at"]).total_seconds() > 300
                or runtime.get("lots") != lots):
            runtime.update(margin=adapter.margin(symbol, lots), margin_at=now, lots=lots)
        if series is None:
            if "series" not in runtime:
                runtime["series"] = cr.Series(market.KiteFeed(), history=market.cached_history)
            series = runtime["series"]
        series.refresh(now)
        front = series.front(now)
        if not front or front["tradingsymbol"] != symbol:
            return
        frame = series.frame(now)
        if frame.empty or frame.ts.iloc[-1] < pd.Timestamp(cutoff) - pd.Timedelta(minutes=1):
            return
        signal = pending_fire(frame, cutoff)
        s["last_minute"] = cutoff.isoformat()
        save(conn, user, cid, s)
        conn.commit()
        if live_clock:
            now = pd.Timestamp(market.now_ist()).tz_localize(None).to_pydatetime()
        if not signal or now.replace(second=0, microsecond=0) != cutoff or now.second > 20:
            return  # Never enter on a stale boot/minute signal.
        if not dry and adapter.get_position().qty:
            return
        queue(conn, user, cid, s, action="ENTER", symbol=symbol, qty=lots,
            price=adapter.limit(symbol), dry=dry, now=now, margin=runtime["margin"])
        settle(conn, user, cid, s, adapter, now)
    finally:
        conn.close()
