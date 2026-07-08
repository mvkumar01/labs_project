"""Shared test fixtures.

The live/labs engines memoize file-derived inputs in module-level caches
(mtime-keyed; see champion_inputs). Tests monkeypatch data dirs and seams, so
stale cross-test cache entries would leak state between tests — clear them
around every test.
"""
import pytest


@pytest.fixture(autouse=True)
def _clear_engine_caches():
    def _clear():
        try:
            from live.engine import champion_inputs as ci
            for cache in (
                ci._LABS_OHLC_CACHE,
                ci._LATEST_MINUTE_CACHE,
                ci._OHLC_BY_MINUTE_CACHE,
                ci._VERIFIED_OPEN_CACHE,
                ci._PREV_CLOSE_CACHE,
            ):
                cache.clear()
        except Exception:
            pass
        try:
            import live.live_runner as lr
            lr._kite_symbol_cache.clear()
        except Exception:
            pass

    _clear()
    yield
    _clear()
