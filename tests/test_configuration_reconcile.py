from types import SimpleNamespace
import pytest

from live.brokers.angel import AngelAdapter
from live.brokers.zerodha import ZerodhaAdapter
from live.configuration_reconcile import confirm_broker_clear


@pytest.mark.parametrize('broker', ['angel', 'zerodha'])
@pytest.mark.parametrize('qty', [0, 1, -1])
@pytest.mark.parametrize('status', ['complete', 'cancelled', 'rejected', 'open', 'trigger pending', 'unknown'])
def test_configuration_checks_all_account_positions_and_working_orders(broker, qty, status):
    # A position outside the adapter's normal instrument must still prevent switching.
    if broker == 'angel':
        adapter = AngelAdapter(user_id='u', conn_id='c', creds={})
        adapter._smart = SimpleNamespace(
            position=lambda: {'status': True, 'data': [{'tradingsymbol': 'GOLD', 'netqty': str(qty)}]},
            orderBook=lambda: {'status': True, 'data': [{'orderstatus': status}]})
    else:
        adapter = ZerodhaAdapter(user_id='u', conn_id='c', creds={})
        adapter._kite = SimpleNamespace(
            positions=lambda: {'net': [{'tradingsymbol': 'GOLD', 'quantity': qty}]},
            orders=lambda: [{'status': status}])
    assert adapter.configuration_is_clear() == (qty == 0 and status in ('complete', 'cancelled', 'rejected'))


def test_angel_failed_order_book_is_not_empty_book():
    adapter = AngelAdapter(user_id='u', conn_id='c', creds={})
    adapter._smart = SimpleNamespace(position=lambda: {'status': True, 'data': []},
                                    orderBook=lambda: {'status': False, 'data': None})
    with pytest.raises(RuntimeError):
        adapter.configuration_is_clear()


def test_confirmation_checks_broker_account_identity():
    adapter = SimpleNamespace(connect=lambda: None, is_connected=lambda: True, account_ref=lambda: 'other',
                              configuration_is_clear=lambda: True)
    with pytest.raises(RuntimeError, match='identity'):
        confirm_broker_clear(adapter, 'expected')


def test_nested_service_helpers_cannot_commit_configuration_early():
    import sqlite3
    from live.configuration_reconcile import TransactionConnection
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE changes(value TEXT)')
    try:
        conn.execute('BEGIN IMMEDIATE')
        scoped = TransactionConnection(conn)
        with scoped:
            scoped.execute("INSERT INTO changes VALUES('first setting')")
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute('SELECT count(*) FROM changes').fetchone()[0] == 0
    finally:
        conn.close()
