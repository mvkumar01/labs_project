from datetime import datetime, timedelta, timezone
import sqlite3

import pandas as pd

from live import live_runner, live_service
from labs.engine.alpha_v212_backfill import _override_from_context
from live.engine import champion_decider, champion_inputs, champion_sim
from storage.live_db import init_live_db


IST = timezone(timedelta(hours=5, minutes=30))
DATE = "2026-07-08"


def test_exact_mark_alpha_is_actionable_without_five_minute_delay() -> None:
    now = datetime(2026, 7, 8, 9, 20, 8, tzinfo=IST)
    cutoff = champion_sim.exact_mark_cutoff(DATE, now)

    assert cutoff == pd.Timestamp(now).floor("min")
    assert cutoff.strftime("%H:%M") == "09:20"

    adf = pd.DataFrame({
        "timestamp": pd.to_datetime([f"{DATE} 09:15", f"{DATE} 09:20"]),
        "alpha": [0.0, 40.0],
        "d_pe_sum": [1.0, 1.0],
        "d_ce_sum": [1.0, 1.0],
        "denom": [100.0, 100.0],
        "spot": [24200.0, 24210.0],
    })
    ohlc = champion_sim.OHLC({
        "09:15": (24200.0, 24200.0, 24200.0, 24200.0),
        "09:20": (24210.0, 24210.0, 24210.0, 24210.0),
    })
    _, trades, open_state = champion_sim.simulate(
        adf, {}, {}, ohlc, DATE, False, 10.0, "PC50", "Wed", "STD",
        24000, 24400, close_eod=False, return_state=True,
        entries_until_ts=cutoff,
    )

    assert trades == []
    assert open_state["side"] == "CALL"
    assert pd.Timestamp(open_state["entry_ts"]).strftime("%H:%M") == "09:20"


def test_new_same_side_closed_segment_forces_exit_then_reentry() -> None:
    target = {
        "position": "PUT",
        "entry_spot": 24223.95,
        "entry_rule": "RULE1",
        "n_closed": 3,
        "last_closed_reason": "ENTRY_SPOT_SL",
        "last_closed_event_id": "event-3",
    }

    exit_sig = champion_decider.reconcile_replay_event(
        target, "PUT", closed_count_seen=2
    )
    reentry_sig = champion_decider.reconcile_replay_event(
        target, None, closed_count_seen=3
    )

    assert exit_sig == {
        "action": "EXIT",
        "side": None,
        "reason": "ENTRY_SPOT_SL",
        "rule": None,
    }
    assert reentry_sig == {
        "action": "ENTER",
        "side": "PUT",
        "reason": "champion_rule1",
        "rule": "RULE1",
    }


def test_completed_ohlc_key_advances_once_per_candle(monkeypatch) -> None:
    frame = pd.DataFrame({
        "timestamp": [
            "2026-07-08 09:41:00",
            "2026-07-08 09:42:00",
        ],
        "open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [1, 1],
    })
    monkeypatch.setattr(champion_inputs, "_labs_spot_ohlc", lambda _: frame)

    assert champion_inputs.latest_completed_ohlc_minute(DATE) == (
        "2026-07-08T09:42"
    )


def test_replay_cursor_survives_trade_state_reset() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_live_db(conn)
    state = live_service._default_trade_state("user", "conn")
    state.update({
        "position": "OPEN",
        "side": "PUT",
        "champion_trade_date": DATE,
        "champion_strategy_version": "v2.13",
        "champion_closed_count": 3,
        "champion_last_event_id": "event-3",
    })
    live_service.save_trade_state("user", "conn", state, conn=conn)

    reset = live_service.reset_trade_state("user", "conn", conn=conn)

    assert reset["position"] == "NONE"
    assert reset["champion_trade_date"] == DATE
    assert reset["champion_strategy_version"] == "v2.13"
    assert reset["champion_closed_count"] == 3
    assert reset["champion_last_event_id"] == "event-3"


def test_open_legacy_cursor_is_version_stamped_without_adopting(monkeypatch) -> None:
    saved = []
    monkeypatch.setattr(
        live_runner,
        "_advance_champion_cursor",
        lambda *_a, **kwargs: saved.append(kwargs),
    )
    state = {
        "position": "OPEN",
        "champion_trade_date": DATE,
        "champion_strategy_version": None,
        "champion_closed_count": 2,
        "champion_last_event_id": "event-2",
    }

    live_runner._scope_champion_cursor(
        "user",
        "conn",
        state,
        trade_date=DATE,
        strategy_version="v2.12",
        target_count=3,
        target_event_id="event-3",
    )

    assert saved[0]["count"] == 2
    assert saved[0]["event_id"] == "event-2"
    assert saved[0]["strategy_version"] == "v2.12"


def test_flat_strategy_change_adopts_new_replay_cursor(monkeypatch) -> None:
    saved = []
    monkeypatch.setattr(
        live_runner,
        "_advance_champion_cursor",
        lambda *_a, **kwargs: saved.append(kwargs),
    )
    state = {
        "position": "NONE",
        "champion_trade_date": DATE,
        "champion_strategy_version": "v2.12",
        "champion_closed_count": 2,
        "champion_last_event_id": "event-2",
    }

    live_runner._scope_champion_cursor(
        "user",
        "conn",
        state,
        trade_date=DATE,
        strategy_version="v2.13",
        target_count=4,
        target_event_id="event-4",
    )

    assert saved[0]["count"] == 4
    assert saved[0]["event_id"] == "event-4"
    assert saved[0]["strategy_version"] == "v2.13"


def test_champion_contract_uses_replay_entry_spot(monkeypatch) -> None:
    seen = []

    class Adapter:
        @staticmethod
        def available_funds():
            return 100_000.0

    def resolve(_adapter, _side, _date=None, *, distance, reference_spot=None):
        seen.append((distance, reference_spot))
        return "NIFTY2671424400PE"

    monkeypatch.setattr(live_runner, "resolve_itm_option", resolve)
    monkeypatch.setattr(live_runner, "_fast_ltp", lambda *_a, **_k: 250.0)

    symbol, _ = live_runner.resolve_affordable_option(
        Adapter(), "PUT", 65, trade_date=DATE, reference_spot=24223.95
    )

    assert symbol == "NIFTY2671424400PE"
    assert seen == [(200, 24223.95)]


def test_historical_rebuild_recovers_frozen_context() -> None:
    override = _override_from_context({
        "range_lower": 23950.0,
        "range_upper": 24750.0,
        "tier": "PC400",
        "direction": "DOWN",
        "vix_open": 12.25,
        "previous_session_date": "2026-07-07",
        "prev_close": 24100.0,
        "prev_close_source": "shared_store_verified:2026-07-07",
        "open_spot": 24200.0,
        "open_spot_source": "kite_historical_verified",
        "biggap": False,
    })

    assert override["lower"] == 23950.0
    assert override["upper"] == 24750.0
    assert override["bucket"] == "PC400"
    assert override["vix"] == 12.25


def test_historical_rebuild_preserves_audited_null_vix() -> None:
    override = _override_from_context({
        "range_lower": 23950.0,
        "range_upper": 24750.0,
        "tier": "PC400",
        "direction": "DOWN",
        "vix_open": None,
        "previous_session_date": "2026-06-12",
        "prev_close": 24100.0,
        "prev_close_source": "shared_store_verified:2026-06-12",
        "open_spot": 24000.0,
        "open_spot_source": "kite_historical_verified",
        "biggap": False,
    })

    assert override["vix"] is None
