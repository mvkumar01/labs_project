"""Alpha-CPR tracker: lot sizing, schema, wiring and paper-only isolation."""
from __future__ import annotations

import inspect
import sqlite3

from labs.engine.alpha_cpr_tracker import (
    CPR_MIN_DIST, LOT_SIZE, MAX_LOTS, RISK_BUDGET, SIZING_DELTA,
    STRATEGY_VERSION, _ensure_tables, lots_for)


# ── risk-normalised sizing ───────────────────────────────────────────────────
def test_anchor_stop_gets_the_reference_size():
    """50-pt stop x 5 lots x Rs 39 == the Rs 9,750 budget."""
    assert RISK_BUDGET == 50.0 * 5 * SIZING_DELTA * LOT_SIZE
    assert lots_for(50.0) == 5


def test_tighter_stop_takes_more_lots_wider_takes_fewer():
    assert lots_for(25.0) == 10
    assert lots_for(100.0) == 2
    assert lots_for(20.0) == 12


def test_lots_are_clamped_both_ends():
    assert lots_for(1.0) == MAX_LOTS          # would want 250
    assert lots_for(10_000.0) == 1            # would want ~0.03


def test_missing_stop_falls_back_to_cap_not_a_crash():
    for bad in (None, 0, -5, "nonsense"):
        assert lots_for(bad) == MAX_LOTS


def test_risk_is_near_constant_across_the_unclamped_band():
    """Between the clamps, realised risk should sit close to the budget."""
    for d in (20.0, 30.0, 40.0, 50.0, 65.0, 80.0, 100.0):
        risk = d * lots_for(d) * SIZING_DELTA * LOT_SIZE
        assert abs(risk - RISK_BUDGET) / RISK_BUDGET < 0.35, (d, risk)


# ── schema ───────────────────────────────────────────────────────────────────
def test_tables_carry_the_columns_the_live_page_selects():
    conn = sqlite3.connect(":memory:")
    _ensure_tables(conn)
    daily = {r[1] for r in conn.execute("PRAGMA table_info(alpha_cpr_daily)")}
    # exactly what labs/ui/routes.py selects for an overlay tab
    for col in ("trade_date", "status", "tier", "gap_dir", "expiry_code",
                "n_segments", "priced_segments", "unavailable_segments",
                "spot_pnl_pts", "gross_rs", "charges_rs", "net_rs",
                "strategy_version", "updated_at"):
        assert col in daily, col
    trades = {r[1] for r in conn.execute("PRAGMA table_info(alpha_cpr_trades)")}
    for col in ("lots", "cpr_sl", "cpr_tp", "delta_spot_sl", "risk_rs"):
        assert col in trades, col
    conn.close()


def test_strategy_version_pins_the_configuration():
    for token in ("mindist20", "risk9750", "cap15"):
        assert token in STRATEGY_VERSION, token
    assert CPR_MIN_DIST == 20.0 and MAX_LOTS == 15


# ── wiring ───────────────────────────────────────────────────────────────────
def test_registered_as_a_live_tab():
    import labs.ui.routes as routes
    src = inspect.getsource(routes.live_strategy)
    assert '"alpha_cpr"' in src
    assert "alpha_cpr" in inspect.getsource(routes)


def test_backfill_endpoint_exists():
    import labs.ui.routes as routes
    assert hasattr(routes, "alpha_cpr_backfill")


def test_daily_loops_include_the_book():
    for mod in ("pa_paper_tracker.py", "pa_paper_tracker_loop.py"):
        from pathlib import Path
        src = Path(__file__).resolve().parents[1].joinpath(mod).read_text(
            encoding="utf-8")
        assert "alpha_cpr" in src, mod


def test_backfill_shares_the_champion_ranges_of_the_other_books():
    """Entries must come off identical ranges or the tabs aren't comparable."""
    import labs.engine.alpha_cpr_backfill as bf
    assert "_historical_ranges" in inspect.getsource(bf)
    from labs.engine.alpha_v213_backfill import _historical_ranges
    assert bf._historical_ranges is _historical_ranges


# ── paper-only ───────────────────────────────────────────────────────────────
def test_tracker_places_no_orders():
    import labs.engine.alpha_cpr_tracker as t
    src = inspect.getsource(t)
    for forbidden in ("place_order", "kite.", "live_executor", "place_idempotent"):
        assert forbidden not in src, forbidden
