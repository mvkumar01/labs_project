"""mtime-keyed input caches (2026-07-08 review): the runner polls
latest_completed_ohlc_minute every ~2s and champion_target rebuilds inputs per
minute; unchanged files must cost a stat(), not a parse, and a changed file
must invalidate immediately (a stale minute key would delay the event clock).
"""
import pandas as pd

from live.engine import champion_inputs as ci


def _write_csv(path, rows):
    body = "timestamp,open,high,low,close,volume\n"
    for ts, o, h, l, c in rows:
        body += f"{ts},{o},{h},{l},{c},0\n"
    path.write_text(body, encoding="utf-8")


def _reset_caches():
    ci._LABS_OHLC_CACHE.clear()
    ci._LATEST_MINUTE_CACHE.clear()
    ci._OHLC_BY_MINUTE_CACHE.clear()


def test_unchanged_csv_is_parsed_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ci, "ARCHIVE_DIR", tmp_path / "arch")
    _reset_caches()
    date = "2026-07-08"
    csv = tmp_path / f"{date}_{ci.SYMBOL}_spot_1min.csv"
    _write_csv(csv, [(f"{date} 09:15:00", 1, 2, 1, 2),
                     (f"{date} 09:16:00", 2, 3, 2, 3)])

    reads = {"n": 0}
    real_read = pd.read_csv
    monkeypatch.setattr(ci.pd, "read_csv",
                        lambda *a, **k: reads.__setitem__("n", reads["n"] + 1)
                        or real_read(*a, **k))

    assert ci.latest_completed_ohlc_minute(date) == f"{date}T09:16"
    for _ in range(10):                       # 10 more polls, same file
        assert ci.latest_completed_ohlc_minute(date) == f"{date}T09:16"
    assert reads["n"] == 1                    # one parse total


def test_changed_csv_invalidates_and_key_advances(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ci, "ARCHIVE_DIR", tmp_path / "arch")
    _reset_caches()
    date = "2026-07-08"
    csv = tmp_path / f"{date}_{ci.SYMBOL}_spot_1min.csv"
    _write_csv(csv, [(f"{date} 09:15:00", 1, 2, 1, 2)])
    assert ci.latest_completed_ohlc_minute(date) == f"{date}T09:15"

    _write_csv(csv, [(f"{date} 09:15:00", 1, 2, 1, 2),
                     (f"{date} 09:16:00", 2, 3, 2, 3)])   # collector wrote
    assert ci.latest_completed_ohlc_minute(date) == f"{date}T09:16"


def test_ohlc_by_minute_uses_cache_and_refreshes(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ci, "ARCHIVE_DIR", tmp_path / "arch")
    monkeypatch.setattr(ci, "ALPHA_DATA_DIR", tmp_path / "no_alpha")
    monkeypatch.setattr(ci, "SHARED_LIVE_DIR", tmp_path / "no_shared")
    _reset_caches()
    date = "2026-07-08"
    csv = tmp_path / f"{date}_{ci.SYMBOL}_spot_1min.csv"
    _write_csv(csv, [(f"{date} 09:15:00", 10, 11, 9, 10)])

    first = ci.ohlc_by_minute(date)
    assert first["09:15"] == (10.0, 11.0, 9.0, 10.0)
    assert ci.ohlc_by_minute(date) is first          # cache hit, same object

    _write_csv(csv, [(f"{date} 09:15:00", 10, 11, 9, 10),
                     (f"{date} 09:16:00", 10, 12, 10, 11)])
    second = ci.ohlc_by_minute(date)
    assert second is not first and "09:16" in second  # invalidated on write
