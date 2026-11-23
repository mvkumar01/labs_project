"""Nifty Omega snap exit with an OI check, then one fresh signal (Vipin 2026-11-23; alphaIMB research
2026-11-14_alpha_velocity_writers README 69-71): live/engine/nifty_omega.py, the paper book and the live runner."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd

from labs.engine import nifty_omega_book as book
from live import live_executor, live_runner, nifty_omega_runner as nr
from live.engine import nifty_omega as omega
from tests.test_nifty_omega_refinements import DAY, session, t

M = timedelta(minutes=1)


def put_snap_day(**kw):
    """PUT signal 10:00 (entry 10:01); at 10:03 put writers have added (1-min alpha back to +6.7, share 1.0): snap.
    A CALL signal follows at 10:30."""
    data = session(DAY, {t(DAY, '10:00'): -40.0, t(DAY, '10:30'): 40.0}, ema={t(DAY, '10:00'): -1.0}, **kw)
    data['pc'] = {t(DAY, '10:00'): (100.0, 140.0), t(DAY, '10:02'): (100.0, 140.0), t(DAY, '10:03'): (160.0, 140.0)}
    return data


def test_contract_and_paper_versions():
    assert omega.SNAP_ABI.startswith('nifty-omega-v2-snapoi15-60-fresh1') and omega.SNAP_START == '2026-11-23'
    assert live_executor.expected_decision_abi(omega.KEY) == omega.ABI
    assert book.SNAP_BOOK.version == omega.SNAP_ABI and book.PRE_SNAP_BOOK.version == omega.PRE_SNAP_ABI
    assert book.BACKFILL_VERSION == omega.PRE_SNAP_ABI+'+backfill'          # the stored backfill rows' label


def test_share_against_the_trade():
    assert omega.against_share((100, 140), (160, 140), -1) == 1.0         # put writers adding: against a PUT
    assert omega.against_share((100, 140), (160, 140), 1) == 0.0
    assert omega.against_share((0, 0), (10, 30), 1) == 0.75               # CALL: calls +30 against, puts +10 for
    assert omega.against_share((0, 0), (-10, -30), 1) == 0.25             # puts covering against, calls covering for
    assert omega.against_share((5, 5), (5, 5), 1) == 0.0                  # no flow


def test_snap_window_and_thresholds():
    s = t(DAY, '10:00')
    data = dict(pc={s: (100.0, 140.0), s+M: (160.0, 140.0), s+2*M: (160.0, 140.0), s+5*M: (160.0, 140.0),
                    s+6*M: (160.0, 140.0)})
    assert [omega.snap_exit(data, -1, s, s+i*M) for i in (1, 2, 5, 6)] == [False, True, True, False]
    data['pc'][s+2*M] = (100.0, 140.0-1e-9)                               # 1-min alpha -16.7: still bearish
    assert not omega.snap_exit(data, -1, s, s+2*M)
    data['pc'][s+2*M] = (130.0, 100.0)                                    # alpha +13: below +15 for a CALL
    assert omega.snap_exit(data, 1, s, s+2*M) is False                    # ... but the flow is for the CALL
    data['pc'][s+2*M] = (80.0, 100.0)                                     # puts -20, calls -40 -> 40/60 against a CALL? no
    assert omega.snap_exit(data, 1, s, s+2*M) is False                    # share = 20/60 against
    data['pc'][s+2*M] = (100.0, 200.0)                                    # calls +60: against a CALL, alpha -33
    assert omega.snap_exit(data, 1, s, s+2*M) is True
    del data['pc'][s]
    assert omega.snap_exit(data, 1, s, s+2*M) is False                    # no OI at the signal mark: no snap


def test_targets_and_alpha_exits_come_first():
    s = t(DAY, '10:00')
    data = dict(alpha={s+5*M: 25.0}, pc={s: (100.0, 140.0), s+5*M: (160.0, 140.0)})
    assert omega.decide(data, s, s+5*M, -1, 90, 101, [12.5, 20]) == ([12.5, 20], 'alpha_stop')
    data['alpha'] = {s+5*M: 0.0}
    assert omega.decide(data, s, s+5*M, -1, 90, 101, [12.5, 20]) == ([12.5, 20], 'snap_oi')
    assert omega.decide(data, s, s+5*M, -1, 114, 101, [12.5, 20]) == ([12.5, 20], 'premium')
    assert omega.decide(data, s, s+5*M, -1, 90, 101, [12.5, 20], snap=False) == ([], 'premium')


def test_what_an_exit_allows_next():
    k = t(DAY, '10:03')
    assert omega.after_exit('snap_oi', -1, k, set()) == ('fresh', t(DAY, '10:05'))
    assert omega.after_exit('snap_oi', 1, t(DAY, '10:05'), set()) == ('fresh', t(DAY, '10:10'))
    assert omega.after_exit('snap_oi', -1, k, {'fresh'}) == ('reclaim', t(DAY, '10:00'))
    assert omega.after_exit('snap_oi', 1, k, {'fresh'}) is None
    assert omega.after_exit('alpha_stop', -1, t(DAY, '10:10'), {'fresh'}) == ('reclaim', t(DAY, '10:10'))
    assert omega.after_exit('alpha_stop', -1, t(DAY, '10:10'), {'reclaim'}) is None
    assert omega.after_exit('premium', -1, k, set()) is None


def test_paper_snap_exit_then_one_fresh_entry():
    trades = book.replay(DAY, put_snap_day(), t(DAY, '15:30'), snap=True)
    assert [(x['side'], x['entry_ts'], x['exit_ts'], x['exit_reason']) for x in trades] == [
        ('PUT', t(DAY, '10:01'), t(DAY, '10:04'), 'snap_oi'),
        ('CALL', t(DAY, '10:31'), t(DAY, '15:26'), 'eod')]
    # before SNAP_START (default): no snap; the PUT is held to its alpha stop on the 10:30 mark, no fresh entry
    old = book.replay(DAY, put_snap_day(), t(DAY, '15:30'))
    assert [(x['side'], x['exit_ts'], x['exit_reason']) for x in old] == [('PUT', t(DAY, '10:31'), 'alpha_stop')]


def test_paper_fresh_entry_is_once_a_day_and_a_later_put_snap_uses_the_reclaim():
    data = session(DAY, {t(DAY, '10:00'): -40.0, t(DAY, '10:30'): -40.0, t(DAY, '10:35'): -40.0,
                         t(DAY, '10:40'): -40.0, t(DAY, '11:00'): -40.0},
                   ema=-1.0)
    # every PUT snaps 3 minutes after its signal
    data['pc'] = {}
    for mark in ('10:00', '10:30', '10:55', '11:30'):
        s = t(DAY, mark)
        data['pc'][s], data['pc'][s+3*M] = (100.0, 140.0), (160.0, 140.0)
    data['alpha'][t(DAY, '10:50')] = 0.0
    data['alpha'][t(DAY, '10:55')] = -1.0                                 # reclaim cross 0 -> below 0 at 10:55
    trades = book.replay(DAY, data, t(DAY, '15:30'), snap=True)
    assert [(x['entry_ts'], x['exit_reason']) for x in trades] == [
        (t(DAY, '10:01'), 'snap_oi'),       # first snap -> one fresh entry
        (t(DAY, '10:31'), 'snap_oi'),       # fresh trade snaps -> the PUT reclaim (fresh already used)
        (t(DAY, '10:56'), 'snap_oi')]       # reclaim snaps -> nothing left


# ------------------------------------------------------------------ live runner ---
class Live:
    def __init__(self, monkeypatch, data, state=None, config=None):
        self.state = dict(state or {'position': 'NONE'})
        self.config = dict(config or {})
        self.orders = []
        quotes = {(m, s): b for m, s, b in zip(data['frame'].minute, data['frame'].tradingsymbol, data['frame'].bid)}
        self.now = None
        svc = SimpleNamespace(
            get_trade_state=lambda u, c: dict(self.state), save_trade_state=lambda u, c, s: self.state.update(s),
            reset_trade_state=lambda u, c: (self.state.clear(), self.state.update(position='NONE')),
            get_config=lambda u, c, k: self.config.get(k, ''), set_config=lambda u, c, k, v: self.config.__setitem__(k, v),
            get_config_int=lambda u, c, k: 0, get_lots=lambda u, c: 2)
        monkeypatch.setattr(nr, 'svc', svc)
        monkeypatch.setattr(nr, 'prior_entry', lambda u, c, d, dry: True)
        monkeypatch.setattr(omega, 'snapshots', lambda day, asof: data)
        monkeypatch.setattr(omega, 'SNAP_START', DAY)
        monkeypatch.setattr(live_runner, '_now_iso', lambda: self.now.isoformat())
        monkeypatch.setattr(live_runner, '_ist_date_of', lambda v: v[:10])
        monkeypatch.setattr(live_runner, '_to_ist_minute_naive',
                            lambda v: pd.Timestamp(v).to_pydatetime().replace(second=0, microsecond=0))
        monkeypatch.setattr(live_runner, 'check_daily_loss', lambda *a, **k: True)
        monkeypatch.setattr(live_runner, '_fast_ltp', lambda a, s: quotes[(self.now, s)])
        monkeypatch.setattr(live_runner, '_freshly_applied', lambda r, dry_run: True)
        monkeypatch.setattr(live_runner, '_record_exit_result', lambda *a, **k: None)

        def route(adapter, u, c, *, action, side, symbol, qty, price, dry_run, entry_rule, attempt=0):
            self.orders.append(dict(action=action, side=side, qty=qty, rule=entry_rule, at=self.now))
            return SimpleNamespace(status='COMPLETE', avg_fill_price=price, raw={}, broker_order_id=None)
        monkeypatch.setattr(live_runner, '_route_order', route)
        monkeypatch.setattr(nr.ex, 'filled_qty', lambda r: self.orders[-1]['qty'])

    def run(self, hm):
        self.now = t(DAY, hm)
        nr.execute('u', 'c', None, self.now, True)


def open_put():
    return dict(position='OPEN', side='PUT', symbol='24000pe', qty=2*omega.LOT, virtual=1,
                entry_time=f'{DAY}T10:01:05', entry_rule='omega_ema9_21')


def test_live_snap_exit_arms_one_fresh_entry_which_trades_the_next_signal(monkeypatch):
    data = put_snap_day()
    data['frame']['tradingsymbol'] = data['frame'].strike.astype(str)+data['frame'].type
    entry = json.dumps(dict(day=DAY, strike=24000, ask=101.0))
    live = Live(monkeypatch, data, open_put(), {'omega_entry': entry})
    live.run('10:03')
    assert live.orders == []                                              # 10:03 not decided yet
    live.run('10:04')
    assert [(o['action'], o['qty'], o['rule']) for o in live.orders] == [('EXIT', 2*omega.LOT, 'omega_snap_oi')]
    arm = json.loads(live.config['omega_reentry'])
    assert (arm['kind'], arm['mark'], arm['used'], arm['taken']) == ('fresh', t(DAY, '10:05').isoformat(), False, [])
    for hm in ('10:05', '10:30'):
        live.run(hm)
    assert len(live.orders) == 1                                          # no signal yet
    live.run('10:31')
    assert [(o['action'], o['side']) for o in live.orders[1:]] == [('ENTER', 'CALL')]
    assert json.loads(live.config['omega_reentry'])['used'] is True


def test_live_put_snap_after_the_fresh_entry_arms_the_reclaim_and_the_reclaim_is_once(monkeypatch):
    data = put_snap_day()
    data['frame']['tradingsymbol'] = data['frame'].strike.astype(str)+data['frame'].type
    used_fresh = dict(day=DAY, side=1, kind='fresh', mark=t(DAY, '09:45').isoformat(), used=True, taken=[], dry_run=1)
    live = Live(monkeypatch, data, open_put(), {'omega_entry': json.dumps(dict(day=DAY, strike=24000, ask=101.0)),
                                                'omega_reentry': json.dumps(used_fresh)})
    live.run('10:04')
    arm = json.loads(live.config['omega_reentry'])
    assert (arm['kind'], arm['mark'], arm['taken']) == ('reclaim', t(DAY, '10:00').isoformat(), ['fresh'])
    # a used reclaim written before this change (no 'kind') blocks a second reclaim
    old = dict(day=DAY, side=-1, mark=t(DAY, '09:30').isoformat(), used=True, dry_run=1)
    live = Live(monkeypatch, data, open_put(), {'omega_entry': json.dumps(dict(day=DAY, strike=24000, ask=101.0)),
                                                'omega_reentry': json.dumps(old)})
    data['pc'] = {}
    data['alpha'][t(DAY, '10:05')] = 25.0                                 # PUT alpha stop
    live.run('10:06')
    assert live.orders[0]['rule'] == 'omega_alpha_stop' and live.config['omega_reentry'] == ''


def test_snapshots_carry_one_minute_put_and_call_flow(monkeypatch):
    rows = []
    for minute, add in ((t(DAY, '10:00'), 0), (t(DAY, '10:01'), 7), (t(DAY, '10:02'), 0)):
        for k in range(23650, 24400, 50):
            for typ in ('CE', 'PE'):
                rows.append(dict(timestamp=minute, strike=k, option_type=typ, expiry='26NOV',
                                 oi=1000 + (add if typ == 'PE' else 0), bid=1.0, ask=2.0, tradingsymbol=f'N{k}{typ}'))
    rows = [r for r in rows if not (r['timestamp'] == t(DAY, '10:02') and r['strike'] == 24000)]   # incomplete minute
    base = pd.DataFrame([dict(strike=k, type=typ, oi=900) for k in range(23650, 24400, 50) for typ in ('ce', 'pe')])
    monkeypatch.setattr(omega.nna, 'options_frame', lambda day: pd.DataFrame(rows))
    monkeypatch.setattr(omega.core, 'nearest_expiry', lambda expiries, day: '26NOV')
    monkeypatch.setattr(omega, 'prior_inputs', lambda day, expiry: (24000.0, DAY, 'test', base, DAY))
    monkeypatch.setattr(omega.nna, 'spot_bars', lambda day: {})
    monkeypatch.setattr(omega, 'warmup', lambda day: pd.Series(dtype=float))
    data = omega.snapshots(DAY, t(DAY, '15:30'))
    assert data['pc'] == {t(DAY, '10:00'): (1500.0, 1500.0), t(DAY, '10:01'): (1605.0, 1500.0)}
    assert data['alpha'] == {t(DAY, '10:00'): 0.0}
