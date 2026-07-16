"""Regression: recent_orders() must not 500 the /live/status poll.

The PnL-annotation subquery originally ordered by ABS(... o.created_at ...) — an
OUTER-scope column referenced inside a subquery's ORDER BY. SQLite cannot
resolve that and raises "no such column: o.created_at", which 500'd the entire
/live/status response so the dashboard's Recent Orders panel (and every polled
field) rendered blank even though the data was present. The fix orders the
subquery by a LOCAL column (t.exit_time). This test seeds an order+trade and
asserts the query runs and attaches PnL to the EXIT row only.
"""
import sqlite3

from storage.live_db import init_live_db
from live import live_service as svc


def _seed_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_live_db(conn)
    return conn


def test_recent_orders_runs_and_attaches_pnl():
    conn = _seed_conn()
    uid, cid = "u1", "u1:angel"
    ts = "2026-07-14T04:34:06.000000+00:00"
    # An ENTER order, a matching EXIT order, and the round-trip trade.
    conn.execute(
        "INSERT INTO live_orders (idem_key,user_id,conn_id,action,side,symbol,"
        "qty,status,dry_run,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("k1", uid, cid, "ENTER", "PUT", "NIFTY24250PE", 65, "complete", 0,
         "2026-07-14T04:32:22.000000+00:00"))
    conn.execute(
        "INSERT INTO live_orders (idem_key,user_id,conn_id,action,side,symbol,"
        "qty,status,dry_run,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("k2", uid, cid, "EXIT", "PUT", "NIFTY24250PE", 65, "complete", 0, ts))
    conn.execute(
        "INSERT INTO live_trades (trade_id,user_id,conn_id,side,symbol,"
        "exit_time,net_pnl,pnl,dry_run) VALUES (?,?,?,?,?,?,?,?,?)",
        ("t1", uid, cid, "PUT", "NIFTY24250PE", ts, 41.71, 41.71, 0))
    conn.commit()

    rows = svc.recent_orders(uid, cid, limit=20, conn=conn)  # must not raise
    by_action = {r["action"]: r for r in rows}
    assert len(rows) == 2
    assert by_action["EXIT"]["net_pnl"] == 41.71    # round-trip PnL attached
    assert by_action["ENTER"]["net_pnl"] is None     # ENTER carries no PnL


def test_recent_orders_empty_for_unknown_conn():
    conn = _seed_conn()
    assert svc.recent_orders("nobody", "nobody:angel", conn=conn) == []
