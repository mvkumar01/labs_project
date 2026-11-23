"""Nifty Omega late EMA entry with the alpha-growth filter (Vipin 2026-11-23; alphaIMB research
2026-11-14_alpha_velocity_writers README 72-73, late_ema_growth_drop.py sig86G(d, 30, 8, 80))."""
from labs.engine import nifty_omega_book as book
from live import live_executor
from live.engine import nifty_omega as omega
from tests.test_nifty_omega_refinements import DAY, FRIDAY, session, t

END = '15:15'


def first(alpha, ema, day=DAY, late=True, **kw):
    data = session(day, {t(day, k): v for k, v in alpha.items()}, ema={t(day, k): v for k, v in ema.items()}, **kw)
    return omega.first_signal(data, day, t(day, END), late=late)


def test_versions():
    assert omega.ABI.startswith('nifty-omega-v2-lateema30-g8-cap80-snapoi15-60-fresh1') and omega.LATE_START == '2026-11-24'
    assert (omega.LATE_WAIT, omega.LATE_GROWTH, omega.LATE_CAP) == (30, 8.0, 80.0)
    assert live_executor.expected_decision_abi(omega.KEY) == omega.ABI == book.BOOK.version


def test_blocked_cross_enters_when_the_ema_agrees_with_enough_growth():
    # 9 Jul 2026 shape: CALL cross 09:20 (40.8) against the EMA; EMA agrees at 09:40 with alpha 51.3 (growth 10.5)
    alpha = {'09:20': 40.8, '09:25': 45.0, '09:30': 47.0, '09:35': 49.0, '09:40': 51.3, '09:45': 52.0}
    ema = {k: -1.0 for k in ('09:20', '09:25', '09:30', '09:35')}
    assert first(alpha, ema)[:2] == (t(DAY, '09:40'), 1)
    assert first(alpha, ema, late=False) is None                  # v2: skipped
    # PUT mirrored (20 Jul 2026 shape): -40.6 at 09:20, EMA agrees 09:25 at -59.5
    assert first({'09:20': -40.6, '09:25': -59.5}, {'09:20': 1.0, '09:25': -1.0})[:2] == (t(DAY, '09:25'), -1)


def test_growth_and_cap_drop_the_cross_for_good():
    ema = {'09:20': -1.0, '09:25': -1.0}
    base = {'09:20': 40.0, '09:25': 44.0}
    assert first({**base, '09:30': 47.9, '09:35': 60.0}, ema) is None          # growth 7.9 at the first EMA mark: dropped
    assert first({**base, '09:30': 48.0, '09:35': 60.0}, ema)[0] == t(DAY, '09:30')
    assert first({**base, '09:30': 80.0}, ema)[0] == t(DAY, '09:30')            # cap is inclusive
    assert first({**base, '09:30': 81.0, '09:35': 70.0}, ema) is None           # above the cap: dropped


def test_cancel_back_inside_or_after_30_minutes():
    ema = {k: -1.0 for k in ('09:20', '09:25', '09:30', '09:35', '09:40', '09:45', '09:50', '09:55')}
    a = {'09:20': 40.0, '09:25': 30.0, '09:30': 29.0, '09:35': 29.5}            # 30 is not > 30: cancelled
    assert first(a, {'09:20': -1.0, '09:25': 1.0}) is None
    hold = {k: 45.0 for k in ('09:20', '09:25', '09:30', '09:35', '09:40', '09:45', '09:50')}
    assert first({**hold, '09:20': 36.0, '09:55': 50.0}, {**ema, '09:50': 1.0})[0] == t(DAY, '09:50')   # 30 min: ok
    assert first({**hold, '09:20': 36.0, '09:55': 50.0}, ema) is None                                   # 35 min: gone


def test_extreme_marks_are_skipped_and_a_new_blocked_cross_replaces_the_old():
    a = {'09:20': 40.0, '09:25': 250.0, '09:30': 50.0}
    assert first(a, {'09:20': -1.0})[0] == t(DAY, '09:30')                      # 09:25 |alpha| > 200 skipped
    # pending CALL 09:20; PUT cross 09:30 also EMA-blocked replaces it; EMA bearish at 09:35 confirms the PUT
    a = {'09:20': 40.0, '09:25': 42.0, '09:30': -40.0, '09:35': -50.0}
    assert first(a, {'09:20': -1.0, '09:25': -1.0, '09:30': 1.0, '09:35': -1.0})[:2] == (t(DAY, '09:35'), -1)


def test_v2_signal_first_wins_and_friday_cut_applies():
    a = {'09:20': 40.0, '09:25': 20.0, '09:30': 35.0}                           # v2 CALL 09:30 (pending cancelled 09:25)
    assert first(a, {'09:20': -1.0})[0] == t(DAY, '09:30')
    a = {'11:50': 40.0, '11:55': 45.0, '12:00': 50.0}
    assert first(a, {'11:50': -1.0, '11:55': -1.0}, day=FRIDAY) is None        # confirmed on Friday 12:00: no entry


def test_retest_block_keeps_the_cross_pending():
    # spot flat at 24000 (drift 0): the day's high is within 10 points, so every CALL mark is retest-blocked
    a = {'10:20': 40.0, '10:25': 50.0, '10:30': 55.0}
    assert first(a, {'10:20': -1.0}, drift=0.0) is None
    data = session(DAY, {t(DAY, k): v for k, v in a.items()}, ema={t(DAY, '10:20'): -1.0}, drift=0.0)
    calls = []
    real = omega.retest_blocked
    try:
        omega.retest_blocked = lambda d, day, mark, side: (calls.append(mark) or mark == t(DAY, '10:25'))
        assert omega.first_signal(data, DAY, t(DAY, END), late=True)[0] == t(DAY, '10:30')
    finally:
        omega.retest_blocked = real
    assert calls[:2] == [t(DAY, '10:25'), t(DAY, '10:30')]


def test_fresh_entry_after_a_snap_does_not_use_pending_crosses():
    data = session(DAY, {t(DAY, '10:20'): 40.0, t(DAY, '10:25'): 50.0}, ema={t(DAY, '10:20'): -1.0})
    assert omega.fresh_signal(data, DAY, t(DAY, '10:00'), t(DAY, END)) is None
