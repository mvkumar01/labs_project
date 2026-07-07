"""The v2.12 spot-SL sources its ~2s spot from Kite (Zerodha data feed), never
from the Angel execution broker (2026-07-07 Angel rate-limit incident). On any
Kite failure it falls back to the 1-min shared-store snapshot, and never raises.
"""
import live.live_runner as lr


class _Kite:
    def __init__(self, last=None, boom=False):
        self._last, self._boom = last, boom

    def ltp(self, key):
        if self._boom:
            raise RuntimeError("kite token expired")
        return {"NSE:NIFTY 50": {"last_price": self._last}}


def _patch_kite(monkeypatch, kite):
    import auth.session_manager as sm
    monkeypatch.setattr(sm, "get_kite", lambda *a, **k: kite)


def test_kite_spot_parsed(monkeypatch):
    _patch_kite(monkeypatch, _Kite(last=24458.25))
    assert lr.get_kite_spot() == 24458.25


def test_kite_spot_none_on_failure(monkeypatch):
    _patch_kite(monkeypatch, _Kite(boom=True))
    assert lr.get_kite_spot() is None            # never raises


def test_fast_spot_prefers_kite(monkeypatch):
    monkeypatch.setattr(lr, "get_kite_spot", lambda: 24460.0)
    monkeypatch.setattr(lr, "get_latest_spot", lambda: 24000.0)
    assert lr._fast_spot() == 24460.0            # kite wins


def test_fast_spot_falls_back_to_1min(monkeypatch):
    monkeypatch.setattr(lr, "get_latest_spot", lambda: 24000.0)
    for bad in (None, 0, -1):
        monkeypatch.setattr(lr, "get_kite_spot", lambda bad=bad: bad)
        assert lr._fast_spot() == 24000.0        # CSV fallback

    monkeypatch.setattr(lr, "get_kite_spot", lambda: None)
    monkeypatch.setattr(lr, "get_latest_spot", lambda: None)
    assert lr._fast_spot() is None               # both down -> HOLD upstream
