"""The live Proposer's strike = Pramanaa Sentinel's rule (Vipin, 2026-10-16): the SHALLOWEST liquid ITM
strike with |delta| >= 0.60, walking ATM -/+200 to -/+500. Cases mirror Sentinel's
services/sentinel-bot/test_strike_selection.py; the Black-Scholes pieces are checked on their own."""
from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from live.engine import proposer_strike as pr

TODAY = date(2026, 10, 16)


def chain(strikes, typ="PE", ltp=100.0, oi=5000, expiry="26O22"):
    return pd.DataFrame([{"timestamp": "t", "tradingsymbol": f"SENSEX{expiry}{k}{typ}", "strike": k, "option_type": typ,
                          "expiry": expiry, "ltp": (ltp(k) if callable(ltp) else ltp),
                          "oi": (oi(k) if callable(oi) else oi), "spot": 0} for k in strikes])


@pytest.fixture
def deltas(monkeypatch):
    """Control each candidate's |delta| directly, as Sentinel's test does."""
    table = {}
    monkeypatch.setattr(pr, "implied_vol", lambda price, spot, k, T, typ, r=0.07: 0.1)
    monkeypatch.setattr(pr, "bs_delta", lambda spot, k, T, sigma, typ, r=0.07: table.get(int(k)))
    return table


def pick(ch, side, spot):
    return pr.delta_itm_symbol(ch, side, spot, TODAY)


def test_min_delta_and_range():
    assert pr.MIN_ENTRY_DELTA == 0.60 and (pr.ITM_DISTANCE, pr.MAX_STRIKE_DEPTH) == (200, 500)


def test_picks_the_shallowest_at_or_above_060(deltas):
    deltas.update({78200: -0.54, 78300: -0.58, 78400: -0.62, 78500: -0.70})
    sym, k, typ, info = pick(chain([78200, 78300, 78400, 78500]), "PUT", 78000)
    assert (k, typ, sym) == (78400, "PE", "SENSEX26O2278400PE") and info["depth"] == 400 and info["delta"] == 0.62


def test_expiry_day_shallow_already_high_keeps_atm200(deltas):
    deltas.update({78200: -0.72, 78300: -0.80, 78400: -0.88, 78500: -0.93})
    assert pick(chain([78200, 78300, 78400, 78500]), "PUT", 78000)[1] == 78200


def test_none_reach_060_takes_the_deepest_known(deltas):
    deltas.update({78200: -0.50, 78300: -0.53, 78400: -0.56, 78500: -0.59})
    assert pick(chain([78200, 78300, 78400, 78500]), "PUT", 78000)[1] == 78500


def test_call_side_walks_down(deltas):
    deltas.update({77800: 0.55, 77700: 0.58, 77600: 0.63, 77500: 0.71})
    sym, k, typ, _ = pick(chain([77800, 77700, 77600, 77500], typ="CE"), "CALL", 78000)
    assert (k, typ) == (77600, "CE")


def test_depth_capped_at_500(deltas):
    deltas.update({78200: -0.50, 78300: -0.52, 78400: -0.54, 78500: -0.56, 78600: -0.65})
    assert pick(chain([78200, 78300, 78400, 78500, 78600]), "PUT", 78000)[1] == 78500


def test_illiquid_strikes_are_skipped(deltas):
    deltas.update({78200: -0.55, 78300: -0.61, 78400: -0.66})
    ch = chain([78200, 78300, 78400], ltp=lambda k: 0.0 if k == 78300 else 100.0)
    assert pick(ch, "PUT", 78000)[1] == 78400                      # 78300 has no trade
    ch = chain([78200, 78300, 78400], oi=lambda k: 0 if k == 78300 else 10)
    assert pick(ch, "PUT", 78000)[1] == 78400                      # 78300 has no open interest


def test_nothing_liquid_means_no_entry(deltas):
    sym, k, typ, info = pick(chain([78200, 78300], ltp=0.0), "PUT", 78000)
    assert sym is None and "no liquid strike" in info["reason"]
    assert pick(pd.DataFrame(), "PUT", 78000)[0] is None


def test_delta_never_computable_takes_the_shallowest_liquid(monkeypatch):
    monkeypatch.setattr(pr, "implied_vol", lambda *a, **k: None)
    assert pick(chain([78200, 78300, 78400]), "PUT", 78000)[1] == 78200


def test_atm_is_the_nearest_hundred(deltas):
    deltas.update({74300: -0.55, 74400: -0.57, 74500: -0.59, 74600: -0.61})
    # 16 Oct: SENSEX 74,138 -> ATM 74,100; the PUT walks 74300 .. 74600 (Pramanaa bought the 74600)
    assert pick(chain([74300, 74400, 74500, 74600]), "PUT", 74138.0)[1] == 74600


def test_black_scholes_pieces():
    T = pr.years_to_expiry(date(2026, 10, 22), TODAY)
    assert T == pytest.approx(6 / 365)
    price = pr._bs_price(74138.0, 74600, T, 0.15, "PE")
    assert pr.implied_vol(price, 74138.0, 74600, T, "PE") == pytest.approx(0.15, abs=1e-3)
    assert pr.implied_vol(1.0, 74138.0, 74600, T, "PE") is None             # below intrinsic
    d_atm = pr.bs_delta(74100.0, 74100, T, 0.15, "CE")
    assert 0.5 < d_atm < 0.56                                                 # ATM call, a little over half
    assert pr.bs_delta(74100.0, 74100, T, 0.15, "PE") == pytest.approx(d_atm - 1, abs=1e-4)
    assert pr.years_to_expiry(TODAY, TODAY) == pytest.approx(0.5 / 365)      # expiry day: half a day
    assert pr.years_to_expiry(date(2026, 10, 15), TODAY) == 0.0


def test_expiry_dates():
    assert pr.expiry_date("26O22") == date(2026, 10, 22)
    assert pr.expiry_date("26OCT") == date(2026, 10, 29)                    # monthly: last Thursday
    assert pr.expiry_date("26NOV") == date(2026, 11, 26)


def test_real_prices_pick_a_deeper_put_six_days_out():
    """Prices from a 15% vol surface, six days to expiry: ATM+200 has delta under 0.60, so the rule
    walks deeper, as Sentinel's note says (ATM -/+200 is ~0.52 delta on non-expiry days)."""
    T = 6 / 365
    ch = chain([74300, 74400, 74500, 74600], ltp=lambda k: round(pr._bs_price(74138.0, k, T, 0.15, "PE"), 2))
    sym, k, typ, info = pr.delta_itm_symbol(ch, "PUT", 74138.0, TODAY)
    seen = {c["strike"]: c["delta"] for c in info["candidates"]}
    assert seen[74300] < 0.60 and info["delta"] >= 0.60 and k > 74300
    assert all(seen[s] < 0.60 for s in seen if s < k)                      # it stopped at the first >= 0.60


def test_simulated_books_switch_from_the_next_session(deltas):
    from datetime import datetime
    from labs.engine import proposer_px_tracker as tr
    deltas.update({74300: -0.55, 74400: -0.57, 74500: -0.61, 74600: -0.66})
    snap = chain([74300, 74400, 74500, 74600]).assign(timestamp=pd.Timestamp("2026-10-19 09:22"))
    later = snap.assign(timestamp=pd.Timestamp("2026-10-19 09:30"))
    near = pd.concat([snap, later])
    t = datetime(2026, 10, 19, 9, 22, 25)
    assert tr._entry_strike("2026-10-16", near, t, 74138.0, "PUT") == (74300, "PE")     # booked days: ATM+200
    assert tr._entry_strike("2026-10-19", near, t, 74138.0, "PUT") == (74500, "PE")     # from 19 Oct: delta rule
    assert tr._entry_strike("2026-10-19", near.iloc[0:0], t, 74138.0, "PUT") == (74300, "PE")
    assert tr._entry_strike("2026-10-19", near, t, 74138.0, "PUT", "proposer_dt25_clock") == (74300, "PE")  # shadows
    assert tr._entry_strike("2026-10-19", near, t, 74138.0, "PUT", "proposer_px") == (74500, "PE")


def test_dt35_on_pramanaas_timing_is_a_paper_book_of_the_live_preset():
    from labs.engine import proposer_px_tracker as tr
    from labs.engine import proposer_v3_dt35_clock_book as clock_book
    from live.engine import proposer_engine as pe
    book = clock_book.BOOK
    assert book.key == "proposer_v3_dt35_clock" and book.strategy_version == pe.STRATEGY_VERSION_V3_DT35
    assert book.print_gap_s == tr.PRAMANAA_PRINT_GAP_S == pytest.approx(310.5)
    assert tr.V3_DT35.print_gap_s == 300.0                      # the book that mirrors the live bot keeps five minutes
    assert book.key in tr.DELTA_STRIKE_BOOKS


def test_live_presets_wait_270_s_after_an_exit_and_the_books_follow_from_19_oct():
    from labs.engine import proposer_px_tracker as tr
    from live.engine import proposer_engine as pe
    for v in (pe.STRATEGY_VERSION, pe.STRATEGY_VERSION_PX, pe.STRATEGY_VERSION_V3, pe.STRATEGY_VERSION_V3_DT35):
        assert pe.params_for(v).reentry_cooldown_secs == 270.0
    assert pe.ProposerParams().reentry_cooldown_secs == 300.0          # Pramanaa's engine default
    assert tr.params_on("2026-10-16", tr.V3_DT35).reentry_cooldown_secs == 300.0
    assert tr.params_on("2026-10-19", tr.V3_DT35).reentry_cooldown_secs == 270.0
    assert tr.params_on("2026-10-19", tr.V3_DT35_CLOCK).reentry_cooldown_secs == 270.0
    assert tr.params_on("2026-10-19", tr.PX).reentry_cooldown_secs == 270.0
    for shadow in (tr.DT25, tr.DT25_CLOCK, tr.V3_CLOCK):                # copies of Pramanaa's bot
        assert tr.params_on("2026-10-19", shadow).reentry_cooldown_secs == 300.0
    assert tr.params_on("2026-10-16", tr.V3).daily_target_pct == pe.params_for(pe.STRATEGY_VERSION_V3).daily_target_pct
