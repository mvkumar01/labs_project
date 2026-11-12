import sqlite3
from datetime import datetime

from labs.engine import proposer_nifty_scalper_v5_book as v5
from labs.engine import proposer_nifty_tracker as nifty
from labs.engine import proposer_px_tracker as tracker


def test_v5_keeps_v4_entries_and_only_target_and_floor():
    pe = nifty.pe
    assert v5.BOOK.key in tracker.ENTRY_BLOCK_BOOKS
    assert v5.BOOK.key != tracker.NIFTY_V5.key  # The historical key belongs to Nifty Scalper V1.
    params = nifty._params('2026-11-12', v5.BOOK, 0.31)
    assert params.daily_target_pct == 0.025
    assert params.bank_regardless_of_book
    assert params.spot_floor_pts == 46.5
    assert params.early_adverse_pts == 0

    now = datetime(2026, 11, 12, 10)
    print_ = {'regime': 'bearish', 'x5': 'strong_bear', 'x5_conf': 1,
              'x5_asof': now.isoformat()}
    pos = pe.Position('CE', 200, 25_000, 65)
    engine = nifty._TargetFloorEngine(params)
    assert engine.evaluate(now, print_, pos, option_ltp=202, spot=25_050).action == 'HOLD'
    assert engine.evaluate(now, print_, pos, option_ltp=190, spot=24_953).reason == 'spot_floor'
    assert engine.evaluate(now, print_, pos, option_ltp=205, spot=25_010).reason == 'daily_target'


def test_v5_backfill_seeds_verified_history_in_batches(tmp_path, monkeypatch):
    db = tmp_path / 'paper.db'
    monkeypatch.setattr(v5, 'get_conn', lambda: sqlite3.connect(db))

    first = v5.run_backfill(end_date='2026-11-11', limit=20)
    assert len(first['done']) == 20 and first['remaining'] == 7
    second = v5.run_backfill(end_date='2026-11-11', limit=20)
    assert len(second['done']) == 7 and second['remaining'] == 0
    assert not v5.run_backfill(end_date='2026-11-11')['done']

    with sqlite3.connect(db) as conn:
        rows, trades, stats = v5.tab_data(conn)
        assert rows[-1]['trade_date'] == '2026-10-01'
        assert rows[-1]['replay'] == 'pramanaa_depth_ohlc'
        assert rows[0]['trade_date'] == '2026-11-11'
        assert len(trades) == 34 and stats['green_days'] == 27
        assert stats['net_total'] == 850998.85
