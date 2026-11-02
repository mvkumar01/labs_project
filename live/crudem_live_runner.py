"""Per-connection CRUDEOILM combination execution through the common gated order ledger.

Decided by Vipin 2026-10-08: Angel One, a stop resting at the broker, target and session close by the bot. The
signals are the engine's (live/engine/crudem_combo_engine.py), on the same series as the dry runner and paper book.

What differs from the dry runner (live/crudem_runner.py):
  - the decision waits SETTLE_S into the minute. The dry runner decides 0-2 s after it, and 3 of its 4 unexplained
    differences from paper in Oct 2026 flip on a 1-3 point change in that candle's close: the candle was not final;
  - the entry is an IOC limit through the touch, not a fill at the last price, and a signal more than
    ENTRY_WINDOW_S into the minute is skipped;
  - the fill sets the stop and target. The resting stop is sent at once, and if the broker refuses or later kills it
    the position is closed: this strategy does not hold a position without a stop at the broker;
  - an exit by the bot first cancels the stop and sees it dead, so two exits can never both fill and reverse the
    position. A stop that trades through its limit is closed by the bot (stop_missed).

Product is CARRYFORWARD, so a runner that is down at 23:29 carries the position overnight with the day's stop gone.
Disarming with a position open leaves the stop resting but no target or close: close such a position by hand.

Pending intents survive a restart. ACK is not a fill. A request whose outcome is unknown blocks entries for
reconciliation, and a position that differs from the broker's blocks the bot and alerts.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time, timedelta, timezone

import pandas as pd

from live import crudem_contract as contract
from live import crudem_runner as cr
from live import live_executor as ex
from live import live_service as svc
from live.brokers.crudem_angel import STOP_LIMIT_PTS, CrudemAngelAdapter
from live.engine import crudem_combo_engine as eng
from live.notify import notify_telegram
from storage.live_db import get_live_conn

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
SETTLE_S = 5                # Kite's 1-minute candle is read this long after its close (see the header)
ENTRY_WINDOW_S = 25         # the dry runner's CANDLE_WAIT_S: later than this a signal is history
FIRST_MINUTE, LAST_ENTRY = time(9, 1), cr.EOD_EXIT
MARGIN_REFRESH_S = 300
CANCEL_RETRY_S = 5
STOP_RECHECK_S = 60
POSITION_GRACE_S = 10       # the position book can lag the order book just after a fill
NOT_SENT = {"GATE_BLOCKED", "PRICE_SKIP", "NO_POSITION"}
LABEL = {1: "LONG", -1: "SHORT"}
_runtime: dict = {}


def stamp(now: datetime) -> str:
    return now.replace(tzinfo=IST).isoformat()


def clock(raw: str) -> datetime:
    return datetime.fromisoformat(raw).replace(tzinfo=None)


def now_ist() -> datetime:
    return pd.Timestamp(cr.now_ist()).to_pydatetime()


def ensure_schema(conn) -> None:
    cr.ensure_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS live_crudem_live_state (user_id TEXT NOT NULL, "
                 "conn_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(user_id,conn_id))")
    conn.commit()


def load(conn, user: str, cid: str) -> dict:
    row = conn.execute("SELECT payload FROM live_crudem_live_state WHERE user_id=? AND conn_id=?", (user, cid)).fetchone()
    return json.loads(row[0]) if row else {"position": None, "pending": None, "seq": 0, "last_minute": None}


def save(conn, user: str, cid: str, state: dict) -> None:
    """Caller commits state, ledger accounting and the dashboard mirror together."""
    conn.execute("INSERT INTO live_crudem_live_state VALUES(?,?,?) ON CONFLICT(user_id,conn_id) "
                 "DO UPDATE SET payload=excluded.payload", (user, cid, json.dumps(state)))
    p = state.get("position")
    entering = None if p else state.get("pending")
    held = p or entering
    side = LABEL[held["side"]] if held else None
    conn.execute("INSERT INTO live_trade_state(conn_id,user_id,position,side,symbol,entry_price,qty,"
        "entry_time,virtual,entry_rule,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(conn_id) "
        "DO UPDATE SET position=excluded.position,side=excluded.side,symbol=excluded.symbol,"
        "entry_price=excluded.entry_price,qty=excluded.qty,entry_time=excluded.entry_time,"
        "virtual=excluded.virtual,entry_rule=excluded.entry_rule,updated_at=excluded.updated_at",
        (cid, user, "OPEN" if held else "NONE", side, held["symbol"] if held else None,
         held.get("entry") if held else None, held["qty"] if held else None,
         held.get("entry_time", held.get("created")) if held else None, int(held["dry"]) if held else 0,
         contract.VERSION if held else None, ex._now_iso()))


def pnl(side: int, entry: float, exit_price: float, qty: int) -> tuple[float, float, float]:
    gross = round(side * (exit_price - entry) * qty, 2)
    buy, sell = (entry, exit_price) if side > 0 else (exit_price, entry)
    fees = round(cr.charges(buy, sell, qty), 2)
    return gross, fees, round(gross - fees, 2)


def open_mtm(user: str, cid: str, conn=None) -> dict:
    own = conn is None
    conn = conn or get_live_conn()
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_crudem_live_state'").fetchone()
        p = load(conn, user, cid).get("position") if exists else None
        if not p:
            return {"open": False}
        price = p.get("last_price", p["entry"])
        gross, fees, net = pnl(p["side"], p["entry"], price, p["qty"])
        return dict(open=True, side=LABEL[p["side"]], symbol=p["symbol"], qty=p["qty"], entry_price=p["entry"],
                    entry_time=p["entry_time"], entry_rule=contract.VERSION, dry_run=int(p["dry"]), ltp_available=True,
                    latest_price=price, latest_time=p.get("last_time", p["entry_time"]), gross_pnl=gross,
                    charges={"total_charges": fees}, charges_total=fees, net_pnl=net,
                    net_points=p["side"] * (price - p["entry"]))
    finally:
        if own:
            conn.close()


def alert(text: str) -> None:
    log.warning("%s", text)
    notify_telegram(f"[CRUDEOILM] {text}")


def block(conn, user: str, cid: str, s: dict, message: str) -> None:
    """Hold this connection for a person. Sent once; cleared by the configure page's broker-flat refresh."""
    if svc.get_config(user, cid, "reconcile_blocked", conn) != "1":
        alert(message)
    svc.set_config(user, cid, "reconcile_blocked", "1", conn)
    svc.set_config(user, cid, "reconcile_message", message, conn)
    save(conn, user, cid, s)
    conn.commit()


def decision(conn, cid: str, fire: dict, outcome: str, detail, now: datetime) -> None:
    conn.execute("INSERT OR IGNORE INTO live_crudem_decisions (book, signal_ts, cid, outcome, detail, decided_at) "
                 "VALUES (?,?,?,?,?,?)", (f"live:{cid}", str(fire["signal_ts"]), fire["cid"], outcome, detail,
                                          now.isoformat(timespec="seconds")))


def book_exit(conn, user: str, cid: str, s: dict, filled: int, avg: float, reason: str, now: datetime) -> None:
    """One exit fill: the trade row, the day's totals and the remaining position, committed together."""
    p = s["position"]
    if filled > p["qty"]:
        raise RuntimeError("CRUDEOILM exit exceeds owned quantity")
    gross, fees, net = pnl(p["side"], p["entry"], avg, filled)
    conn.execute("INSERT INTO live_trades(trade_id,user_id,conn_id,side,symbol,entry_price,exit_price,"
        "qty,pnl,gross_pnl,charges_total,charges_json,net_pnl,entry_time,exit_time,reason,strategy,dry_run) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, user, cid, LABEL[p["side"]], p["symbol"], p["entry"], avg, filled, net, gross, fees,
         json.dumps({"total": fees}), net, p["entry_time"], stamp(now), f"{reason} (rule {p['rule']})",
         contract.VERSION, int(p["dry"])))
    bucket, count = ("realized_pnl_dry", "trade_count_dry") if p["dry"] else ("realized_pnl", "trade_count")
    conn.execute(f"INSERT INTO live_day_pnl(trade_date,user_id,conn_id,realized_pnl,trade_count,"
        f"realized_pnl_dry,trade_count_dry,halted) VALUES(?,?,?,?,?,?,?,0) "
        f"ON CONFLICT(trade_date,conn_id) DO UPDATE SET "
        f"{bucket}=COALESCE({bucket},0)+excluded.{bucket},{count}=COALESCE({count},0)+1",
        (p["trade_date"], user, cid, 0 if p["dry"] else net, 0 if p["dry"] else 1,
         net if p["dry"] else 0, 1 if p["dry"] else 0))
    tag = "DRY-RUN" if p["dry"] else "LIVE"
    notify_telegram(f"[{tag}] CRUDEOILM exit ({reason}) {filled} @ {avg:.0f} | rule {p['rule']} | net Rs {net:,.0f}")
    p["qty"] -= filled
    if not p["qty"]:
        s["position"] = None


def book_entry(conn, user: str, cid: str, s: dict, intent: dict, filled: int, avg: float, now: datetime) -> None:
    member = eng.MEMBER_BY_CID[intent["rule"]]
    dist, stop, target = eng.levels(member, avg, intent["atr"])
    stop, target = cr.tick_levels(member.side, stop, target)
    s["position"] = dict(symbol=intent["symbol"], side=member.side, qty=filled, entry=avg, entry_time=stamp(now),
                         trade_date=now.date().isoformat(), dry=intent["dry"], rule=member.cid,
                         signal_ts=intent["signal_ts"], stop_dist=dist, stop=stop, target=target,
                         stop_order=None, stop_failed=None, stop_booked=0, exit_reason=None)
    tag = "DRY-RUN" if intent["dry"] else "LIVE"
    notify_telegram(f"[{tag}] CRUDEOILM {LABEL[member.side]} {filled} {intent['symbol']} @ {avg:.0f} | "
                    f"rule {member.cid} | stop {stop:.0f} target {target:.0f}")


def finish(conn, user: str, cid: str, s: dict, intent: dict, filled: int, avg, now: datetime) -> None:
    """One terminal order's actual fill, atomically with state and daily totals."""
    if filled < 0 or filled > intent["qty"] or (filled and not avg):
        raise RuntimeError("Inconsistent CRUDEOILM order fill; reconcile before proceeding")
    if filled and intent["action"] == "ENTER":
        book_entry(conn, user, cid, s, intent, filled, avg, now)
    elif filled:
        book_exit(conn, user, cid, s, filled, avg, intent["reason"], now)
    s["pending"] = None
    save(conn, user, cid, s)
    conn.commit()


def request_outcome(conn, cid: str, key: str):
    row = conn.execute("SELECT outcome FROM live_order_requests WHERE conn_id=? AND intent_key=? ORDER BY rowid DESC LIMIT 1",
                       (cid, key)).fetchone()
    return row[0] if row else None


def past_window(intent: dict, now: datetime) -> bool:
    deadline = pd.Timestamp(intent["created"]).tz_localize(None).floor("min") + pd.Timedelta(seconds=ENTRY_WINDOW_S)
    return pd.Timestamp(now).tz_localize(None) > deadline


def entry_price(adapter, intent: dict, now=None):
    """Recheck the signal's window after slow entry gates, immediately before send."""
    return None if past_window(intent, now or now_ist()) else adapter.limit(intent["symbol"], buy=intent["side"] > 0)


def send(conn, user: str, cid: str, adapter, intent: dict, now: datetime) -> None:
    ex.place_idempotent(adapter, user_id=user, conn_id=cid, idem_key=intent["key"], side=LABEL[intent["side"]],
        symbol=intent["symbol"], qty=intent["qty"], price=intent["price"], action=intent["action"],
        dry_run=intent["dry"], trade_date=now.date().isoformat(), strategy_version=contract.VERSION,
        bar_timestamp=intent["created"], entry_rule=contract.VERSION,
        price_fn=(lambda: entry_price(adapter, intent)) if intent["action"] == "ENTER" else None, conn=conn)


def settle_stop(conn, user: str, cid: str, s: dict, intent: dict, row: dict) -> None:
    p = s["position"]
    if row.get("broker_order_id"):
        p["stop_order"] = dict(id=row["broker_order_id"], key=intent["key"], state="sent", tries=0)
    elif str(row.get("status") or "").upper() in NOT_SENT or request_outcome(conn, cid, intent["key"]) in (None, "rejected"):
        p["stop_failed"] = str(row.get("status") or "not placed")
        alert(f"resting stop not placed ({p['stop_failed']}) on {p['symbol']}; closing the position")
    else:
        s["pending"] = None
        block(conn, user, cid, s, "CRUDEOILM stop outcome is uncertain; check the broker's order book before continuing.")
        return
    s["pending"] = None
    save(conn, user, cid, s)
    conn.commit()


def settle(conn, user: str, cid: str, s: dict, adapter, now: datetime) -> None:
    """Never resend a pending intent with another key or assume ACK means filled."""
    intent = s["pending"]
    row = svc.get_order_ledger(intent["key"], conn)
    if not row:
        if intent["action"] == "ENTER" and past_window(intent, now):
            finish(conn, user, cid, s, intent, 0, None, now)
            return
        send(conn, user, cid, adapter, intent, now)     # reuses the committed intent after a crash before the ledger
        row = svc.get_order_ledger(intent["key"], conn)
    status = str(row.get("status") or "").upper()
    if intent["action"] == "STOP":
        settle_stop(conn, user, cid, s, intent, row)
        return
    if intent["dry"] and status == "DRY_RUN":
        finish(conn, user, cid, s, intent, intent["qty"], float(row["avg_fill_price"]), now)
    elif row.get("broker_order_id"):
        order = adapter.get_order_status(row["broker_order_id"])
        status, filled, avg = ex._order_fields(order)
        if status not in ex._FILLED_STATUSES | ex._DEAD_STATUSES:
            return
        if status in ex._FILLED_STATUSES and filled != intent["qty"]:
            # Barrels were proven on Angel's margin calculator, not on a fill. A broker reporting otherwise must
            # reach a person at once: an entry here is a real position that has no stop yet.
            block(conn, user, cid, s, f"CRUDEOILM {intent['action']} complete with {filled} filled of {intent['qty']}: "
                                      "check the position at the broker now, no stop was placed.")
            return
        svc.update_order_ledger(intent["key"], status=status, avg_fill_price=avg, conn=conn)
        finish(conn, user, cid, s, intent, filled, avg, now)
    elif status in NOT_SENT or (status == "FAILED" and request_outcome(conn, cid, intent["key"]) in (None, "rejected")):
        finish(conn, user, cid, s, intent, 0, None, now)  # entry consumed; a latched exit goes again next cycle
    else:       # PENDING or FAILED with no order id: it may have reached the broker
        block(conn, user, cid, s, "CRUDEOILM order outcome is uncertain; check the broker's order book before continuing.")


def queue(conn, user: str, cid: str, s: dict, *, action: str, symbol: str, qty: int, side: int, price: float, dry: bool,
          now: datetime, reason: str = "", rule=None, atr=None, signal_ts=None) -> None:
    s["seq"] += 1
    key = ex.build_idem_key(conn_id=cid, trade_date=now.date().isoformat(), strategy_version=contract.VERSION,
        bar_timestamp=now.isoformat(), action=action, side=LABEL[side], entry_rule=str(s["seq"]), symbol=symbol)
    s["pending"] = dict(key=key, action=action, symbol=symbol, qty=qty, side=side, price=price, dry=dry,
                        created=stamp(now), reason=reason, rule=rule, atr=atr, signal_ts=signal_ts)
    save(conn, user, cid, s)
    conn.commit()


# ---------------------------------------------------------------- position ---
def stop_alive(p: dict) -> bool:
    stop = p.get("stop_order")
    return bool(stop) and stop.get("state") in ("sent", "working")


def read_stop(conn, adapter, p: dict) -> tuple[str, int, float | None]:
    """The resting stop's status from the broker, ("", 0, None) while unreadable. A dead stop is recorded."""
    stop = p["stop_order"]
    try:
        order = adapter.get_order_status(stop["id"]) or {}
    except Exception as e:
        log.warning("stop order status read failed: %s", type(e).__name__)
        order = {}
    if not order:
        return "", 0, None
    status, filled, avg = ex._order_fields(order)
    if status in ex._DEAD_STATUSES:
        stop["state"] = "dead"
    elif status in ex._FILLED_STATUSES:
        stop["state"] = "filled"
        filled = filled or p["qty"] + p.get("stop_booked", 0)
    else:
        stop["state"] = "working"
    svc.update_order_ledger(stop["key"], status=status, avg_fill_price=avg, conn=conn)
    return status, filled, avg


def reconcile(conn, user: str, cid: str, s: dict, adapter, now: datetime) -> bool:
    """Compare the book with the broker, booking whatever the resting stop has filled. False holds this cycle."""
    p = s["position"]
    actual = adapter.get_position()
    held = None if actual.symbol not in (None, p["symbol"]) else actual.qty
    if held == p["side"] * p["qty"]:
        p.pop("differs_since", None)
        return True
    if p.get("stop_order"):
        _, filled, avg = read_stop(conn, adapter, p)
        fresh = filled - p.get("stop_booked", 0)
        if fresh > 0 and avg:
            p["stop_booked"] = filled
            book_exit(conn, user, cid, s, fresh, avg, "stop", now)
            save(conn, user, cid, s)
            conn.commit()
            if s["position"] is None:
                return False
            p = s["position"]
            if held == p["side"] * p["qty"]:
                return True
    since = p.setdefault("differs_since", stamp(now))
    save(conn, user, cid, s)
    conn.commit()
    if (now - clock(since)).total_seconds() > POSITION_GRACE_S:
        guard = "" if stop_alive(p) else " NO STOP IS RESTING AT THE BROKER."
        block(conn, user, cid, s, f"CRUDEOILM broker position {actual.symbol} {actual.qty} differs from the bot's "
                                  f"{p['symbol']} {p['side'] * p['qty']}; reconcile before any order.{guard}")
    return False


def exit_reason(p: dict, price, now: datetime):
    if p.get("exit_reason"):
        return p["exit_reason"]
    if p.get("stop_failed"):
        return "no_broker_stop"
    if now.date().isoformat() != p["trade_date"]:
        return "eod_missed"
    if now.time() >= cr.EOD_EXIT:
        return "eod"
    if price is None:
        return None
    side = p["side"]
    if side * (price - p["target"]) >= 0:
        return "target"
    if p["dry"]:
        return "stop" if side * (price - p["stop"]) <= 0 else None
    return "stop_missed" if side * (price - p["stop"]) <= -STOP_LIMIT_PTS else None


def kill_stop(conn, cid: str, p: dict, adapter, now: datetime) -> None:
    """Ask the broker to cancel the resting stop. Nothing is sent for the exit until it is seen dead."""
    conn.commit()                          # the transport writes on its own connection
    stop = p["stop_order"]
    last = stop.get("cancelled_at")
    if last and (now - clock(last)).total_seconds() < CANCEL_RETRY_S:
        return
    stop["tries"] += 1
    stop["cancelled_at"] = stamp(now)
    try:
        adapter.cancel_stop(stop["id"], f"{stop['key']}|cancel{stop['tries']}")
    except Exception as e:                 # usually the stop has just filled; the next read shows it
        log.warning("stop cancel failed: %s %s | conn=%s", type(e).__name__, str(e)[:160], cid)


def manage(conn, user: str, cid: str, s: dict, adapter, now: datetime) -> None:
    p = s["position"]
    in_session = now.weekday() < 5 and cr.SESSION_OPEN <= now.time() < cr.SESSION_END
    if not p["dry"]:
        if not reconcile(conn, user, cid, s, adapter, now):
            return
        p = s["position"]
        if not p.get("stop_order") and not p.get("stop_failed") and not p.get("exit_reason"):
            if not in_session:
                return
            queue(conn, user, cid, s, action="STOP", symbol=p["symbol"], qty=p["qty"], side=p["side"], price=p["stop"],
                  dry=False, now=now)
            settle(conn, user, cid, s, adapter, now)
            return
        stop = p.get("stop_order")
        if stop and stop["state"] == "sent" or stop and stop["state"] == "working" and (
                not stop.get("checked") or (now - clock(stop["checked"])).total_seconds() >= STOP_RECHECK_S):
            read_stop(conn, adapter, p)
            stop["checked"] = stamp(now)
            if stop["state"] == "dead" and not p.get("stop_failed") and not p.get("exit_reason"):
                p["stop_failed"] = "broker killed it"
                alert(f"resting stop on {p['symbol']} is dead at the broker; closing the position")
    try:
        price = adapter.get_ltp(p["symbol"])
    except Exception as e:
        log.warning("crude price read failed: %s", type(e).__name__)
        price = None
    if price is not None:
        p["last_price"], p["last_time"] = price, stamp(now)
    reason = exit_reason(p, price, now)
    if not reason or not in_session:
        save(conn, user, cid, s)
        conn.commit()
        return
    p["exit_reason"] = reason
    if not p["dry"] and p.get("stop_order"):
        if stop_alive(p):
            read_stop(conn, adapter, p)
        if stop_alive(p):
            kill_stop(conn, cid, p, adapter, now)
            save(conn, user, cid, s)
            conn.commit()
            return
        if p["stop_order"]["state"] == "filled":        # it filled in the meantime: the next cycle books it
            save(conn, user, cid, s)
            conn.commit()
            return
    queue(conn, user, cid, s, action="EXIT", symbol=p["symbol"], qty=p["qty"], side=p["side"],
          price=adapter.limit(p["symbol"], buy=p["side"] < 0), dry=p["dry"], now=now, reason=reason)
    settle(conn, user, cid, s, adapter, now)


# ------------------------------------------------------------------- entry ---
def prepare_margin(user: str, cid: str, runtime: dict, adapter, symbol: str, qty: int, now: datetime) -> None:
    """A fresh margin quote and the account's funds, read before any signal so an entry does not wait for them."""
    at = runtime.get("margin_at")
    if at is not None and (now - at).total_seconds() <= MARGIN_REFRESH_S and runtime.get("qty") == qty \
            and runtime.get("symbol") == symbol:
        return
    runtime.update(margin_at=now, margin=None, funds=None, qty=qty, symbol=symbol)   # a failed read blocks entries
    runtime.update(margin=adapter.margin(symbol, qty), funds=adapter.available_funds())


def enter(conn, user: str, cid: str, s: dict, adapter, now: datetime, dry: bool, series, live_clock: bool) -> None:
    if now.weekday() >= 5 or not FIRST_MINUTE <= now.time() < LAST_ENTRY or now.second < SETTLE_S:
        return
    if svc.get_config(user, cid, "reconcile_blocked", conn) == "1":
        return
    if not dry and not ex.all_passed([ex.gate_daily_loss_ok(adapter, user, cid, conn),
                                      ex.gate_kill_switch_clear(user, cid, conn)]):
        return
    cutoff = now.replace(second=0, microsecond=0)
    if s.get("last_minute") == cutoff.isoformat():
        return
    runtime = _runtime.setdefault(cid, {})
    if series is None:
        series = runtime.setdefault("series", cr.Series(cr.KiteFeed()))
    series.refresh(now)
    front = series.front(now)
    if not front:
        return
    symbol, qty = front["tradingsymbol"], svc.get_lots(user, cid, conn) * eng.LOT_QTY
    prepare_margin(user, cid, runtime, adapter, symbol, qty, now)
    frame = series.frame(now)
    if not len(frame) or frame["ts"].iloc[-1] < pd.Timestamp(cutoff) - pd.Timedelta(minutes=1):
        if now.second >= ENTRY_WINDOW_S:
            s["last_minute"] = cutoff.isoformat()
            save(conn, user, cid, s)
            conn.commit()
        return
    fires = eng.pending_fires(frame, cr.START, cutoff)
    s["last_minute"] = cutoff.isoformat()
    save(conn, user, cid, s)
    conn.commit()
    if not fires:
        return
    if live_clock:
        now = now_ist()
    late = now.replace(second=0, microsecond=0) != cutoff or now.second > ENTRY_WINDOW_S
    funds, margin = runtime.get("funds"), runtime.get("margin")
    short_of_funds = not dry and (margin is None or (funds is not None and funds < margin))
    fire, rest = fires[0], fires[1:]
    for other in rest:
        decision(conn, cid, other, "position_held", f"behind {fire['cid']}", now)
    if late or short_of_funds or (not dry and adapter.get_position().qty):
        outcome = "late" if late else ("no_funds" if short_of_funds else "account_position")
        decision(conn, cid, fire, outcome, f"funds {funds} margin {margin}" if short_of_funds else None, now)
        conn.commit()
        if short_of_funds:
            alert(f"entry skipped: funds {funds} below margin {margin} for {qty} {symbol}")
        return
    member = eng.MEMBER_BY_CID[fire["cid"]]
    decision(conn, cid, fire, "taken", f"delay {now.second}s", now)
    queue(conn, user, cid, s, action="ENTER", symbol=symbol, qty=qty, side=member.side,
          price=adapter.limit(symbol, buy=member.side > 0), dry=dry, now=now, rule=member.cid,
          atr=fire["atr_stop_dist"], signal_ts=str(fire["signal_ts"]))
    settle(conn, user, cid, s, adapter, now)


def process(user: str, cid: str, *, adapters: dict, now=None, series=None) -> None:
    live_clock = now is None
    now = now or now_ist()
    now = pd.Timestamp(now).tz_localize(None).to_pydatetime()
    mode = ex.get_mode(user, cid)
    if mode == ex.Mode.DISARMED:
        return
    row = svc.get_connection(user, cid)
    if row.get("broker") != "angel":
        return
    cache_key = "crudem:" + cid
    adapter = adapters.get(cache_key)
    creds = svc.load_credentials(user, cid)
    if adapter is None or getattr(adapter, "_creds", None) != creds:
        adapter = CrudemAngelAdapter(user_id=user, conn_id=cid, creds=creds)
        adapter.connect()
        adapters[cache_key] = adapter
    conn = get_live_conn()
    try:
        ensure_schema(conn)
        s = load(conn, user, cid)
        dry = mode == ex.Mode.DRY_RUN
        owned = s.get("pending") or s.get("position")
        if owned and bool(owned["dry"]) != dry:     # persisted mode wins for an owned trade; never simulate a real exit
            return
        if s.get("pending"):
            settle(conn, user, cid, s, adapter, now)
            s = load(conn, user, cid)
            if s.get("pending"):
                return
        if s.get("position"):
            manage(conn, user, cid, s, adapter, now)
            return
        enter(conn, user, cid, s, adapter, now, dry, series, live_clock)
    finally:
        conn.close()
