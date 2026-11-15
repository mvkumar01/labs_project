"""Nifty Omega refinements of 2026-11-15: premium exits, Friday cut, retest filter, delta strike, PUT loss exit with
one re-entry (live/engine/nifty_omega.py, labs/engine/nifty_omega_book.py)."""
from datetime import date, datetime, timedelta
from math import erf, log, sqrt

import pandas as pd

from labs.engine.nifty_omega_book import replay
from live import live_executor
from live.engine import nifty_omega as omega

DAY = '2026-11-18'                       # a Wednesday, 6 days before the 24 Nov expiry
FRIDAY = '2026-11-20'
M = timedelta(minutes=1)


def t(day, hm):
    return datetime.fromisoformat(f'{day}T{hm}')


def session(day, alpha_by_mark, drift=0.5, ema=1.0, quotes=None, expiry=date(2026, 11, 24)):
    """Spot from 24000 rising `drift` points a minute (so the retest filter stays out of the way), alpha on the given
    marks (others 0), EMA9-EMA21 = `ema` (or {mark: value}, others +1), option quotes 100 / 101 for every strike
    23700-24300 unless {(minute, strike, type): (bid, ask)} says otherwise."""
    start = t(day, '09:15')
    minutes = [start + i*M for i in range(376)]
    bars = {m: (24000+i*drift, 24000+i*drift+1, 24000+i*drift-1, 24000+i*drift) for i, m in enumerate(minutes)}
    alpha = {m: 0.0 for m in minutes if m.minute % 5 == 0}
    alpha.update(alpha_by_mark)
    ema_s = pd.Series({m: (ema.get(m, 1.0) if isinstance(ema, dict) else ema) for m in minutes})
    q = dict(quotes or {})
    rows = [dict(minute=m, strike=k, type=typ, bid=q.get((m, k, typ), (100.0, 101.0))[0],
                 ask=q.get((m, k, typ), (100.0, 101.0))[1], tradingsymbol=f'N{k}{typ}')
            for m in minutes for k in range(23700, 24350, 50) for typ in ('ce', 'pe')]
    return dict(bars=bars, alpha=alpha, ema=ema_s, frame=pd.DataFrame(rows), expiry_date=expiry)


def test_premium_targets_are_tested_on_the_bid_before_alpha():
    assert omega.decision_exit(None, 1, 113.5, 101, [12.5, 20]) == ([12.5], 'premium')
    assert omega.decision_exit(None, 1, 121.0, 101, [12.5, 20]) == ([12.5, 20], 'premium')
    assert omega.decision_exit(None, 1, 113.4, 101, [12.5, 20]) == ([], 'premium')
    assert omega.decision_exit(None, 1, None, 101, [20]) == ([], 'premium')
    # spot no longer matters; alpha favourable and EOD still close every held lot
    assert omega.decision_exit(100.0, 1, 90, 101, [12.5, 20]) == ([12.5, 20], 'alpha_fav')
    assert omega.decision_exit(5.0, 1, 90, 101, [20], end=True) == ([20], 'eod')
    assert omega.decision_exit(250.0, 1, 90, 101, [20]) == ([], 'premium')      # beyond +-200 is ignored


def test_the_loss_exit_is_alpha_minus_30_for_calls_and_plus_20_for_puts():
    assert omega.decision_exit(-29.0, 1, 90, 101, [12.5, 20]) == ([], 'premium')
    assert omega.decision_exit(-30.0, 1, 90, 101, [12.5, 20]) == ([12.5, 20], 'alpha_stop')
    assert omega.decision_exit(19.0, -1, 90, 101, [12.5, 20]) == ([], 'premium')
    assert omega.decision_exit(20.0, -1, 90, 101, [12.5, 20]) == ([12.5, 20], 'alpha_stop')
    # a premium hit on the same minute names the exit 'premium' (lot 1's reason, as the research): no re-entry
    assert omega.decision_exit(25.0, -1, 114, 101, [12.5, 20]) == ([12.5, 20], 'premium')
    assert omega.REENTRY_SIDES == (-1,)


def test_no_friday_entry_from_a_1200_signal():
    early = session(FRIDAY, {t(FRIDAY, '11:55'): 40.0})
    assert omega.first_signal(early, FRIDAY, t(FRIDAY, '15:15'))[:2] == (t(FRIDAY, '11:55'), 1)
    late = session(FRIDAY, {t(FRIDAY, '12:00'): 40.0, t(FRIDAY, '13:00'): 40.0})
    assert omega.first_signal(late, FRIDAY, t(FRIDAY, '15:15')) is None
    weekday = session(DAY, {t(DAY, '12:00'): 40.0})
    assert omega.first_signal(weekday, DAY, t(DAY, '15:15'))[0] == t(DAY, '12:00')


def test_retest_filter_skips_a_signal_near_the_old_high_and_a_later_cross_can_trade():
    data = session(DAY, {t(DAY, '10:30'): 40.0, t(DAY, '10:35'): 10.0, t(DAY, '11:30'): 40.0}, drift=0)
    data['bars'][t(DAY, '11:30')] = (24050.0, 24051, 24049, 24050)          # well through it by then
    data['bars'][t(DAY, '09:40')] = (24000, 24008, 23999, 24000)            # old high 24008, made 50+ min ago
    mark = t(DAY, '10:30')
    for spot, blocked in ((23997.0, False), (23998.0, True), (24017.0, True), (24018.0, False)):
        data['bars'][mark] = (spot, spot+1, spot-1, spot)
        assert omega.retest_blocked(data, DAY, mark, 1) is blocked, spot
    data['bars'][mark] = (24008.0, 24009, 24007, 24008)                      # at the level: skipped
    assert omega.first_signal(data, DAY, t(DAY, '15:15'))[0] == t(DAY, '11:30')
    # the level is the high as it stood 45 minutes before the signal: a newer high does not count
    data['bars'][t(DAY, '10:00')] = (24000, 24100, 23999, 24000)
    assert omega.retest_blocked(data, DAY, mark, 1)
    # PUT: the old low
    data['bars'][t(DAY, '09:41')] = (24000, 24001, 23990, 24000)
    data['bars'][mark] = (23995.0, 23996, 23994, 23995)
    assert omega.retest_blocked(data, DAY, mark, -1)


def _bs_price(fwd, strike, T, iv, call):
    cdf = lambda x: 0.5*(1+erf(x/sqrt(2)))                                    # noqa: E731
    sd = iv*sqrt(T)
    d1 = (log(fwd/strike)+0.5*sd*sd)/sd
    c = fwd*cdf(d1)-strike*cdf(d1-sd)
    return c if call else c-fwd+strike


def _chain(minute, expiry, spot=24000.0, iv=0.15):
    T = (datetime.combine(expiry, datetime.min.time()).replace(hour=15, minute=30)-minute).total_seconds()/(365*86400)
    rows = []
    for k in range(23600, 24450, 50):
        for typ in ('ce', 'pe'):
            p = _bs_price(spot, k, T, iv, typ == 'ce')
            rows.append(dict(minute=minute, strike=k, type=typ, bid=round(p-0.5, 2), ask=round(p+0.5, 2),
                             tradingsymbol=f'N{k}{typ}'))
    return pd.DataFrame(rows)


def test_delta_strike_near_expiry_and_atm_otherwise():
    minute = t('2026-11-23', '10:01')
    data = dict(frame=_chain(minute, date(2026, 11, 24)), expiry_date=date(2026, 11, 24))
    deltas = omega.strike_deltas(data, minute, 24000, 1, 24000.0)
    assert set(deltas) <= {24000-s for s in range(0, 301, 50)} and abs(deltas[24000]-0.5) < 0.02
    call = omega.pick_strike(data, '2026-11-23', minute, 24000, 1, 24000.0)
    put = omega.pick_strike(data, '2026-11-23', minute, 24000, -1, 24000.0)
    assert call < 24000 < put
    assert abs(deltas[call]-0.70) == min(abs(d-0.70) for d in deltas.values())
    # 2 days to expiry: ATM
    assert omega.pick_strike(data, '2026-11-22', minute, 24000, 1, 24000.0) == 24000


def test_put_loss_exit_then_one_reentry_on_a_cross_below_zero():
    alpha = {t(DAY, '10:00'): -40.0,                     # PUT signal, entry 10:01
             t(DAY, '10:05'): -5.0, t(DAY, '10:10'): 20.0,   # +20: loss exit decided 10:10, filled 10:11
             t(DAY, '10:15'): 5.0, t(DAY, '10:20'): -3.0,    # 5 -> -3: re-entry signal 10:20, entry 10:21
             t(DAY, '10:25'): 20.0,                         # the re-entry's own +20 exit
             t(DAY, '10:30'): 5.0, t(DAY, '10:35'): -5.0}   # no second re-entry
    data = session(DAY, alpha, ema=-1.0)
    trades = replay(DAY, data, t(DAY, '15:30'))
    assert [(x['side'], x['entry_ts'], x['exit_ts'], x['exit_reason']) for x in trades] == [
        ('PUT', t(DAY, '10:01'), t(DAY, '10:11'), 'alpha_stop'),
        ('PUT', t(DAY, '10:21'), t(DAY, '10:26'), 'alpha_stop')]
    # EMAs not bearish on the 10:20 crossing: the re-entry waits for the next crossing (10:35)
    data = session(DAY, alpha, ema=-1.0)
    data['ema'][t(DAY, '10:20')] = 1.0
    assert [x['entry_ts'] for x in replay(DAY, data, t(DAY, '15:30'))] == [t(DAY, '10:01'), t(DAY, '10:36')]


def test_a_call_keeps_alpha_minus_30_and_has_no_reentry():
    alpha = {t(DAY, '10:00'): 40.0, t(DAY, '10:05'): -20.0, t(DAY, '10:10'): -30.0, t(DAY, '10:15'): -5.0,
             t(DAY, '10:20'): 5.0}
    trades = replay(DAY, session(DAY, alpha), t(DAY, '15:30'))
    assert [(x['side'], x['exit_ts'], x['exit_reason']) for x in trades] == [('CALL', t(DAY, '10:11'), 'alpha_stop')]


def test_no_reentry_on_a_friday_from_1200():
    alpha = {t(FRIDAY, '11:30'): -40.0, t(FRIDAY, '11:45'): 25.0, t(FRIDAY, '11:50'): 5.0,
             t(FRIDAY, '12:00'): -5.0}
    trades = replay(FRIDAY, session(FRIDAY, alpha, ema=-1.0), t(FRIDAY, '15:30'))
    assert len(trades) == 1 and trades[0]['exit_reason'] == 'alpha_stop'
    alpha[t(FRIDAY, '11:55')] = -5.0                     # crossing at 11:55 is still allowed
    assert len(replay(FRIDAY, session(FRIDAY, alpha, ema=-1.0), t(FRIDAY, '15:30'))) == 2


def test_lots_sell_on_the_bid_at_ask_plus_12_5_and_20():
    quotes = {(t(DAY, '10:05'), 24000, 'ce'): (113.5, 114.0), (t(DAY, '10:06'), 24000, 'ce'): (112.0, 113.0),
              (t(DAY, '10:20'), 24000, 'ce'): (121.0, 122.0), (t(DAY, '10:21'), 24000, 'ce'): (120.5, 121.5)}
    trade, = replay(DAY, session(DAY, {t(DAY, '10:00'): 40.0}, quotes=quotes), t(DAY, '15:30'))
    assert (trade['entry_ask'], trade['lotA_ts'], trade['lotA_bid'], trade['exit_ts'], trade['exit_bid']) == (
        101.0, t(DAY, '10:06'), 112.0, t(DAY, '10:21'), 120.5)
    assert trade['exit_reason'] == 'premium' and trade['strike'] == 24000      # 6 days to expiry: ATM


def test_the_live_decision_contract_names_the_refined_rules():
    assert live_executor.expected_decision_abi(omega.KEY) == omega.ABI
