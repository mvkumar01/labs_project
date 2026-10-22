"""Date scope, accounting grain, null handling and excursion boundary regressions."""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import jinja2
import pytest

from labs.services.strategy_report import (SOURCES, Source, build_reports, make_report,
                                            normalize, summarize, IST)
from labs.services.strategy_excursions import estimate, day_peak, contract_marks


def trade(day='2026-10-22', seq=1, **changes):
    return dict(trade_date=day,seq=seq,status='closed',tradingsymbol='GOLD26DECFUT',
                entry_ts=day+'T10:00:00',exit_ts=day+'T11:00:00',entry_price=100,
                exit_price=90,qty=10,direction='short',gross_rs=100,charges_rs=5,
                slippage_rs=2,net_rs=93,**changes)


def test_net_gross_costs_and_capital_are_distinct():
    t=normalize(trade(),SOURCES['gold_cci'],'gold_cci')
    assert t['gross']==100 and t['costs']==7 and t['net']==93
    assert t['capital'] is None and t['pnl_pct'] is None  # futures notional is not margin
    assert t['exit_value']==900 and t['duration']=='1h 0m 0s'


def test_put_is_long_option_not_short_premium():
    r=trade();r.update(side='PUT',direction=None,entry_price=100,exit_price=120,gross_rs=1300,
                       qty=None,entry_spot=23000,exit_spot=22980,net_rs=1200)
    t=normalize(r,SOURCES['alpha_v212'],'alpha_v212')
    assert t['qty']==65 and t['capital']==6500 and t['spot_points']==20
    assert t['pnl_pct']==pytest.approx(1200/6500*100)


def test_multi_leg_trade_counts_once_and_uses_allocated_capital():
    raw=[]
    for i in range(4):
        r=trade(seq=i);r.update(leg=str(i),allocated_capital_rs=1000,position_side='SHORT' if i<2 else 'LONG',
                               direction=None)
        raw.append(r)
    p=make_report(raw,SOURCES['theta_iron_fly'],'theta_iron_fly','Fly')
    assert p['stats']['closed']==1
    assert p['stats']['net']==372
    assert p['trades'][0]['capital']==4000
    assert p['trades'][0]['multi_leg']


def test_win_denominators_include_break_even_and_exclude_open_and_unpriced():
    values=[]
    for day,net,status in [('2026-10-01',10,'closed'),('2026-10-01',-10,'closed'),
                           ('2026-10-02',20,'closed'),('2026-10-03',0,'closed'),
                           ('2026-10-04',999,'open'),('2026-10-05',None,'closed')]:
        r=trade(day);r.update(net_rs=net,status=status,charges_rs=None if net is None else 5,
                             gross_rs=None if net is None else 100)
        t=normalize(r,SOURCES['gold_cci'],'gold_cci');values.append(t)
    s=summarize(values)
    assert s['closed']==5 and s['priced']==4 and s['unpriced']==1
    assert s['net']==20 and s['open_net']==999
    assert s['trade_win_pct']==50 and s['days']==3
    assert s['day_win_pct']==pytest.approx(100/3) and s['breakeven_days']==2


def table(conn,name):
    conn.execute(f'CREATE TABLE {name} (trade_date TEXT,seq INTEGER,status TEXT,tradingsymbol TEXT, entry_ts TEXT,exit_ts TEXT,entry_price REAL,exit_price REAL,qty REAL,direction TEXT,gross_rs REAL,charges_rs REAL,slippage_rs REAL,net_rs REAL)')


def insert(conn,name,r):
    conn.execute(f'INSERT INTO {name} ('+','.join(r)+') VALUES ('+','.join('?' for _ in r)+')',tuple(r.values()))


def test_date_filter_and_monthly_totals_have_no_60_or_100_row_limit():
    c=sqlite3.connect(':memory:');table(c,'gold_cci_trades')
    insert(c,'gold_cci_trades',trade('2026-09-30'))
    for seq in range(125):insert(c,'gold_cci_trades',trade(seq=seq))
    p=build_reports(c,'gold_cci','2026-10-01','2026-10-22',live_conn=sqlite3.connect(':memory:'))[0]
    assert p['stats']['closed']==125 and p['stats']['net']==125*93
    assert len(p['trades'])==100 and p['pages']==2
    assert len(p['months'])==1 and p['months'][0]['net']==125*93
    second=build_reports(c,'gold_cci','2026-10-01','2026-10-22',page=2,live_conn=sqlite3.connect(':memory:'))[0]
    assert len(second['trades'])==25 and second['stats']['net']==p['stats']['net']


def test_dry_run_is_filtered_and_never_combined_with_replay():
    c=sqlite3.connect(':memory:');table(c,'gold_cci_trades')
    l=sqlite3.connect(':memory:');table(l,'live_gold_trades')
    l.execute('ALTER TABLE live_gold_trades ADD COLUMN book TEXT')
    insert(c,'gold_cci_trades',trade())
    for day,book in [('2026-10-01','dry'),('2026-10-22','dry'),('2026-10-22','live')]:
        r=trade(day);r['book']=book;insert(l,'live_gold_trades',r)
    panels=build_reports(c,'gold_cci','2026-10-22','2026-10-22',live_conn=l)
    assert len(panels)==2 and all(p['stats']['net']==93 for p in panels)
    assert panels[1]['stats']['closed']==1
    assert 'dry run' in panels[1]['title']


def test_empty_selected_range_is_zero_not_lifetime_fallback():
    c=sqlite3.connect(':memory:');table(c,'gold_cci_trades');insert(c,'gold_cci_trades',trade())
    p=build_reports(c,'gold_cci','2027-01-01','2027-01-02',live_conn=sqlite3.connect(':memory:'))[0]
    assert p['stats']['net']==0 and p['count']==0 and not p['months']


def test_missing_prices_and_costs_are_not_fabricated():
    r=trade();r.update(net_rs=None,charges_rs=None,slippage_rs=None,gross_rs=100)
    t=normalize(r,SOURCES['gold_cci'],'gold_cci')
    assert t['costs'] is None and t['net'] is None
    assert summarize([t])['net'] is None


def test_all_strategy_tabs_have_an_adapter():
    from labs.ui.routes import LIVE_TABS
    assert set(LIVE_TABS)-{'overview','archive'} <= set(SOURCES)


def test_daily_portfolio_does_not_invent_trade_wins():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE risk_core_days (trade_date TEXT,net_rs REAL)')
    c.executemany('INSERT INTO risk_core_days VALUES (?,?)',[('2026-09-30',999),('2026-10-01',20),('2026-10-02',-5)])
    p=build_reports(c,'risk_core','2026-10-01','2026-10-02')[0]
    assert p['stats']['net']==15 and p['stats']['day_win_pct']==50
    assert p['stats']['trade_win_pct'] is None and p['stats']['closed'] is None
    assert p['stats']['gross'] is None


def test_crypto_filter_keeps_actual_opening_equity_and_usd():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE crypto_xs_daily (decision_date TEXT,complete INTEGER,gross REAL,funding REAL,cost REAL,net REAL,equity REAL)')
    c.executemany('INSERT INTO crypto_xs_daily VALUES (?,1,?,0,?,?,?)',[
        ('2026-09-30',.1,0,.1,110000),('2026-10-01',.02,.01,.01,111100)])
    p=build_reports(c,'crypto_xs','2026-10-01','2026-10-01')[0]
    assert p['currency']=='USD' and p['stats']['net']==pytest.approx(1100)
    assert p['stats']['costs']==pytest.approx(1100)


def test_template_renders_rupee_and_negative_no_escaped_entity():
    env=jinja2.Environment(loader=jinja2.FileSystemLoader('templates'),autoescape=True)
    p=make_report([trade()],SOURCES['gold_cci'],'gold_cci','Gold')
    env.globals['url_for'] = lambda *args, **kwargs: '/labs/live'
    html=env.get_template('_strategy_report.html').render(report_panels=[p],date_from='2026-10-01',date_to='2026-10-22')
    assert '₹93.00' in html and '&amp;#8377;' not in html
    for label in ('Entry capital','Duration','Direction','Peak P&amp;L that day','Post-exit trough','Day win rate'):
        assert label in html


def marks(day='2026-10-22'):
    return [(datetime.fromisoformat(day+'T'+t).replace(tzinfo=IST),lo,hi,cl) for t,lo,hi,cl in [
        ('10:01:00',95,105,100),('10:30:00',80,110,90),('11:02:00',70,120,80)]]


def test_short_peak_and_post_exit_use_original_entry_baseline():
    t=normalize(trade(),SOURCES['gold_cci'],'gold_cci')
    v=estimate(t,marks())
    assert v['peak']==200 and v['post_peak']==300 and v['post_trough']==-200


def test_missing_mark_series_is_unknown_not_zero():
    t=normalize(trade(),SOURCES['gold_cci'],'gold_cci')
    assert estimate(t,[])['peak'] is None
    assert day_peak([t],{}) is None


def test_partial_entry_and_exit_bars_are_not_used():
    t=normalize(trade(),SOURCES['gold_cci'],'gold_cci')
    t['entry_ts']='2026-10-22T10:00:30+05:30'
    t['exit_ts']='2026-10-22T10:30:30+05:30'
    m=marks();m[0]=(m[0][0],1,1000,100)
    v=estimate(t,m)
    assert v['peak']==200  # excludes the 10:00-10:01 partial entry bar


def test_day_peak_does_not_sum_non_simultaneous_trade_peaks():
    a=normalize(trade(),SOURCES['gold_cci'],'gold_cci')
    b=dict(a,id='second',direction='LONG')
    series={a['id']:marks(),b['id']:marks()}
    assert day_peak([a,b],series)==200  # known exits 100 + 100; held marks offset


def test_multi_leg_cannot_use_single_leg_price_proxy():
    t=normalize(trade(),SOURCES['gold_cci'],'gold_cci');t['multi_leg']=True
    assert contract_marks(sqlite3.connect(':memory:'),t)[0]==[]


def test_btc_times_display_ist_but_date_filter_stays_utc_cohort():
    r=trade();r.update(entry_ts='2026-10-22T20:00:00',exit_ts='2026-10-22T21:00:00')
    t=normalize(r,SOURCES['btc_rsi_roc'],'btc_rsi_roc')
    assert t['entry_ts'].startswith('2026-10-23T01:30') and t['date']=='2026-10-22'

@pytest.mark.parametrize('module_name', ['gold_cci_tracker','crudem_combo_tracker'])
def test_legacy_dry_tables_apply_the_same_filter_without_60_trade_truncation(tmp_path,monkeypatch,module_name):
    import importlib
    from storage import live_db
    mod=importlib.import_module('labs.engine.'+module_name)
    prefix='gold' if module_name.startswith('gold') else 'crudem'
    path=tmp_path/'dry.db';c=sqlite3.connect(path)
    c.execute(f'CREATE TABLE live_{prefix}_trades (trade_date TEXT,book TEXT,trade_ref TEXT,symbol TEXT,signal_symbol TEXT,qty REAL,cid INTEGER,direction TEXT,signal_ts TEXT,entry_ts TEXT,exit_ts TEXT,entry_price REAL,exit_price REAL,stop_price REAL,target_price REAL,stop_moved_at TEXT,exit_reason TEXT,net_rs REAL)')
    c.execute(f'CREATE TABLE live_{prefix}_orders (trade_ref TEXT,kind TEXT,delay_s REAL,bar_open REAL)')
    c.execute(f'CREATE TABLE live_{prefix}_decisions (book TEXT,signal_ts TEXT,outcome TEXT)')
    for i in range(75):
        day='2026-10-22' if i<74 else '2026-09-01'
        c.execute(f'INSERT INTO live_{prefix}_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (day,'dry',str(i),'TEST','SIGNAL',10,1,'short',day+'T10:00:00',day+'T10:00:01',day+'T11:00:00',100,90,110,80,None,'target',100))
        c.execute(f'INSERT INTO live_{prefix}_orders VALUES (?,?,?,?)',(str(i),'entry',1,100))
        c.execute(f'INSERT INTO live_{prefix}_decisions VALUES (?,?,?)',('dry',day+'T10:00:00','taken'))
    c.commit();c.close()
    monkeypatch.setattr(live_db,'get_live_conn',lambda:sqlite3.connect(path))
    report=mod.dry_run_data(date_clause=' AND trade_date >= ? AND trade_date <= ?',date_params=('2026-10-01','2026-10-22'))
    assert report['closed']==74 and report['net_total']==7400
    if prefix=='gold':assert report['decisions']=={'taken':74}


def test_flask_page_filter_and_excursion_endpoint(tmp_path,monkeypatch):
    from flask import Flask
    from labs.ui.routes import labs_bp
    from storage import db
    from config import labs_config
    from labs.services import strategy_excursions
    path=tmp_path/'paper.db';c=sqlite3.connect(path);table(c,'gold_cci_trades')
    insert(c,'gold_cci_trades',trade());insert(c,'gold_cci_trades',trade('2026-09-01'))
    c.commit();c.close()
    live=tmp_path/'dry.db';sqlite3.connect(live).close()
    monkeypatch.setattr(db,'get_conn',lambda:sqlite3.connect(path))
    monkeypatch.setattr(labs_config,'LIVE_DB_PATH',live)
    root=Path(__file__).resolve().parents[1]
    app=Flask('strategy-report-test',template_folder=str(root/'templates'));app.register_blueprint(labs_bp)
    client=app.test_client()
    url='/labs/live?tab=gold_cci&date_from=2026-10-22&date_to=2026-10-22'
    response=client.get(url);html=response.get_data(as_text=True)
    assert response.status_code==200 and 'Net P&amp;L after costs' in html and '₹93.00' in html
    assert '2026-09-01' not in html
    assert client.get('/labs/api/strategy-report/excursions?tab=bad').status_code==400
    c=sqlite3.connect(path)
    p=build_reports(c,'gold_cci','2026-10-22','2026-10-22',live_conn=sqlite3.connect(live))[0]
    ident=p['trades'][0]['id'];c.close()
    monkeypatch.setattr(strategy_excursions,'contract_marks',lambda conn,t:(marks(),'Test contract marks.'))
    result=client.get('/labs/api/strategy-report/excursions?tab=gold_cci&date_from=2026-10-22&date_to=2026-10-22&ids='+ident)
    assert result.status_code==200
    assert result.json['trades'][ident]['peak']==200
    assert result.json['trades'][ident]['post_trough']==-200


@pytest.mark.parametrize('size',[10,20,25,50,100,150,200])
def test_custom_page_size_keeps_full_range_metrics(size):
    from labs.services.strategy_report import page_size
    p=make_report([trade(seq=i) for i in range(225)],SOURCES['gold_cci'],'gold_cci','Gold',per_page=size)
    assert len(p['trades'])==size
    assert p['stats']['net']==225*93 and p['stats']['closed']==225
    assert p['per_page']==size and page_size(size)==size


@pytest.mark.parametrize('size',[0,-1,201,'infinite',None])
def test_invalid_page_size_uses_bounded_default(size):
    from labs.services.strategy_report import page_size
    assert page_size(size)==100


def test_page_size_dropdown_preserves_dates_and_resets_page(tmp_path,monkeypatch):
    from flask import Flask
    from labs.ui.routes import labs_bp
    from storage import db
    from config import labs_config
    path=tmp_path/'paper.db';c=sqlite3.connect(path);table(c,'gold_cci_trades')
    for seq in range(22):insert(c,'gold_cci_trades',trade(seq=seq))
    c.commit();c.close()
    live=tmp_path/'dry.db';sqlite3.connect(live).close()
    monkeypatch.setattr(db,'get_conn',lambda:sqlite3.connect(path))
    monkeypatch.setattr(labs_config,'LIVE_DB_PATH',live)
    root=Path(__file__).resolve().parents[1]
    app=Flask('rows-test',template_folder=str(root/'templates'));app.register_blueprint(labs_bp)
    response=app.test_client().get('/labs/live?tab=gold_cci&date_from=2026-10-22&date_to=2026-10-22&report_size=10&report_page=2')
    html=response.get_data(as_text=True)
    assert response.status_code==200
    assert '<option value="10" selected>' in html
    assert 'name="report_page" value="1"' in html
    assert 'name="date_from" value="2026-10-22"' in html
    assert 'report_size=10' in html and '₹2,046.00' in html



def test_expiry_strangle_uses_settlement_cohort_but_actual_timestamps():
    r={'expiry':'2026-10-22','entry_date':'2026-10-21','entry_minute':'2026-10-21T15:20:00',
       'exit_minute':'2026-10-22T15:25:00','status':'closed','credit':100,'buyback':50,
       'qty':65,'margin_rs':10000,'gross_rs':3250,'charges_rs':100,'net_rs':3150,'und':'SENSEX'}
    t=normalize(r,SOURCES['expiry_strangle'],'expiry_strangle')
    assert t['date']=='2026-10-22' and t['entry_ts'].startswith('2026-10-21T15:20')
    assert t['closed'] and t['capital']==10000


def test_basket_joins_original_timestamps_and_counts_priced_round_trip():
    import json
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE paper_strategy_trades (trade_date TEXT,seq INTEGER,side TEXT,entry_ts TEXT,exit_ts TEXT)')
    c.execute("INSERT INTO paper_strategy_trades VALUES ('2026-10-22',1,'CALL','2026-10-22T10:00:00','2026-10-22T11:00:00')")
    c.execute('CREATE TABLE basket_trades (trade_date TEXT,seq INTEGER,side TEXT,basket TEXT,status TEXT,legs_json TEXT,gross_rs REAL,charges_rs REAL,net_rs REAL)')
    legs=json.dumps([{'action':'BUY','strike':22000,'otype':'ce','entry_px':100,'exit_px':120}])
    c.execute("INSERT INTO basket_trades VALUES ('2026-10-22',1,'CALL','test','priced',?,1300,100,1200)",(legs,))
    p=build_reports(c,'baskets','2026-10-22','2026-10-22')[0]
    assert p['stats']['closed']==1 and p['stats']['net']==1200
    assert p['trades'][0]['duration']=='1h 0m 0s' and 'BUY 22000ce'==p['trades'][0]['contract']


def test_trade_ids_stay_stable_while_open_marks_change():
    r=trade();a=normalize(r,SOURCES['gold_cci'],'gold_cci')
    r.update(exit_price=95,net_rs=43,updated_at='new')
    assert normalize(r,SOURCES['gold_cci'],'gold_cci')['id']==a['id']


def test_tuesday_put_contract_resolves_pe_not_ce():
    r=trade();r.update(direction=None,side=None,strike=23000,expiry_code='26OCT')
    t=normalize(r,SOURCES['nifty_tuesday_put'],'nifty_tuesday_put')
    assert t['direction']=='PUT' and t['option_type']=='PE'



def test_open_mark_timestamp_is_not_an_actual_exit():
    r=trade();r['status']='open'
    t=normalize(r,SOURCES['gold_cci'],'gold_cci')
    assert not t['closed'] and t['exit_ts'] is None and t['exit_price'] is None
    assert t['exit_value'] is None and t['net']==93


def test_date_only_pair_does_not_invent_execution_times():
    r={'entry_date':'2026-10-20','exit_date':'2026-10-22','status':'closed','net_rs':100,'gross_rs':110,'costs_rs':10,'notional':20000}
    t=normalize(r,SOURCES['infy_tcs_pair'],'infy_tcs_pair')
    assert not t['entry_has_time'] and not t['exit_has_time']
    assert t['duration']=='2 days (dates only)'


def test_multi_leg_excursions_use_synchronized_quotes_not_sum_of_leg_peaks(monkeypatch):
    import pandas as pd
    from market_data import shared_store
    raw=[]
    for i,side in enumerate(['LONG','SHORT']):
        r=trade(seq=i);r.update(direction=side,tradingsymbol='LEG'+str(i),entry_price=100,
                               exit_price=100,qty=1,gross_rs=0,net_rs=0,charges_rs=0,
                               allocated_capital_rs=1000)
        raw.append(r)
    t=make_report(raw,SOURCES['theta_straddle'],'theta_straddle','Straddle')['trades'][0]
    frame=pd.DataFrame([dict(timestamp='2026-10-22T'+ts,tradingsymbol='LEG'+str(i),ltp=price)
                        for i in range(2) for ts,price in [('10:30:00',150),('12:00:00',180)]])
    monkeypatch.setattr(shared_store,'load_options_frame',lambda *a,**kw:frame)
    series,note=contract_marks(sqlite3.connect(':memory:'),t)
    assert all(m[3]==0 for m in series)
    assert estimate(t,series)['peak']==0
    assert estimate(t,series)['post_peak']==0
    assert 'Synchronized' in note
