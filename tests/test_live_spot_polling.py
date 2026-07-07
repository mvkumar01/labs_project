"""_live_spot: the v2.12 spot-SL reads the broker's live LTP at poll cadence
(~2s) instead of the stale 1-min shared-store snapshot, so the entry-spot stop
reacts promptly. A failed/blank broker read must fall back to the 1-min value
(and never raise), so it can never fabricate or miss a stop by erroring.
"""
import live.live_runner as lr


class _Adapter:
    def __init__(self, spot=None, boom=False):
        self._spot, self._boom = spot, boom

    def get_spot(self):
        if self._boom:
            raise RuntimeError("angel LTP down")
        return self._spot


def test_uses_broker_ltp_when_available(monkeypatch):
    monkeypatch.setattr(lr, "get_latest_spot", lambda: 24000.0)
    assert lr._live_spot(_Adapter(spot=24458.25)) == 24458.25   # broker wins


def test_falls_back_to_1min_when_broker_raises(monkeypatch):
    monkeypatch.setattr(lr, "get_latest_spot", lambda: 24000.0)
    assert lr._live_spot(_Adapter(boom=True)) == 24000.0         # no raise


def test_falls_back_on_blank_or_bad_broker_spot(monkeypatch):
    monkeypatch.setattr(lr, "get_latest_spot", lambda: 24000.0)
    for bad in (None, 0, -1):
        assert lr._live_spot(_Adapter(spot=bad)) == 24000.0


def test_none_when_both_unavailable(monkeypatch):
    monkeypatch.setattr(lr, "get_latest_spot", lambda: None)
    # evaluate_v212_fast_spot HOLDs on None -> no spurious stop.
    assert lr._live_spot(_Adapter(boom=True)) is None
