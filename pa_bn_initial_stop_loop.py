"""Independent minute-risk PAPER loop. No alerts, broker routes or order calls."""
from pathlib import Path
from datetime import datetime
import sys,time,json,traceback
sys.path.insert(0,str(Path(__file__).resolve().parent))
from labs.engine.bn_initial_stop_tracker import run_live,BOOKS,FIRST_FORWARD
from labs.engine import bn_alpha_tracker as core
from storage.db import get_conn
_worker=None

def start_background():
    """One independent worker in the existing paper process; never duplicate it."""
    global _worker
    from threading import Thread
    if _worker is not None and _worker.is_alive():return _worker
    _worker=Thread(target=_supervise,name='bn-initial-stop-paper',daemon=True)
    _worker.start()
    return _worker

def _supervise():
    while True:
        try:main()
        except Exception:traceback.print_exc()
        time.sleep(5)

def fingerprint(now):
    day=now.date().isoformat()
    paths=[core.DATA_DIR/f'{day}_BANKNIFTY_spot_1min.csv',core.SHARED_LIVE_DIR/day/'BANKNIFTY_options_1min.csv']
    return (now.strftime('%Y-%m-%dT%H:%M'),*(p.stat().st_mtime_ns if p.exists() else None for p in paths))

def main():
    from scripts.bootstrap_bn_initial_stop import main as initialize
    setup=Path(__file__).resolve().parent/'logs/bn_initial_stop_setup.json'
    if not setup.exists():initialize()
    conn=get_conn()
    try:
        for key in BOOKS:core._ensure_tables(conn,key)
    finally:conn.close()
    log=Path(__file__).resolve().parent/'logs/bn_initial_stop_heartbeat.json';log.parent.mkdir(parents=True,exist_ok=True)
    print('[bn-initial-stop] started; one-minute risk; forward '+FIRST_FORWARD,flush=True)
    last=None
    while True:
        started=time.monotonic();now=datetime.now(core.IST)
        stamp=fingerprint(now)
        if stamp!=last:
            try:
                result=run_live(now)
                data=dict(timestamp=now.isoformat(),status='idle' if result is None else result.get('status'),result=result,books=list(BOOKS))
                temp=log.with_suffix('.tmp');temp.write_text(json.dumps(data),encoding='utf-8');temp.replace(log)
                print('[bn-initial-stop]',json.dumps(data),flush=True)
                last=stamp
            except Exception:
                traceback.print_exc()
        time.sleep(max(1,5-(time.monotonic()-started)))

if __name__=='__main__':main()
