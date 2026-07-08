"""Funds-aware strike fallback: ITM200 first; step 50 pts cheaper until the
premium fits available funds (max OTM100); failed funds read proceeds at
ITM200 (broker RMS stays the final gate); nothing affordable -> entry skipped.
"""
import pytest

import live.live_runner as lr


class _Adapter:
    def __init__(self, funds):
        self._funds = funds

    def available_funds(self):
        if isinstance(self._funds, Exception):
            raise self._funds
        return self._funds


PREMIUMS = {200: 250.0, 150: 210.0, 100: 170.0, 50: 135.0,
            0: 105.0, -50: 80.0, -100: 60.0}   # distance -> LTP


def _patch(monkeypatch):
    monkeypatch.setattr(
        lr, "resolve_itm_option",
        lambda adapter, side, trade_date=None, distance=lr.ITM_DISTANCE:
            f"NIFTY_TEST_{side}_{distance}")
    monkeypatch.setattr(
        lr, "_fast_ltp",
        lambda adapter, symbol, **kw: PREMIUMS[int(symbol.rsplit("_", 1)[1])])
    monkeypatch.setattr(lr, "notify_telegram", lambda msg: None)


def test_itm200_when_funds_suffice(monkeypatch):
    _patch(monkeypatch)
    # 250 * 65 * 1.03 = 16,737.5
    sym, price = lr.resolve_affordable_option(_Adapter(20000.0), "CALL", 65)
    assert sym.endswith("_200") and price == 250.0


def test_steps_down_to_affordable_strike(monkeypatch):
    _patch(monkeypatch)
    # 12,000: ITM200 needs 16.7k, ITM150 14.1k, ITM100 needs 11,381 -> fits
    sym, price = lr.resolve_affordable_option(_Adapter(12000.0), "CALL", 65)
    assert sym.endswith("_100") and price == 170.0


def test_funds_read_failure_proceeds_at_itm200(monkeypatch):
    _patch(monkeypatch)
    sym, price = lr.resolve_affordable_option(
        _Adapter(RuntimeError("rms down")), "PUT", 65)
    assert sym.endswith("_200") and price == 250.0


def test_nothing_affordable_skips_entry(monkeypatch):
    _patch(monkeypatch)
    # OTM100 = 60 * 65 * 1.03 = 4,016 — 3,000 cannot cover even that
    with pytest.raises(RuntimeError):
        lr.resolve_affordable_option(_Adapter(3000.0), "CALL", 65)


def test_never_deeper_otm_than_100(monkeypatch):
    _patch(monkeypatch)
    calls = []
    def fake_resolve(adapter, side, trade_date=None, distance=lr.ITM_DISTANCE):
        calls.append(distance)
        return f"NIFTY_TEST_{side}_{distance}"
    monkeypatch.setattr(lr, "resolve_itm_option", fake_resolve)
    with pytest.raises(RuntimeError):
        lr.resolve_affordable_option(_Adapter(3000.0), "CALL", 65)
    assert min(calls) == -100                      # ladder stops at OTM100