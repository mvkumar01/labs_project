"""Nifty Omega execution through the existing live runner and order rails."""
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
        quantity, reason, price = 0, '', None
        if minute.time() >= time(15,26):
            quantity, reason = held, 'omega_eod'
        else:
            data = omega.snapshots(day, minute)
            entry = runner._to_ist_minute_naive(state['entry_time'])
            side = 1 if state['side'] == 'CALL' else -1
            targets = [25,40] if held == 2*omega.LOT else [25] if state.get('entry_rule')=='omega_single25' else [40]
            decision = entry+omega.MINUTE
            while decision < minute:
                bar = data['bars'].get(decision-omega.MINUTE)
                if bar is not None:
                    hits, cause = omega.decision_exit(data['alpha'].get(decision), side, bar,
                                                      state['entry_spot'], targets)
                    if hits:
                        # A newer alpha exit or +40 hit takes precedence over a pending +25.
                        quantity = max(quantity, len(hits)*omega.LOT)
                        reason = 'omega_'+cause
                decision += omega.MINUTE
        if not quantity:
            return
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
    if minute.time() > time(15,16) or prior_entry(user_id, conn_id, day, dry_run):
        return
    data = omega.snapshots(day, minute)
    signal = omega.first_signal(data, day, minute-omega.MINUTE)
    if signal is None or signal[0]+omega.MINUTE != minute:
        return
    _, side, strike = signal
    quote = omega.contract_quote(data, minute, strike, side)
    if quote is None:
        return
    bar = data['bars'].get(minute)
    # The live collector can publish the option snapshot before the spot bar.
    anchor = float(bar[0]) if bar else runner._fast_spot()
    if anchor is None or not isfinite(anchor) or anchor <= 0:
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
