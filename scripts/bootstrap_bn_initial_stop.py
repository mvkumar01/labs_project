"""One-time forward table setup; assert both control ledgers are unchanged."""
from pathlib import Path
import sys,json,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from storage.db import get_conn
from labs.engine.bn_initial_stop_tracker import BOOKS,FIRST_FORWARD
from labs.engine import bn_alpha_tracker as core

def control_hash(conn):
    result={}
    for table in ['bn_alpha_daily','bn_alpha_trades','bn_selective_daily','bn_selective_trades']:
        rows=[tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY 1,2')]
        result[table]=hashlib.sha256(json.dumps(rows,default=str,separators=(',',':')).encode()).hexdigest()
    return result

def main():
    conn=get_conn()
    try:
        before=control_hash(conn)
        for key in BOOKS:core._ensure_tables(conn,key)
        assert before==control_hash(conn),'A control ledger changed during table setup'
        counts={key:conn.execute(f'SELECT COUNT(*) FROM {key}_trades').fetchone()[0] for key in BOOKS}
        result=dict(status='complete',forward_start=FIRST_FORWARD,books=counts,controls_unchanged=True)
        path=Path(__file__).resolve().parents[1]/'logs/bn_initial_stop_setup.json'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result),encoding='utf-8')
        print(json.dumps(result),flush=True)
    finally:conn.close()

if __name__=='__main__':main()
