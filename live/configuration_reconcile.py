"""Broker confirmation for configuration changes; never submits or cancels orders."""
import json
from live import live_service as svc


class TransactionConnection:
    """Keep service helpers' nested `with conn` blocks inside the caller's transaction."""
    def __init__(self, conn):
        self.conn = conn

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def configuration_revision(conn, user_id, conn_id):
    """Detect a runner/order/configuration write during the broker refresh."""
    state = json.dumps(svc.get_trade_state(user_id, conn_id, conn), sort_keys=True, default=str)
    orders = tuple(tuple(row) for row in conn.execute(
        'SELECT idem_key,status,broker_order_id FROM live_orders WHERE user_id=? AND conn_id=? ORDER BY idem_key',
        (user_id, conn_id)))
    requests = tuple(tuple(row) for row in conn.execute(
        'SELECT request_id,outcome FROM live_order_requests WHERE user_id=? AND conn_id=? ORDER BY request_id',
        (user_id, conn_id)))
    config = tuple(svc.get_config(user_id, conn_id, key, conn) for key in
                   ('mode', 'armed', 'strategy_version', 'decision_engine', 'proposer_sizing_mode'))
    return state, orders, requests, config


def local_exposure_pending(conn, user_id, conn_id):
    state = svc.get_trade_state(user_id, conn_id, conn)
    if str(state.get('position') or '').upper() == 'OPEN':
        return True
    if conn.execute(
        "SELECT 1 FROM live_orders WHERE user_id=? AND conn_id=? AND dry_run=0 "
        "AND lower(status) NOT IN ('complete','completed','filled','cancelled','canceled',"
        "'rejected','failed','no_long_position','exit_qty_exceeds_position','blocked','skipped','gate_blocked') LIMIT 1",
        (user_id, conn_id),
    ).fetchone():
        return True
    return bool(conn.execute(
        "SELECT 1 FROM live_order_requests WHERE user_id=? AND conn_id=? "
        "AND outcome IN ('reserved','uncertain') LIMIT 1", (user_id, conn_id),
    ).fetchone())


def confirm_broker_clear(adapter, expected_account_ref):
    adapter.connect()
    if not adapter.is_connected():
        raise RuntimeError('Broker is disconnected')
    if not expected_account_ref or adapter.account_ref() != expected_account_ref:
        raise RuntimeError('Broker account identity mismatch')
    return adapter.configuration_is_clear()


def clear_stale_state(conn, user_id, conn_id):
    """Clear execution mirrors after broker-flat confirmation, retaining an audit copy."""
    from live.control_plane import audit
    previous = svc.get_trade_state(user_id, conn_id, conn)
    detail = {'trade_state': previous, 'source': 'fresh_broker_position_and_order_book'}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_gold_v2_state'").fetchone():
        row = conn.execute('SELECT payload FROM live_gold_v2_state WHERE user_id=? AND conn_id=?',
                           (user_id, conn_id)).fetchone()
        if row:
            gold = json.loads(row['payload'])
            if gold.get('position') or gold.get('pending'):
                detail['gold_state'] = json.loads(row['payload'])
                gold['position'] = gold['pending'] = None
                conn.execute('UPDATE live_gold_v2_state SET payload=? WHERE user_id=? AND conn_id=?',
                             (json.dumps(gold), user_id, conn_id))
    audit(conn, user_id, 'broker_flat_configuration_refresh', conn_id, detail)
    svc.reset_trade_state(user_id, conn_id, conn)
    svc.set_config(user_id, conn_id, 'reconcile_blocked', '0', conn)
    svc.set_config(user_id, conn_id, 'reconcile_message', '', conn)
