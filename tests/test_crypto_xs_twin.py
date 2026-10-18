"""Crypto-only twin of the crypto taker-imbalance paper book: which contracts count as crypto, the engine's
universe mask, the second ledger built alongside the first, its card, tab and wiring."""
from __future__ import annotations

import py_compile
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_crypto_xs import START, SYMBOLS, FakeArchive  # noqa: E402

from labs.engine import crypto_xs_data as data  # noqa: E402
from labs.engine import crypto_xs_engine as engine  # noqa: E402
from labs.engine import crypto_xs_tracker as tracker  # noqa: E402
from labs.engine import crypto_xs_universe as universe  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
TRADFI = SYMBOLS[10:]                                   # C10..C13 play the stock / commodity perpetuals


def test_the_label_snapshot_marks_tradfi_perpetuals():
    labels = universe.load_labels()
    assert labels["BTCUSDT"] == "COIN" and labels["PAXGUSDT"] == "COIN" and labels["SLXUSDT"] == "COIN"
    for s in ("TSLAUSDT", "NVDAUSDT", "SOXLUSDT", "SNXXUSDT", "SPCXUSDT", "DRAMUSDT"):
        assert labels[s] == "EQUITY"
    assert labels["XAUUSDT"] == labels["BZUSDT"] == "COMMODITY"


def test_unlabelled_contracts_are_judged_by_their_weekend_volume():
    hours = pd.date_range("2026-05-01", "2026-06-15 23:00", freq="h", tz="UTC")
    weekend = hours.dayofweek >= 5
    qv = pd.DataFrame({"COINY": 1e6, "STOCKY": np.where(weekend, 5e4, 1e6), "LABELLED": np.where(weekend, 5e4, 1e6)}, index=hours)
    close = pd.DataFrame(1.0, index=hours, columns=qv.columns)
    allowed = universe.crypto_allowed(close, qv, labels={"LABELLED": "COIN"})
    last = allowed.iloc[-1]
    assert bool(last["COINY"]) and not bool(last["STOCKY"]) and bool(last["LABELLED"])     # the label wins
    assert not allowed["COINY"].iloc[0]                                                     # too little history yet


def test_the_mask_leaves_the_rule_unchanged_when_absent_and_narrows_the_ranking_when_present():
    hours = pd.date_range("2026-03-01", "2026-05-10 23:00", freq="h", tz="UTC")
    rng = np.random.RandomState(3)
    cols = [f"S{k:02d}" for k in range(14)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.004, (len(hours), 14)), axis=0)), index=hours, columns=cols)
    qv = pd.DataFrame((np.arange(14) + 1) * 1e6 * (1 + rng.rand(len(hours), 14)), index=hours, columns=cols)
    tb = qv * np.clip(0.5 + rng.normal(0, 0.05, qv.shape), 0.05, 0.95)
    fund = pd.DataFrame(0.0, index=hours, columns=cols)
    a = engine.run(close, fund, qv, tb)
    b = engine.run(close, fund, qv, tb, None)
    assert np.array_equal(a["weights"], b["weights"])
    decisions = a["market"].decisions
    mask = pd.DataFrame(True, index=decisions, columns=cols)
    mask[["S12", "S13"]] = False                          # the two most liquid contracts are not crypto
    c = engine.run(close, fund, qv, tb, mask)
    assert not c["market"].eligible[:, 12:].any() and np.abs(c["weights"][:, 12:]).sum() == 0
    assert c["market"].eligible[-1].sum() == 12 and a["market"].eligible[-1].sum() == 14


@pytest.fixture
def archive(monkeypatch):
    monkeypatch.setattr(data, "DATA_START", START)
    monkeypatch.setattr(data, "WORKERS", 1)
    monkeypatch.setattr(data, "MIN_COVERAGE", 0.9)
    monkeypatch.setattr(tracker, "PAPER_START", "2026-04-20")
    monkeypatch.setattr(tracker, "FIRST_UNSEEN", "2026-05-01")
    monkeypatch.setattr(universe, "load_labels", lambda path=None: {s: ("EQUITY" if s in TRADFI else "COIN") for s in SYMBOLS})
    monkeypatch.setattr(tracker, "CRYPTO_ONLY", tracker.Book("crypto_xs_c", tracker.CRYPTO_ONLY.version,
                                                             tracker.CRYPTO_ONLY.reference_version, True, "2026-05-05"))
    return FakeArchive()


def test_refresh_builds_the_twin_alongside_the_main_book(archive, tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    out = tracker.refresh(datetime(2026, 5, 12, 6, 0, tzinfo=UTC), store=tmp_path, get=archive, connection=conn, log=lambda *_: None)
    twin = out["crypto_only"]
    assert out["through"] == twin["through"] == "2026-05-09" and twin["reconciles"] is None
    main_rows, _, main_st = tracker.tab_data(conn, reference=True)
    rows, held, st = tracker.tab_data(conn, reference=True, book=tracker.CRYPTO_ONLY)
    assert all(r["n_eligible"] == 14 for r in main_rows) and all(r["n_eligible"] == 10 for r in rows)
    assert {h["symbol"] for h in held}.isdisjoint(TRADFI) and len(held) > 0
    assert conn.execute("SELECT COUNT(*) FROM crypto_xs_c_weights WHERE symbol IN (?,?,?,?)", TRADFI).fetchone()[0] == 0
    assert st["crypto_only"] and st["forward_from"] == "2026-05-05" and st["reconcile"].get("na")
    assert st["research"]["days"] + st["seen"]["days"] + st["forward"]["days"] == len([r for r in rows if r["complete"]])
    assert st["forward"]["first"] == "2026-05-05" and st["seen"]["last"] == "2026-05-04"
    assert [r["net"] for r in rows] != [r["net"] for r in main_rows]
    # the account tables exist for both, and the second refresh with nothing new rebuilds neither
    _, _, acc = tracker.tab_data(conn, book=tracker.CRYPTO_ONLY)
    assert acc["account"] and acc["equity"] > 0
    again = tracker.refresh(datetime(2026, 5, 12, 6, 30, tzinfo=UTC), store=tmp_path, get=archive, connection=conn, log=lambda *_: None)
    assert again.get("unchanged") and "crypto_only" not in again

    from labs.services import book_overview_more as more
    card = more._crypto(conn, "2026-05-12", "crypto_xs_c")
    assert card["key"] == "crypto_xs_c" and card["label"].endswith("crypto only") and "only decisions from 19 Oct" in card["note"]
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")))
    rows, held, st = tracker.tab_data(conn, book=tracker.CRYPTO_ONLY)
    html = env.get_template("_crypto_xs_c.html").render(crypto_xs_c_rows=rows, crypto_xs_c_book=held, crypto_xs_c_stats=st)
    assert "crypto only" in html and "its test" in html and held[0]["symbol"] in html
    conn.close()


def test_ui_patch_applies_next_to_the_crypto_wiring_and_is_idempotent(tmp_path):
    for rel in ("labs/ui/routes.py", "templates/live_strategy.html"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{rel}"], capture_output=True, check=True).stdout)
    script = str(ROOT / "scripts" / "patch_crypto_xs_c_ui.py")
    first = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count("0 edits applied") == 2
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"crypto_xs_c": "Crypto taker · crypto only"') == 1 and routes.count("book=CRYPTO_ONLY") == 1
    py_compile.compile(str(tmp_path / "labs/ui/routes.py"), doraise=True)
    import jinja2
    jinja2.Environment().parse((tmp_path / "templates/live_strategy.html").read_text(encoding="utf-8"))
    jinja2.Environment().parse((ROOT / "templates/_crypto_xs_c.html").read_text(encoding="utf-8"))
