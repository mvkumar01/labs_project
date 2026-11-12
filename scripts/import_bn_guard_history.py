"""Atomic, idempotent import of verified historical rows; no tracker execution."""
from pathlib import Path
import hashlib,json,sys,sqlite3,time
from collections import Counter

FIRST_FORWARD='2026-11-11'
EXPECTED={'bn_alpha_guard':(103,26579.88),'bn_selective_guard':(29,26826.18)}
CONTROLS=['bn_alpha_daily','bn_alpha_trades','bn_selective_daily','bn_selective_trades']

def snapshots(conn):
    def h(table,where='',params=()):
        rows=list(map(tuple,conn.execute(f'SELECT * FROM {table} {where} ORDER BY 1,2',params)))
        return hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
    out={t:h(t) for t in CONTROLS}
    for key in EXPECTED:
        for suffix in ('daily','trades'):
            table=f'{key}_{suffix}';out[table]=h(table,'WHERE trade_date>=?',(FIRST_FORWARD,))
    return out

def apply(conn,payload):
    if payload['schema']!=1 or payload['first_forward']!=FIRST_FORWARD or set(payload['books'])!=set(EXPECTED):raise ValueError('Wrong backfill payload')
    conn.execute('BEGIN IMMEDIATE')
    try:
        before=snapshots(conn);result={}
        for key,(count,net) in EXPECTED.items():
            book=payload['books'][key];trades=book['trades'];daily=book['daily'];counts=Counter(t['trade_date'] for t in trades)
            if len(trades)!=count or round(sum(t['net_rs'] for t in trades),2)!=net:raise ValueError('Backfill total mismatch')
            if counts!=Counter({d['trade_date']:d['n_trades'] for d in daily}):raise ValueError('Daily/trade count mismatch')
            for suffix,rows in [('daily',daily),('trades',trades)]:
                table=f'{key}_{suffix}';cols=list(rows[0]);schema={r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
                if set(cols)!=schema or any(set(r)!=schema or r['trade_date']>=FIRST_FORWARD for r in rows):raise ValueError('Unsafe backfill scope or columns')
                ordered=sorted(rows,key=lambda r:(r['trade_date'],r.get('seq',0)))
                values=[tuple(r[c] for c in cols) for r in ordered]
                prior=list(map(tuple,conn.execute(f"SELECT {','.join(cols)} FROM {table} WHERE trade_date<? ORDER BY trade_date"+(',seq' if suffix=='trades' else ''),(FIRST_FORWARD,))))
                if prior and prior!=values:raise ValueError('Conflicting pre-existing history in '+table)
                if not prior:conn.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",values)
            persisted=conn.execute(f'SELECT COUNT(*),ROUND(SUM(net_rs),2),MIN(trade_date),MAX(trade_date) FROM {key}_trades WHERE trade_date<?',(FIRST_FORWARD,)).fetchone()
            if tuple(persisted)[:2]!=(count,net):raise ValueError('Persisted totals differ')
            result[key]=dict(trades=count,net_rs=net,first_date=persisted[2],last_date=persisted[3],traded_dates=len(daily))
        if snapshots(conn)!=before:raise ValueError('Forward or control ledger changed')
        conn.commit()
        return dict(status='complete',controls_and_forward_unchanged=True,books=result)
    except Exception:
        conn.rollback();raise

def main():
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
    from config.labs_config import DB_PATH
    job=root/'.codex/backups/bn_guard_history_20261111'
    try:
        raw=(job/'payload.json').read_bytes();payload=json.loads(raw)
        for attempt in range(6):
            # Existing shared journal mode is left alone; allow ordinary writers to finish.
            conn=sqlite3.connect(str(DB_PATH),timeout=30)
            try:
                conn.execute('PRAGMA foreign_keys=ON')
                result=apply(conn,payload);break
            except sqlite3.OperationalError as e:
                if attempt==5 or not any(s in str(e).lower() for s in ('locked','busy')):raise
                print(f'Shared DB busy on attempt {attempt+1}; retrying',flush=True)
                time.sleep(3+attempt*2)
            finally:conn.close()
        result['payload_sha256']=hashlib.sha256(raw).hexdigest()
    except Exception as e:
        result=dict(status='failed',error=f'{type(e).__name__}: {e}')
    path=root/'logs/bn_guard_history_result_v2.json';tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(result),encoding='utf-8');tmp.replace(path)
    print(json.dumps(result),flush=True)
    if result['status']!='complete':raise SystemExit(1)

if __name__=='__main__':main()
