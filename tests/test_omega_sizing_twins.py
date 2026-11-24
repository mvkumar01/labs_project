"""Nifty Omega 3 sizing twins (Vipin 2026-11-24): omega_chop_half and omega_fade_streak re-size Omega's own trades."""
import sqlite3

import jinja2

from labs.engine import nifty_omega_book as book, nna_twins_tracker as twins, omega_sizing_twins as ot, tick_replay
from labs.services import strategy_report as sr
from live.engine import nifty_omega as omega
from tests.test_nifty_omega_refinements import DAY, session, t

UP = ('x', 24000.0, 24210.0, 23990.0, 24200.0)        # efficiency +0.91: trend up
DOWN = ('x', 24200.0, 24210.0, 23990.0, 24000.0)      # -0.91: trend down
CHOP = ('x', 24000.0, 24210.0, 23990.0, 24050.0)      # +0.23: choppy


def candles(d2, d1):
    return [('2026-11-16',) + d2[1:], ('2026-11-17',) + d1[1:]]


def test_day_type_and_lots():
    assert ot.day_type(UP[1:]) == (True, 1) and ot.day_type(DOWN[1:]) == (True, -1)
    assert ot.day_type(CHOP[1:])[0] is False and ot.day_type((1, 1, 1, 1)) == (False, 0)
    assert ot.day_type((100, 110, 100, 104)) == (True, 1)                 # exactly 0.4 is a trend day
    half = lambda d1: ot.lots_per_target('chop_half', 1, d1[1:], UP[1:])   # noqa: E731
    assert (half(UP), half(DOWN), half(CHOP)) == (2, 2, 1)
    fade = lambda side, d2, d1: ot.lots_per_target('fade_streak', side, d1[1:], d2[1:])   # noqa: E731
    assert fade(-1, UP, UP) == 2 and fade(1, DOWN, DOWN) == 2               # against a 2-day streak: double
    assert fade(1, UP, UP) == 1 and fade(-1, DOWN, DOWN) == 1               # with the streak
    assert fade(-1, DOWN, UP) == 1 and fade(-1, CHOP, UP) == 1 and fade(-1, UP, CHOP) == 1


def call_day(same_minute=False):
    """CALL signal 10:00, entry 10:01 at ask 101 (ATM 24000). Lot 1 at 10:10 (bid 114), lot 2 at 10:20 (bid 122),
    or both at 10:10 (bid 122)."""
    q = {}
    first, second = (122.0, 122.0) if same_minute else (114.0, 122.0)
    for hm, bid in (('10:10', first), ('10:11', first), ('10:20', second), ('10:21', second)):
        q[(t(DAY, hm), 24000, 'ce')] = (bid, bid + 1)
    return session(DAY, {t(DAY, '10:00'): 40.0}, quotes=q)


def omega_db(same_minute=False):
    conn = sqlite3.connect(':memory:')
    data = call_day(same_minute)
    trades = book.replay(DAY, data, t(DAY, '15:30'), snap=False, late=False)
    assert len(trades) == 1 and trades[0]['status'] == 'closed'
    inp = dict(expiry='26N24', range_lo=23650, range_hi=24350, marks=[t(DAY, '10:00')], alpha=[40.0], spot=[24000.0],
               session_done=True)
    twins._ensure_tables(conn, omega.KEY)
    twins._save(conn, DAY, book.BOOK, dict(inp=inp, trades=trades, prev_close=24000.0, prev_close_date='2026-11-17',
                                            prev_close_source='test', baseline_date='2026-11-17', vix_open=None,
                                            vix_source=None, replay=tick_replay.ONE_MINUTE))
    return conn, trades[0]


def test_one_lot_per_target_is_omega_exactly_and_two_doubles_the_gross():
    for same in (False, True):
        conn, trade = omega_db(same)
        row = dict(zip([c[0] for c in conn.execute('SELECT * FROM nifty_omega_trades').description],
                       conn.execute('SELECT * FROM nifty_omega_trades').fetchone()))
        assert (row['lotA_ts'] == row['exit_ts']) is same
        one, two = ot.price(row, 1), ot.price(row, 2)
        assert one['net_rs'] == row['net_rs'] == trade['net_rs'] and one['qty'] == 130 and one['lots'] == 2
        assert abs(one['charges_rs'] - row['charges_rs']) < 1e-9
        assert two['qty'] == 260 and two['lots'] == 4 and abs(two['gross_rs'] - 2*row['gross_rs']) < 1e-9
        assert row['charges_rs'] < two['charges_rs'] < 2*row['charges_rs']    # brokerage per order does not double


def test_derive_day_sizes_each_twin_and_stores_the_context():
    conn, trade = omega_db()
    out = ot.derive_day(conn, DAY, candles=candles(UP, CHOP))           # previous day choppy
    assert out['omega_chop_half']['lots'] == [2] and out['omega_chop_half']['net_rs'] == trade['net_rs']
    assert out['omega_fade_streak']['lots'] == [2] and out['omega_fade_streak']['net_rs'] == trade['net_rs']
    out = ot.derive_day(conn, DAY, candles=candles(DOWN, DOWN))         # two down days: the CALL fades them
    assert out['omega_chop_half']['lots'] == [4] and out['omega_fade_streak']['lots'] == [4]
    assert out['omega_fade_streak']['net_rs'] > 2*trade['net_rs']       # (net is positive; charges grow < 2x)
    daily = dict(zip([c[0] for c in conn.execute('SELECT * FROM omega_fade_streak_daily').description],
                     conn.execute('SELECT * FROM omega_fade_streak_daily').fetchone()))
    assert daily['qty'] == 260 and daily['status'] == 'traded' and daily['n_trades'] == 1
    assert daily['strategy_version'].startswith(ot.BOOKS['omega_fade_streak'].version + '|')
    assert conn.execute('SELECT COUNT(*) FROM omega_fade_streak_trades').fetchone()[0] == 1    # replaced, not added
    ctx = conn.execute('SELECT d1_date, d2_date, source FROM omega_twins_context').fetchone()
    assert ctx == ('2026-11-17', '2026-11-16', 'given')
    rows, trades, stats = ot.tab_data(conn, 'omega_fade_streak')
    assert rows[0]['prev_day'] == 'trend down -0.91' and rows[0]['lots'] == 4 and stats['backfill_before'] == ot.START


def test_no_omega_row_means_nothing_and_a_forward_day_before_start_is_refused():
    conn = sqlite3.connect(':memory:')
    assert ot.derive_day(conn, DAY, candles=candles(UP, UP)) is None
    assert ot.run_paper_day('2026-11-24') is None and ot.START == '2026-11-25'


def test_report_and_template_label_backfill():
    assert sr.BACKFILL_BEFORE['omega_chop_half'] == sr.BACKFILL_BEFORE['omega_fade_streak'] == ot.START
    raw = [dict(id=1, trade_date='2026-11-24', status='closed', side='CALL', entry_ts='2026-11-24T10:01',
                exit_ts='2026-11-24T10:21', net_rs=2000.0, gross_rs=2100.0, charges_rs=100.0, qty=260,
                entry_ask=101.0, exit_bid=122.0, tradingsymbol='N24000ce')]
    rep = sr.make_report(raw, sr.SOURCES['omega_chop_half'], 'omega_chop_half', 'x')
    assert rep['trades'][0]['backfill']
    env = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))
    html = env.get_template('_omega_twins.html').render(
        nna_tw_stats=dict(key='omega_fade_streak', label='Omega 3 · fade 2-day streak', backfill_before=ot.START,
                          trades=2, unseen_trades=1, net_rs=900.0, unseen_net=-100.0, max_dd_rs=100.0),
        nna_tw_rows=[dict(trade_date='2026-11-25', status='traded', n_trades=1, net_rs=-100.0, lots=4,
                          prev_day='trend up +0.62', prev2_day='trend up +0.55')],
        nna_tw_trades=[dict(trade_date='2026-11-24', tradingsymbol='X', lots=2, entry_ts='t', lotA_ts=None,
                            exit_ts=None, net_rs=1.0)])
    assert 'Fade a 2-day streak' in html and html.count('<span class="muted">backfill</span>') == 1
    assert 'trend up +0.62' in html and 'Forward paper from 2026-11-25: 1 trades' in html


def test_registered_in_the_paper_loop_after_omega():
    src = open('pa_paper_tracker_loop.py', encoding='utf-8').read()
    assert src.index('("nifty_omega", run_omega_day)') < src.index('("omega_twins", run_omega_twins_day)')


def test_tab_renders_through_the_route(monkeypatch):
    from flask import Flask
    from labs.engine import paper_strategy_tracker
    from labs.ui.routes import labs_bp
    app = Flask(__name__, template_folder='../templates')
    app.register_blueprint(labs_bp)
    for key, label in (('omega_chop_half', 'Half after chop'), ('omega_fade_streak', 'Fade a 2-day streak')):
        conn, _ = omega_db()                                 # the page closes its connection: one per request
        conn.row_factory = sqlite3.Row
        paper_strategy_tracker._ensure_tables(conn)          # the page reads the v2.11 book first, as in production
        ot.derive_day(conn, DAY, candles=candles(UP, UP))
        monkeypatch.setattr('storage.db.get_conn', lambda conn=conn: conn)
        html = app.test_client().get(f'/labs/live?tab={key}').get_data(as_text=True)
        assert label in html and DAY in html and 'trend up +0.91' in html
