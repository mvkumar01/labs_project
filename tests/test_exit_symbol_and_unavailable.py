"""Review-fix regressions (2026-07-08):

1. kite_symbol_for — exits price current_symbol from the ANGEL book
   (NIFTY14JUL2624400PE); Kite KeyError'd on it at all 6 live exits. The
   mapper resolves the Kite tradingsymbol with the same strike/type/expiry
   DATE from the collector chain, pure string/CSV — no broker API.
2. champion_target returns UNAVAILABLE (never None) on transient input
   failures, so reconcile HOLDs instead of flattening a healthy position.
"""
import pytest
import pandas as pd

import live.live_runner as lr
from live.engine import champion_decider


# ── kite_symbol_for ───────────────────────────────────────────────────────
def _chain_csv(tmp_path, trade_date, rows):
    day = tmp_path / trade_date
    day.mkdir(parents=True)
    body = "timestamp,underlying,tradingsymbol,strike,option_type,expiry,ltp\n"
    for sym, strike, otype in rows:
        body += f"2026-07-08T09:30:00,NIFTY,{sym},{strike},{otype},X,100\n"
    (day / "NIFTY_options_1min.csv").write_text(body, encoding="utf-8")
    return tmp_path


def test_maps_angel_weekly_symbol_via_chain(tmp_path, monkeypatch):
    trade_date = "2026-07-08"
    monkeypatch.setattr(
        lr, "SHARED_LIVE_DIR",
        _chain_csv(tmp_path, trade_date, [
            ("NIFTY2671424400PE", 24400, "PE"),     # 14 Jul 2026 weekly
            ("NIFTY2671424400CE", 24400, "CE"),
            ("NIFTY26JUL24400PE", 24400, "PE"),     # 28 Jul monthly (decoy)
        ]))
    lr._kite_symbol_cache.clear()
    assert lr.kite_symbol_for("NIFTY14JUL2624400PE", trade_date) == "NIFTY2671424400PE"
    # cached second call (no CSV re-read): poison the dir and expect the hit
    monkeypatch.setattr(lr, "SHARED_LIVE_DIR", tmp_path / "missing")
    assert lr.kite_symbol_for("NIFTY14JUL2624400PE", trade_date) == "NIFTY2671424400PE"


def test_kite_format_and_unknown_pass_through(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "SHARED_LIVE_DIR", tmp_path / "none")
    assert lr.kite_symbol_for("NIFTY2671424400PE") == "NIFTY2671424400PE"
    assert lr.kite_symbol_for("GARBAGE") == "GARBAGE"
    # Angel-format but chain missing -> unchanged (raise happens in _fast_ltp)
    lr._kite_symbol_cache.clear()
    assert lr.kite_symbol_for("NIFTY14JUL2624400PE", "2026-01-01") == "NIFTY14JUL2624400PE"


def test_fast_ltp_prices_angel_symbol_through_mapping(monkeypatch):
    quotes = {"NIFTY2671424400PE": 245.3}
    monkeypatch.setattr(lr, "get_kite_ltp", lambda s: quotes.get(s))
    monkeypatch.setattr(lr, "kite_symbol_for",
                        lambda s, trade_date=None: "NIFTY2671424400PE")
    assert lr._fast_ltp(None, "NIFTY14JUL2624400PE") == 245.3


def test_fast_ltp_still_never_touches_angel(monkeypatch):
    class AngelLike:
        @staticmethod
        def get_ltp(_s):
            raise AssertionError("Angel market data must not be called")

    monkeypatch.setattr(lr, "get_kite_ltp", lambda s: None)
    monkeypatch.setattr(lr, "kite_symbol_for", lambda s, trade_date=None: s)
    with pytest.raises(RuntimeError, match="no Kite LTP"):
        lr._fast_ltp(AngelLike(), "NIFTY14JUL2624400PE")


# ── champion_target UNAVAILABLE on transient failure ─────────────────────
def test_transient_build_failure_is_unavailable_not_flat(monkeypatch):
    monkeypatch.setattr(champion_decider, "_resolve_day",
                        lambda td, ov: {"bucket": "PC400", "direction": "DOWN",
                                        "lower": 24000.0, "upper": 24500.0,
                                        "vix": 12.0, "biggap": False})
    monkeypatch.setattr(champion_decider.champion_inputs, "ohlc_by_minute",
                        lambda td, extra_minutes=None: {})
    class _Ctx:
        direction = "DOWN"; vix_open = 12.0; sgap = -100.0
        use_trail = False; weekday = "Wed"; regime = "TRAIL"
    monkeypatch.setattr(champion_decider.champion_inputs, "resolve_day_context",
                        lambda *a, **k: _Ctx())
    monkeypatch.setattr(champion_decider.champion_inputs, "alpha_source",
                        lambda *a, **k: ("regime", True))
    def boom(*a, **k):
        raise OSError("csv mid-write")
    monkeypatch.setattr(champion_decider.champion_inputs, "build_sim_inputs", boom)

    target = champion_decider.champion_target("2026-07-08")
    assert target is not None and target["position"] == "UNAVAILABLE"
    # An open position must HOLD, not flatten, on an unavailable replay.
    sig = champion_decider.reconcile_replay_event(target, "PUT", closed_count_seen=0)
    assert sig["action"] == "HOLD"


def test_champion_target_forwards_v211b_decision_gate(monkeypatch):
    monkeypatch.setattr(champion_decider, "_resolve_day",
                        lambda td, ov: {"bucket": "PC50", "direction": "UP",
                                        "lower": 24000.0, "upper": 24500.0,
                                        "vix": 12.0, "biggap": False})
    monkeypatch.setattr(champion_decider.champion_inputs, "ohlc_by_minute",
                        lambda td, extra_minutes=None: {})

    class _Ctx:
        direction = "UP"; vix_open = 12.0; sgap = 0.0
        use_trail = False; weekday = "Wed"; regime = "TRAIL"

    monkeypatch.setattr(champion_decider.champion_inputs, "resolve_day_context",
                        lambda *a, **k: _Ctx())
    monkeypatch.setattr(champion_decider.champion_inputs, "alpha_source",
                        lambda *a, **k: ("regime", True))
    adf = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-07-08 09:15", "2026-07-08 09:20"]),
        "alpha": [0.0, 40.0],
    })
    monkeypatch.setattr(champion_decider.champion_inputs, "build_sim_inputs",
                        lambda *a, **k: (None, adf, {}, {}))
    captured = {}

    def fake_simulate(*args, **kwargs):
        captured.update(kwargs)
        return 0.0, [], None

    monkeypatch.setattr(champion_decider.champion_sim, "simulate", fake_simulate)

    target = champion_decider.champion_target(
        "2026-07-08", suppress_pc50_call_entries=True)

    assert target["position"] == "FLAT"
    assert captured["suppress_pc50_call_entries"] is True
