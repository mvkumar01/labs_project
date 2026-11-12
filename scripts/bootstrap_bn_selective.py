"""Initialize only Bank Nifty Selective's ledger; never start any trading loop."""
from pathlib import Path
import sys,json,hashlib,sqlite3
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from labs.engine import bn_selective_tracker as book
from config.labs_config import DB_PATH

def original_hash():
    c=sqlite3.connect(str(DB_PATH))
    try:
        values={t:c.execute('SELECT * FROM '+t+' ORDER BY trade_date'+(',seq' if t.endswith('_trades') else '')).fetchall()
                for t in ['bn_alpha_daily','bn_alpha_trades']}
        return hashlib.sha256(json.dumps(values,default=str,sort_keys=True).encode()).hexdigest()
    finally:c.close()

before=original_hash()
result=book.initialize_history()
c=sqlite3.connect(str(DB_PATH))
try:
    result['summary']=dict(zip(['trades','net_rs'],c.execute("SELECT count(*),round(sum(net_rs),2) FROM bn_selective_trades WHERE status='closed'").fetchone()))
    result['max_trades_per_day']=c.execute('SELECT max(n_trades) FROM bn_selective_daily').fetchone()[0]
finally:c.close()
result['original_unchanged']=original_hash()==before
assert result['original_unchanged']
assert (result['max_trades_per_day'] or 0)<=1
result['status']='complete'
out=ROOT/'logs/bn_selective_setup_result.json';out.parent.mkdir(parents=True,exist_ok=True)
out.write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')
print(json.dumps(result,default=str),flush=True)
