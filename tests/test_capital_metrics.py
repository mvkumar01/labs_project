import sqlite3
from labs.services.capital_metrics import peak_deployed_capital
from labs.services.capital_metrics import recorded_nifty_quantity
from labs.services.book_overview_more import _proposer


def position(capital, entry, exit_=None):
    return dict(capital=capital, entry_ts='2026-10-22T'+entry,
                exit_ts='2026-10-22T'+exit_ if exit_ else None)


def test_sequential_trades_reuse_capital():
    assert peak_deployed_capital([position(100000,'09:30','10:00'),
                                  position(130000,'10:00','11:00')]) == 130000


def test_same_second_round_trips_still_deploy_capital_without_double_counting():
    assert peak_deployed_capital([position(100000,'09:30','10:00'),
                                  position(130000,'10:00','10:00'),
                                  position(120000,'10:00','11:00')]) == 130000


def test_simultaneous_positions_add_only_while_they_overlap():
    assert peak_deployed_capital([position(100000,'09:30','11:00'),
                                  position(130000,'10:00','10:30'),
                                  position(300000,'12:00','13:00')]) == 300000
    assert peak_deployed_capital([position(100000,'09:30'),position(130000,'10:00')]) == 230000


def test_unknown_capital_or_timing_is_not_a_known_peak():
    assert peak_deployed_capital([position(None,'09:30')]) is None
    assert peak_deployed_capital([dict(capital=100000)]) is None


def test_flat_nifty_trade_quantity_requires_matching_recorded_charges():
    from labs.engine.charges import round_trip_charges
    trade=dict(entry_price=100,exit_price=100,gross_rs=0,
               charges_rs=round_trip_charges(100,100,11960)['total'])
    assert recorded_nifty_quantity(trade)==11960
    assert recorded_nifty_quantity(dict(trade,charges_rs=trade['charges_rs']+1)) is None
    assert recorded_nifty_quantity(dict(trade,gross_rs=1000)) is None


def test_nifty_quantity_reconciles_the_precision_of_stored_premiums():
    from labs.engine.charges import round_trip_charges
    from labs.services.strategy_report import normalize,SOURCES
    entry,exit_,qty=100.004,100.006,11960
    trade=dict(trade_date='2026-10-22',seq=1,side='CALL',entry_price=round(entry,2),
               exit_price=round(exit_,2),gross_rs=round((exit_-entry)*qty,2),
               charges_rs=round_trip_charges(entry,exit_,qty)['total'],
               entry_ts='2026-10-22T10:00:00',exit_ts='2026-10-22T11:00:00')
    assert recorded_nifty_quantity(trade)==qty
    assert normalize(trade,SOURCES['proposer_nifty_v5'],'proposer_nifty_v5')['qty']==qty


def test_nifty_uses_recorded_quantity_instead_of_sensex_fixed_units():
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE proposer_nifty_v5_daily (trade_date TEXT,net_rs REAL,status TEXT)')
    conn.execute("INSERT INTO proposer_nifty_v5_daily VALUES ('2026-10-22',20000,'closed')")
    conn.execute('CREATE TABLE proposer_nifty_v5_trades (trade_date TEXT,seq INTEGER,side TEXT,entry_price REAL,exit_price REAL,gross_rs REAL,entry_ts TEXT,exit_ts TEXT)')
    for seq,entry,exit_,qty in [(1,100,110,11960),(2,200,210,5980)]:
        conn.execute('INSERT INTO proposer_nifty_v5_trades VALUES (?,?,?,?,?,?,?,?)',
                     ('2026-10-22',seq,'CALL',entry,exit_,(exit_-entry)*qty,
                      f'2026-10-22T{8+seq:02d}:30:00',f'2026-10-22T{9+seq:02d}:00:00'))
    card=_proposer(conn,'proposer_nifty_v5','2026-10-22')
    assert card['capital']==1196000


def test_sensex_fixed_size_is_peak_entry_cost_not_sum_of_reentries():
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE proposer_v5_dt25_daily (trade_date TEXT,net_rs REAL,status TEXT)')
    conn.execute("INSERT INTO proposer_v5_dt25_daily VALUES ('2026-10-22',20000,'closed')")
    conn.execute('CREATE TABLE proposer_v5_dt25_trades (trade_date TEXT,seq INTEGER,side TEXT,entry_price REAL,entry_ts TEXT,exit_ts TEXT)')
    for seq,price in [(1,100),(2,200)]:
        conn.execute('INSERT INTO proposer_v5_dt25_trades VALUES (?,?,?,?,?,?)',
                     ('2026-10-22',seq,'CALL',price,f'2026-10-22T{8+seq:02d}:30:00',f'2026-10-22T{9+seq:02d}:00:00'))
    assert _proposer(conn,'proposer_v5_dt25','2026-10-22')['capital']==400000
