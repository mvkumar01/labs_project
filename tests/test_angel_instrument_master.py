"""An order must never wait for Angel's instrument master.

2026-10-13: the first SENSEX entry of the day left 96 s after its trigger. The master is
35 MB and took 72 s to fetch from PythonAnywhere, and the lookup fetched it inline whenever
the file on disk was not today's; every new strike also re-parsed all 150,000 rows.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from live.brokers import angel
from live import proposer_runner as pr

DAY = 86400


def row(symbol, token, name="SENSEX", seg="BFO", lot="20"):
    return {"token": token, "symbol": symbol, "name": name, "expiry": "15OCT2026",
            "strike": "7200000.000000", "lotsize": lot, "instrumenttype": "OPTIDX", "exch_seg": seg}


MASTER = [row("NIFTY13OCT2622000CE", "1", name="NIFTY", seg="NFO", lot="65"),
          row("SENSEX26O1572000CE", "1100", ), row("SENSEX26O1572400CE", "1104"),
          row("BANKEX26O1572000CE", "2200", name="BANKEX")]


@pytest.fixture
def master(tmp_path, monkeypatch):
    f = tmp_path / "angel_instruments.json"
    monkeypatch.setattr(angel, "INSTRUMENT_FILE", f)
    angel._UNDERLYING_ROWS.clear()
    angel._REFRESH_STATE.update(running=False, tried_at=None)
    downloads = []
    done = threading.Event()

    def fake_download():
        downloads.append(time.monotonic())
        f.write_text(json.dumps(MASTER), encoding="utf-8")
        done.set()

    monkeypatch.setattr(angel, "_download_instrument_master", fake_download)

    def write(age_days=0, rows=MASTER):
        f.write_text(json.dumps(rows), encoding="utf-8")
        t = time.time() - age_days * DAY
        os.utime(f, (t, t))

    return type("M", (), {"file": f, "downloads": downloads, "done": done, "write": staticmethod(write)})


def adapter():
    a = angel.AngelAdapter(user_id="u", conn_id="u:angel", creds={})
    a.use_segment("BFO", "SENSEX")
    return a


def test_yesterdays_file_answers_at_once_and_today_is_fetched_on_the_side(master):
    master.write(age_days=1)
    a = adapter()
    seen_before_download = []
    original = angel._download_instrument_master

    def slow_download():
        time.sleep(0.2)
        original()

    angel._download_instrument_master = slow_download
    try:
        started = time.monotonic()
        meta = a._resolve_symbol_meta("SENSEX26O1572000CE")
        seen_before_download.append(len(master.downloads))
        took = time.monotonic() - started
        assert master.done.wait(5)
    finally:
        angel._download_instrument_master = original
    assert meta == {"symbol": "SENSEX26O1572000CE", "token": "1100", "lotsize": 20}
    assert seen_before_download == [0] and took < 0.15     # the lookup did not wait for the fetch
    assert len(master.downloads) == 1


def test_no_file_waits_for_the_download(master):
    a = adapter()
    assert a._resolve_symbol_meta("SENSEX26O1572400CE")["token"] == "1104"
    assert len(master.downloads) == 1


def test_a_file_older_than_the_limit_is_not_trusted(master):
    master.write(age_days=angel.INSTRUMENT_MAX_AGE_DAYS + 1)
    a = adapter()
    assert a._underlying_rows() is None
    a._ensure_instrument_master()
    assert len(master.downloads) == 1


def test_todays_file_is_never_fetched_again(master):
    master.write(age_days=0)
    a = adapter()
    a._resolve_symbol_meta("SENSEX26O1572000CE")
    time.sleep(0.05)
    assert master.downloads == []


def test_new_strikes_are_looked_up_in_memory(master, monkeypatch):
    master.write(age_days=0)
    a = adapter()
    reads = []
    real = angel.AngelAdapter._ensure_instrument_master
    monkeypatch.setattr(angel.AngelAdapter, "_ensure_instrument_master",
                        lambda self: reads.append(1) or real(self))
    a.warm_symbols()
    assert reads == [1]
    assert a._resolve_symbol_meta("SENSEX26O1572000CE")["token"] == "1100"
    assert a._resolve_symbol_meta("SENSEX26O1572400CE")["token"] == "1104"
    b = adapter()                                     # a reconnect builds a new adapter
    assert b._resolve_symbol_meta("SENSEX26O1572400CE")["token"] == "1104"
    assert reads == [1]                               # the file was parsed once in all


def test_kept_rows_hold_only_this_underlying(master):
    master.write(age_days=0)
    a = adapter()
    assert [r["symbol"] for r in a._underlying_rows()] == ["SENSEX26O1572000CE", "SENSEX26O1572400CE"]
    # A symbol of another underlying on the same exchange still resolves, from the whole file.
    assert a._resolve_symbol_meta("BANKEX26O1572000CE")["token"] == "2200"


def test_a_new_master_drops_symbols_resolved_from_the_old_one(master):
    master.write(age_days=1)
    angel._REFRESH_STATE.update(tried_at=time.monotonic())      # no side fetch in this test
    a = adapter()
    assert a._resolve_symbol_meta("SENSEX26O1572000CE")["lotsize"] == 20
    changed = [dict(r, lotsize="25") if r["name"] == "SENSEX" else r for r in MASTER]
    master.write(age_days=0, rows=changed)
    assert a._resolve_symbol_meta("SENSEX26O1572000CE")["lotsize"] == 25


def test_only_one_side_fetch_runs_and_a_failure_is_not_retried_at_once(master, monkeypatch):
    calls = []
    gate = threading.Event()

    def failing():
        calls.append(1)
        gate.wait(2)
        raise OSError("network")

    monkeypatch.setattr(angel, "_download_instrument_master", failing)
    assert angel.refresh_instrument_master_in_background() is True
    assert angel.refresh_instrument_master_in_background() is False     # one is running
    gate.set()
    for _ in range(100):
        if not angel._REFRESH_STATE["running"]:
            break
        time.sleep(0.01)
    assert angel.refresh_instrument_master_in_background() is False     # tried moments ago
    assert calls == [1]


def test_download_swaps_the_file_in_whole(tmp_path, monkeypatch):
    f = tmp_path / "angel_instruments.json"
    monkeypatch.setattr(angel, "INSTRUMENT_FILE", f)
    f.write_text("[]", encoding="utf-8")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return MASTER

    import requests
    monkeypatch.setattr(requests, "get", lambda url, timeout: Response())
    angel._download_instrument_master()
    assert json.loads(f.read_text(encoding="utf-8")) == MASTER
    assert [p.name for p in tmp_path.iterdir()] == ["angel_instruments.json"]


def test_step_clock_names_each_step():
    c = pr.StepClock()
    c.mark("state")
    time.sleep(0.06)
    c.mark("contract")
    assert [n for n, _ in c.steps] == ["state", "contract"]
    assert c.steps[1][1] >= 0.03 and c.total() >= 0.03
    assert c.text().startswith("state=0.0s contract=")
