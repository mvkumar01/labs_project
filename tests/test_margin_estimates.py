from datetime import datetime
import json
import sqlite3
import pytest
from labs.services import margin_estimates as m
from labs.services.strategy_report import normalize,SOURCES


@pytest.fixture
def cache(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'CACHE_PATH',tmp_path/'margins.json')
    monkeypatch.setattr(m,'_cached',(None,{}))
    return m.CACHE_PATH


class MarginBroker:
    def __init__(self):self.calls=[]
    def instruments(self,exchange):
        assert exchange=='MCX'
        return [dict(name=name,instrument_type='FUT',expiry='2026-12-01',
                     tradingsymbol=name+'26DECFUT',lot_size=1) for name in m.UNITS]
    def quote(self,symbols):return {s:{'last_price':10000} for s in symbols}
    def basket_order_margins(self,orders,consider_positions):
        assert consider_positions is False and len(orders)==1
        assert orders[0]['quantity']==1 and orders[0]['product']=='NRML'
        self.calls.append(orders[0])
        return {'final':{'total':20000 if orders[0]['transaction_type']=='BUY' else 30000}}


def test_refresh_quotes_is_standalone_and_cached_without_execution(cache):
    broker=MarginBroker()
    now=datetime.fromisoformat('2026-10-27T09:00:00+05:30')
    data=m.refresh_quotes(broker,now)
    assert len(broker.calls)==6 and set(data['quotes'])==set(m.UNITS)
    assert not data['unavailable']
    assert m.refresh_quotes(broker,now)==data and len(broker.calls)==6


def test_estimate_scales_actual_physical_units_and_preserves_recorded_margin(cache):
    m.refresh_quotes(MarginBroker(),datetime.fromisoformat('2026-10-27T09:00:00+05:30'))
    result=m.estimate_margin('GOLD',12000,100,'short')
    assert result['value']==36000
    row=dict(trade_date='2026-10-22',seq=1,entry_ts='2026-10-22T10:00:00',
             exit_ts='2026-10-22T11:00:00',direction='short',entry_price=12000,
             exit_price=11900,qty=100,gross_rs=10000,net_rs=9900,charges_rs=100)
    estimated=normalize(row,SOURCES['gold_cci'],'gold_cci')
    assert estimated['capital']==36000 and estimated['capital_estimated']
    assert estimated['pnl_pct']==pytest.approx(27.5)
    recorded=normalize(dict(row,margin_rs=25000),SOURCES['gold_cci'],'gold_cci')
    assert recorded['capital']==25000 and not recorded['capital_estimated']


def test_no_quote_is_not_invented_margin(cache):
    assert m.estimate_margin('GOLD',10000,100,'short') is None
    row=dict(trade_date='2026-10-22',seq=1,entry_price=10000,qty=10,
             margin_rs=100000,margin_source='notional_proxy')
    t=normalize(row,SOURCES['crudeoilm_ma20'],'crudeoilm_ma20')
    assert t['capital'] is None


def test_mcx_tile_uses_estimated_peak_not_sum_of_sequential_trades(cache):
    from labs.services.book_overview_more import _mcx
    m.refresh_quotes(MarginBroker(),datetime.fromisoformat('2026-10-27T09:00:00+05:30'))
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE crudem_combo_daily (trade_date TEXT,net_rs REAL,n_trades INTEGER,open_trades INTEGER)')
    c.execute("INSERT INTO crudem_combo_daily VALUES ('2026-10-22',100,2,0)")
    c.execute('CREATE TABLE crudem_combo_trades (trade_date TEXT,seq INTEGER,entry_price REAL,exit_price REAL,qty REAL,entry_ts TEXT,exit_ts TEXT)')
    c.executemany('INSERT INTO crudem_combo_trades VALUES (?,?,?,?,?,?,?)',[
        ('2026-10-22',1,10000,10100,10,'2026-10-22T09:00:00','2026-10-22T10:00:00'),
        ('2026-10-22',2,20000,20100,10,'2026-10-22T10:00:00','2026-10-22T11:00:00')])
    tile=_mcx(c,'crudem_combo','2026-10-22')
    assert tile['capital']==40000 and tile['capital_estimated']
    assert '2026-10-27' in tile['capital_quote_asof']


def test_dry_run_tile_uses_its_own_instrument_margin(cache,tmp_path,monkeypatch):
    from storage import live_db
    from labs.services.book_overview_more import _dry_runner
    m.refresh_quotes(MarginBroker(),datetime.fromisoformat('2026-10-27T09:00:00+05:30'))
    path=tmp_path/'dry.db';c=sqlite3.connect(path)
    c.execute('CREATE TABLE live_gold_trades (trade_date TEXT,book TEXT,entry_ts TEXT,exit_ts TEXT,entry_price REAL,exit_price REAL,qty REAL,net_rs REAL)')
    c.execute("INSERT INTO live_gold_trades VALUES ('2026-10-22','dry','2026-10-22T09:00:00','2026-10-22T10:00:00',10000,9900,10,900)")
    c.execute('CREATE TABLE live_gold_position (id INTEGER)');c.commit();c.close()
    monkeypatch.setattr(live_db,'get_live_conn',lambda:sqlite3.connect(path))
    tile=_dry_runner('dry_gold','Gold dry','GOLDM','live_gold','gold_cci','2026-10-22')
    assert tile['capital']==30000 and tile['capital_estimated']


def test_quote_refresh_endpoint_does_not_expose_broker_errors(monkeypatch):
    from flask import Flask
    from labs.ui.routes import labs_bp
    app=Flask('margin-refresh');app.register_blueprint(labs_bp)
    monkeypatch.setattr(m,'refresh_quotes',lambda:dict(asof='2026-10-27',quotes={'GOLD':{}},unavailable=[]))
    response=app.test_client().post('/labs/api/strategy-report/margins/refresh')
    assert response.status_code==200 and response.json['products']==['GOLD']
    def unavailable():raise RuntimeError('private broker failure details')
    monkeypatch.setattr(m,'refresh_quotes',unavailable)
    response=app.test_client().post('/labs/api/strategy-report/margins/refresh')
    assert response.status_code==503 and 'private broker' not in response.get_data(as_text=True)
