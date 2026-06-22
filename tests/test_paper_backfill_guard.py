import json
import sqlite3

import pytest

from labs.engine import paper_backfill
from labs.engine import paper_strategy_tracker


def _ranges(tmp_path):
    path = tmp_path / "ranges.json"
    path.write_text(json.dumps({"2026-06-01": {}, "2026-06-02": {}}))
    return path


def test_preflight_failure_publishes_nothing(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_run(date, override, *, persist=True, require_all_variants=False, **_kwargs):
        calls.append((date, persist, require_all_variants))
        if date == "2026-06-02":
            raise paper_strategy_tracker.ReplayInputError("missing archive")
        return {"trade_date": date, "status": "traded", "n_trades": 1, "net_rs": 10}

    monkeypatch.setattr(paper_strategy_tracker, "run_day", fake_run)

    with pytest.raises(paper_strategy_tracker.ReplayInputError):
        paper_backfill.backfill(str(_ranges(tmp_path)))

    assert calls == [
        ("2026-06-01", False, True),
        ("2026-06-02", False, True),
    ]


def test_benchmark_mismatch_publishes_nothing(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_run(date, override, *, persist=True, require_all_variants=False, **_kwargs):
        calls.append((date, persist, require_all_variants))
        return {"trade_date": date, "status": "traded", "n_trades": 1, "net_rs": 10}

    monkeypatch.setattr(paper_strategy_tracker, "run_day", fake_run)

    with pytest.raises(RuntimeError, match="Preflight net mismatch"):
        paper_backfill.backfill(str(_ranges(tmp_path)), expected_net=99, expected_trades=2)

    assert all(persist is False for _, persist, _ in calls)


def test_publish_failure_rolls_back_every_date(tmp_path, monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE marker (trade_date TEXT)")
    monkeypatch.setattr("storage.db.get_conn", lambda: conn)

    def fake_run(date, override, *, persist=True, connection=None, **_kwargs):
        if not persist:
            return {"trade_date": date, "status": "traded", "n_trades": 1, "net_rs": 10}
        connection.execute("INSERT INTO marker VALUES (?)", (date,))
        if date == "2026-06-02":
            raise RuntimeError("simulated write failure")
        return {"trade_date": date, "status": "traded", "n_trades": 1, "net_rs": 10}

    monkeypatch.setattr(paper_strategy_tracker, "run_day", fake_run)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        paper_backfill.backfill(str(_ranges(tmp_path)))

    assert conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0
