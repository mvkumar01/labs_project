"""Read-only process/import diagnostics; never executes any strategy runner."""
from pathlib import Path
import ast,importlib,json,signal,subprocess,sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
OUT=ROOT/'logs/bn_selective_startup_probe.json'
tree=ast.parse((ROOT/'pa_paper_tracker_loop.py').read_text(encoding='utf-8'))
main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
mods=list(dict.fromkeys(n.module for n in main.body if isinstance(n,ast.ImportFrom)))
ps=subprocess.check_output(['ps','-u','mvkumar01','-o','pid,etime,args'],text=True)
data=dict(processes=[x for x in ps.splitlines() if 'pa_paper_tracker_loop.py' in x],imports=[],status='running')
def save():OUT.write_text(json.dumps(data,indent=2),encoding='utf-8')
def alarm(*args):raise TimeoutError('Import exceeded 30 seconds')
signal.signal(signal.SIGALRM,alarm)
save()
for mod in mods:
    data['current_import']=mod;save();start=time.monotonic()
    try:
        signal.alarm(30);importlib.import_module(mod);signal.alarm(0)
        data['imports'].append(dict(module=mod,status='ok',seconds=round(time.monotonic()-start,2)))
    except Exception as exc:
        signal.alarm(0);data['imports'].append(dict(module=mod,status='failed',error_type=type(exc).__name__,error=str(exc)[:250]))
        data['status']='failed';save();raise
data['status']='complete';save()
