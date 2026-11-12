"""Report aggregate live exposure from a consistent SQLite snapshot."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import sys

root = Path(__file__).resolve().parents[1]
database = root/'storage/live.db'
now = datetime.now(timezone(timedelta(hours=5,minutes=30)))
with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as conn:
    conn.execute('BEGIN')
    mode = conn.execute("SELECT COUNT(*) FROM live_config WHERE key='mode' AND value='LIVE_ARMED'").fetchone()[0]
    all_open = conn.execute("SELECT COUNT(*) FROM live_trade_state WHERE position='OPEN' AND virtual=0").fetchone()[0]
    scope = " LEFT JOIN live_config v ON v.user_id=t.user_id AND v.conn_id=t.conn_id AND v.key='strategy_version' "
    owner = " AND COALESCE(v.value,'') NOT LIKE 'proposer%' "
    opened = conn.execute("SELECT COUNT(*) FROM live_trade_state t"+scope+
                          "WHERE t.position='OPEN' AND t.virtual=0"+owner).fetchone()[0]
    managed = conn.execute("SELECT COUNT(*) FROM live_trade_state t"+scope+
                           "JOIN live_config m ON m.user_id=t.user_id AND m.conn_id=t.conn_id AND m.key='mode' "
                           "WHERE t.position='OPEN' AND t.virtual=0 AND m.value='LIVE_ARMED'"+owner).fetchone()[0]
    orders = conn.execute("SELECT COUNT(*) FROM live_orders t"+scope+"WHERE t.dry_run=0 AND t.trade_date=? AND "
                          "(upper(t.status) IN ('PENDING','PLACED','OPEN','TRIGGER PENDING','UNKNOWN') OR t.status IS NULL)"+owner,
                          (now.date().isoformat(),)).fetchone()[0]
result = dict(asof=now.isoformat(), live_armed=mode, all_live_open=all_open, live_open=opened,
              managed_live_open=managed, pending_live_orders=orders)
sys.path.insert(0,str(root))
from live import live_executor
from live.engine import nifty_omega
from live.nifty_omega_runner import affordable_lots
from labs.engine.nifty_omega_book import BOOK
from labs.ui.live_routes import offered_presets
assert live_executor.expected_decision_abi(nifty_omega.KEY)==nifty_omega.ABI
assert BOOK.lots==2 and nifty_omega.KEY in offered_presets(None)
assert [affordable_lots(funds,100,1.03) for funds in (5000,9000,20000)]==[0,1,2]
result['imports_and_live_preset'] = True
print(json.dumps(result),flush=True)
