"""Nifty Omega execution through the existing live runner and order rails.

After the first entry of the day, a new entry needs the arm in config 'omega_reentry', written by the exit that
flattened the previous trade (omega.after_exit): kind 'reclaim' (PUT loss exit, the day's one re-entry) or 'fresh'
(the day's first snap exit, one fresh signal on either side). 'taken' carries the kinds already used today."""
from datetime import datetime, time
import json
from math import isfinite

from live import live_executor as ex, live_service as svc
from live.engine import nifty_omega as omega


def affordable_lots(funds, price, buffer):
    if funds is None:
        return 2
    return sum(funds >= price*omega.LOT*count*buffer for count in (1,2))


def prior_entry(user_id, conn_id, day, dry_run):
    conn = svc.get_live_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM live_orders WHERE user_id=? AND conn_id=? AND trade_date=? "
            "AND strategy_version=? AND action='ENTER' AND dry_run=? LIMIT 1",
            (user_id, conn_id, day, omega.KEY, int(dry_run))).fetchone()
        return row is not None
    finally:
        conn.close()


def entry_contract(user_id, conn_id, day):
    """Strike and entry ask of today's Omega position (written just before the entry order), or None."""
    raw = svc.get_config(user_id, conn_id, 'omega_entry')
    record = json.loads(raw) if raw else None
    if not record or record.get('day') != day:
        return None
    return dict(strike=int(record['strike']), ask=float(record['ask']))


def day_arm(user_id, conn_id, day, dry_run):
    """Today's re-entry arm for this mode, or None. A record without 'kind' is a PUT reclaim (before the snap exit)."""
    raw = svc.get_config(user_id, conn_id, 'omega_reentry')
    arm = json.loads(raw) if raw else None
    if not arm or arm.get('day') != day or bool(arm.get('dry_run')) != dry_run:
        return None
    return dict(arm, kind=arm.get('kind') or 'reclaim')


def taken_today(arm):
    """Entry kinds ('reclaim', 'fresh') already used today."""
    if not arm:
        return set()
    return set(arm.get('taken') or ()) | ({arm['kind']} if arm.get('used') else set())


def execute(user_id, conn_id, adapter, now, dry_run):
    from live import live_runner as runner

    day = now.date().isoformat()
    if day < omega.START:
        return
    minute = now.replace(tzinfo=None, second=0, microsecond=0)
    state = svc.get_trade_state(user_id, conn_id)
    opened = state.get('position') == 'OPEN'
    position = None if dry_run else adapter.get_position()
    if opened and not dry_run and bool(state.get('virtual')) and not position.qty:
        svc.reset_trade_state(user_id, conn_id)
        state = svc.get_trade_state(user_id, conn_id)
        opened = False
    if opened and bool(state.get('virtual')) != dry_run:
        return
    if opened and runner._ist_date_of(state.get('entry_time')) != day:
        return

    if not dry_run:
        pending = svc.get_config(user_id, conn_id, 'omega_pending_entry')
        if pending:
            if opened:
                svc.set_config(user_id, conn_id, 'omega_pending_entry', '')
            else:
                record = json.loads(pending)
                if record['day'] != day:
                    return
                broker_order = adapter.get_order_status(record['order_id']) or {}
                status, filled, price = ex._order_fields(broker_order)
                if not ex._is_final_status(status):
                    return
                if filled and price and filled in (omega.LOT, 2*omega.LOT):
                    state.update(record['state'], qty=filled, entry_price=price,
                                 entry_rule='omega_single25' if filled==omega.LOT else 'omega_ema9_21')
                    svc.save_trade_state(user_id, conn_id, state)
                elif filled or position.qty:
                    return
                svc.set_config(user_id, conn_id, 'omega_pending_entry', '')
                return

    def book_exit(quantity, price, reason):
        runner._record_exit_result(user_id, conn_id, state, exit_price=price,
                                   qty=quantity, reason=reason, dry_run=dry_run)
        remaining = int(state['qty'])-quantity
        if remaining:
            state['qty'] = remaining
            svc.save_trade_state(user_id, conn_id, state)
        else:
            svc.reset_trade_state(user_id, conn_id)
        return remaining

    if opened:
        held = int(state.get('qty') or 0)
        if held not in (omega.LOT, 2*omega.LOT):
            return
        symbol = state['symbol']
        if not dry_run:
            working = ex.resolve_working_exit(adapter, user_id=user_id, conn_id=conn_id,
                                              symbol=symbol, since=state['entry_time'])
            if working:
                if working['state'] == 'working':
                    return
                if working['filled_qty']:
                    if not working['avg'] or not 0 < working['filled_qty'] <= held:
                        return
                    book_exit(working['filled_qty'], working['avg'], 'omega_exit_reconciled')
                    return
            if position.symbol != symbol or int(position.qty) != held:
                # A mismatch can be an unrecorded broker fill. Avoid a new order.
                runner.log.warning('Omega position needs reconciliation conn=%s', conn_id)
                return
        quantity, reason, price, closing = 0, '', None, None
        side = 1 if state['side'] == 'CALL' else -1
        if minute.time() >= time(15,26):
            quantity, reason = held, 'omega_eod'
        else:
            data = omega.snapshots(day, minute)
            entry = runner._to_ist_minute_naive(state['entry_time'])
            first, second = omega.PREMIUM_TARGETS
            # 'omega_single25' = one lot bought (funds); it carries lot 1's target.
            targets = [first, second] if held == 2*omega.LOT else [first] if state.get('entry_rule')=='omega_single25' else [second]
            contract = entry_contract(user_id, conn_id, day)
            if contract is None:
                runner.log.warning('Omega entry contract unknown conn=%s: alpha and EOD exits only', conn_id)
            decision = entry+omega.MINUTE
            signal_mark = entry-omega.MINUTE
            while decision < minute:
                quote = omega.contract_quote(data, decision, contract['strike'], side) if contract else None
                hits, cause = omega.decide(data, signal_mark, decision, side,
                                           float(quote.bid) if quote is not None else None,
                                           contract['ask'] if contract else None, targets,
                                           snap=day >= omega.SNAP_START)
                if hits:
                    quantity = max(quantity, len(hits)*omega.LOT)
                    reason = 'omega_'+cause
                    if len(hits) == len(targets):
                        # The first minute that closes every held lot decides, as in the research.
                        closing = cause, decision
                        break
                decision += omega.MINUTE
        if not quantity:
            return
        if quantity == held:
            # Arm (PUT loss exit: reclaim; first snap exit: one fresh entry) or clear, with the exit that flattens.
            taken = taken_today(day_arm(user_id, conn_id, day, dry_run))
            allowed = omega.after_exit(closing[0], side, closing[1], taken) if closing else None
            arm = dict(day=day, side=side, kind=allowed[0], mark=allowed[1].isoformat(), used=False,
                       taken=sorted(taken), dry_run=int(dry_run)) if allowed else None
            svc.set_config(user_id, conn_id, 'omega_reentry', json.dumps(arm) if arm else '')
        price = runner._fast_ltp(adapter, symbol)
        result = runner._route_order(adapter, user_id, conn_id, action='EXIT', side=state['side'],
                                     symbol=symbol, qty=quantity, price=price, dry_run=dry_run,
                                     entry_rule=reason)
        filled = quantity if dry_run and runner._freshly_applied(result, dry_run=True) else ex.filled_qty(result)
        if not filled and runner._freshly_applied(result, dry_run=dry_run):
            filled = quantity
        if (dry_run or ex._is_final_status(result.status)) and filled and 0 < filled <= quantity and not (result.raw or {}).get('idempotent_skip'):
            if dry_run or result.avg_fill_price:
                book_exit(filled, result.avg_fill_price or price, reason)
        return

    if minute.time() >= time(15,26):
        if not dry_run and not position.qty:
            ex.disarm(user_id, conn_id)
        return
    if (not dry_run and position.qty) or svc.get_config_int(user_id, conn_id, 'reconcile_blocked'):
        return
    if svc.get_lots(user_id, conn_id) != 2 or not runner.check_daily_loss(user_id, conn_id, dry_run=dry_run):
        return
    if minute.time() > time(15,16):
        return
    arm = None
    if prior_entry(user_id, conn_id, day, dry_run):
        # Only an entry armed by the exit that flattened the previous trade (see the module notes).
        arm = day_arm(user_id, conn_id, day, dry_run)
        if not arm or arm.get('used'):
            return
        data = omega.snapshots(day, minute)
        if arm['kind'] == 'fresh':
            signal = omega.fresh_signal(data, day, datetime.fromisoformat(arm['mark']), minute-omega.MINUTE)
        else:
            signal = omega.reentry_signal(data, day, int(arm['side']), datetime.fromisoformat(arm['mark']),
                                          minute-omega.MINUTE)
    else:
        data = omega.snapshots(day, minute)
        signal = omega.first_signal(data, day, minute-omega.MINUTE)
    if signal is None or signal[0]+omega.MINUTE != minute:
        return
    _, side, atm = signal
    bar = data['bars'].get(minute)
    # The live collector can publish the option snapshot before the spot bar.
    anchor = float(bar[0]) if bar else runner._fast_spot()
    if anchor is None or not isfinite(anchor) or anchor <= 0:
        return
    strike = omega.pick_strike(data, day, minute, atm, side, anchor)
    quote = omega.contract_quote(data, minute, strike, side)
    if quote is None:
        return
    label = 'CALL' if side == 1 else 'PUT'
    price = float(quote.ask)
    funds = None
    if not dry_run:
        try:
            funds = adapter.available_funds()
        except Exception:
            runner.log.warning('Omega funds lookup unavailable conn=%s', conn_id)
    lots = affordable_lots(funds, price, runner.AFFORDABILITY_BUFFER)
    if not lots:
        return

    def buy(count, attempt=0):
        return runner._route_order(adapter, user_id, conn_id, action='ENTER', side=label,
                                   symbol=quote.tradingsymbol, qty=count*omega.LOT, price=price,
                                   dry_run=dry_run, entry_rule='omega_single25' if count==1 else 'omega_ema9_21',
                                   attempt=attempt)

    if arm is not None:
        svc.set_config(user_id, conn_id, 'omega_reentry', json.dumps(dict(arm, used=True)))
    # The premium targets are measured from this quote's ask, as in the paper book.
    svc.set_config(user_id, conn_id, 'omega_entry', json.dumps(dict(day=day, strike=int(strike), ask=price)))
    result = buy(lots)
    if not dry_run and not ex.filled_qty(result) and ex.stepped_down_lots(lots, result) == 1:
        lots = 1
        result = buy(lots, attempt=1)
    if (result.raw or {}).get('idempotent_skip'):
        return
    filled = ex.filled_qty(result)
    if not filled and runner._freshly_applied(result, dry_run=dry_run):
        filled = lots*omega.LOT
    entry_state = dict(position='OPEN', side=label, symbol=(result.raw or {}).get('broker_symbol') or quote.tradingsymbol,
                       entry_spot=anchor, entry_time=runner._now_iso(), virtual=int(dry_run), entry_rule='omega_ema9_21')
    if filled in (omega.LOT, 2*omega.LOT) and (dry_run or (ex._is_final_status(result.status) and result.avg_fill_price)):
        state.update(entry_state, entry_price=result.avg_fill_price or price, qty=filled)
        if filled == omega.LOT:
            state['entry_rule'] = 'omega_single25'
        svc.save_trade_state(user_id, conn_id, state)
    elif result.broker_order_id and (not ex._is_final_status(result.status) or filled):
        svc.set_config(user_id, conn_id, 'omega_pending_entry', json.dumps(
            dict(day=day, order_id=result.broker_order_id, state=entry_state)))
