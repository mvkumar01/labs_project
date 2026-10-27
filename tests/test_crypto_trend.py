"""Crypto trend (BTC/ETH) and taker + trend combined paper books: the rule, the P&L, the combined mix, the
ledgers, the tab and the wiring. Parity with the research (run_btc_benchmarks.py B2_trend_60d 50/50 and
COMBINED_DAILY_NET_RETURNS.csv) is checked outside the suite on the stored panels: 91 days 1 Jun - 30 Aug 2026,
trend within 3e-15 a day, combined within 4e-7 (the research mixed at unrounded weights), months
+7.64 / -5.53 / +19.99% and +4.89 / -7.61 / +9.47%."""
from __future__ import annotations

import py_compile
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import crypto_trend_tracker as ct

ROOT = Path(__file__).resolve().parents[1]


def _panels(days=100, btc_drift=0.0004, eth_drift=-0.0004):
    idx = pd.date_range("2026-03-01 01:00", periods=days * 24, freq="h", tz="UTC")
    rng = np.random.default_rng(7)
    shocks = rng.normal(0, 0.004, size=(len(idx), 2))
    logp = np.cumsum(shocks + np.array([btc_drift, eth_drift]), axis=0)
    close = pd.DataFrame(np.exp(logp) * [60000, 3000], index=idx, columns=list(ct.SYMBOLS))
    funding = pd.DataFrame(0.0, index=idx, columns=list(ct.SYMBOLS))
    funding.loc[funding.index.hour % 8 == 0] = 0.0001
    return close, funding


def test_direction_size_and_weight_follow_the_research_rule():
    close, _ = _panels()
    decisions = close.index[close.index.hour == 0]
    w, direction, scale, vol = ct.trend_weights(close, decisions)
    day = decisions[80]
    assert direction.at[day, "BTCUSDT"] == 1 and direction.at[day, "ETHUSDT"] == -1   # up / down 60 days
    hourly = np.log(close).diff().loc[:day].iloc[-720:]
    want_vol = hourly.std() * np.sqrt(24 * 365)
    assert vol.loc[day].to_numpy() == pytest.approx(want_vol.to_numpy())
    assert scale.loc[day].to_numpy() == pytest.approx(np.minimum(0.30 / want_vol.to_numpy(), 2.0))
    assert w.at[day, "BTCUSDT"] == pytest.approx(0.5 * scale.at[day, "BTCUSDT"])
    assert w.at[day, "ETHUSDT"] == pytest.approx(-0.5 * scale.at[day, "ETHUSDT"])
    assert (w.loc[decisions[:59]] == 0).all().all()          # no 60-day history yet: flat, not NaN


def test_the_size_is_capped_at_two():
    idx = pd.date_range("2026-03-01 01:00", periods=100 * 24, freq="h", tz="UTC")
    close = pd.DataFrame({"BTCUSDT": np.linspace(60000, 60600, len(idx)), "ETHUSDT": np.linspace(3000, 3030, len(idx))},
                         index=idx)                                 # nearly no volatility
    decisions = idx[idx.hour == 0]
    w, _, scale, _ = ct.trend_weights(close, decisions)
    assert scale.iloc[-1].max() == 2.0 and w.iloc[-1].to_numpy() == pytest.approx([1.0, 1.0])


def test_pnl_is_price_minus_funding_minus_cost_on_the_drifted_change():
    close, funding = _panels()
    out = ct.run(close, funding)
    m, w, f = out["market"], out["weights"], out["frame"]
    i = 70
    r = np.nan_to_num(m.forward_return)
    drifted = w[i - 1] * (1 + r[i - 1])
    assert f["gross"].iloc[i] == pytest.approx(float((w[i] * r[i]).sum()))
    assert f["funding"].iloc[i] == pytest.approx(float(-(w[i] * m.forward_funding[i]).sum()))
    assert f["cost"].iloc[i] == pytest.approx(float(np.abs(w[i] - drifted).sum() * 7 / 1e4))
    assert f["net"].iloc[i] == pytest.approx(f["gross"].iloc[i] + f["funding"].iloc[i] - f["cost"].iloc[i])
    assert not f["complete"].iloc[-1]                                # the last hold is still open


def _ledger(monkeypatch, tmp_path):
    close, funding = _panels()
    out = ct.run(close, funding)
    out.update(estimate_from=pd.Timestamp("2026-06-05", tz="UTC"), manifest={"through": "2026-06-08",
                                                                          "funding_through": "2026-06-04"})
    monkeypatch.setattr(ct, "compute", lambda *a, **k: out)
    conn = sqlite3.connect(tmp_path / "labs.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE crypto_xs_daily (decision_date TEXT PRIMARY KEY, complete INTEGER, exposure REAL, "
                 "turnover REAL, gross REAL, funding REAL, cost REAL, net REAL, funding_source TEXT)")
    conn.execute("CREATE TABLE crypto_xs_state (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO crypto_xs_state VALUES ('built_at', 'x')")
    for d in pd.date_range("2026-06-01", "2026-06-07"):
        conn.execute("INSERT INTO crypto_xs_daily VALUES (?,1,1.0,0.1,0.011,-0.001,0.0001,0.0099,'actual')",
                     (d.strftime("%Y-%m-%d"),))
    conn.commit()
    return conn, out


def test_ledgers_and_the_combined_mix(monkeypatch, tmp_path):
    conn, out = _ledger(monkeypatch, tmp_path)
    res = ct.rebuild(conn)
    assert res["trend_days"] > 0 and res["combo_days"] == 7
    t = dict(conn.execute("SELECT * FROM crypto_xs_trend_daily WHERE decision_date='2026-06-03'").fetchone())
    c = dict(conn.execute("SELECT * FROM crypto_xs_combo_daily WHERE decision_date='2026-06-03'").fetchone())
    assert c["net"] == pytest.approx(0.5555 * 0.0099 + 0.4445 * t["net"])
    assert c["taker_net"] == pytest.approx(0.0099) and c["trend_net"] == pytest.approx(t["net"])
    assert t["funding_source"] == "actual"
    late = dict(conn.execute("SELECT * FROM crypto_xs_trend_daily WHERE decision_date='2026-06-06'").fetchone())
    assert late["funding_source"] == "estimated"
    assert dict(conn.execute("SELECT * FROM crypto_xs_combo_daily WHERE decision_date='2026-06-06'").fetchone())[
        "funding_source"] == "estimated"
    # a day the taker book has not finished is not a combined day
    conn.execute("UPDATE crypto_xs_daily SET complete=0, net=NULL WHERE decision_date='2026-06-07'")
    ct.rebuild(conn)
    assert conn.execute("SELECT complete, net FROM crypto_xs_combo_daily WHERE decision_date='2026-06-07'").fetchone()[0] == 0


def test_refresh_rebuilds_only_when_the_taker_book_or_panels_move(monkeypatch, tmp_path):
    conn, _ = _ledger(monkeypatch, tmp_path)
    calls = []
    real = ct.rebuild
    monkeypatch.setattr(ct, "rebuild", lambda c, *a, **k: calls.append(1) or real(c, *a, **k))
    monkeypatch.setattr(ct.data, "load_manifest", lambda store=None: {"through": "2026-06-08"})
    ct.refresh(connection=conn)
    ct.refresh(connection=conn)
    assert len(calls) == 1
    conn.execute("UPDATE crypto_xs_state SET v='y' WHERE k='built_at'")
    ct.refresh(connection=conn)
    assert len(calls) == 2


def test_both_tabs_render(monkeypatch, tmp_path):
    conn, _ = _ledger(monkeypatch, tmp_path)
    ct.rebuild(conn)
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.Undefined)
    for key, title in (("crypto_xs_trend", "Crypto trend, BTC/ETH"), ("crypto_xs_combo", "Crypto taker + trend combined")):
        rows, stats = ct.tab_data(conn, key)
        assert rows and stats["key"] == key and stats["reconcile"]["want"] == ct.REFERENCE[key]
        html = env.get_template("_crypto_trend.html").render(ct_rows=rows, ct_stats=stats)
        assert title in html and "Paper equity" in html


def test_registered_everywhere():
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import SOURCES
    from labs.ui.routes import LIVE_TABS
    for key in ("crypto_xs_trend", "crypto_xs_combo"):
        assert key in LIVE_TABS and key in MORE_BOOKS and SOURCES[key].table == f"{key}_daily"
    assert "run_crypto_trend_live(now)" in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")


def test_ui_patch_is_idempotent_and_compiles(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
             "labs/services/book_overview_more.py", "labs/services/strategy_report.py")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_crypto_trend_ui.py"), str(tmp_path)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.count("0 edits applied") == 5, run.stdout + run.stderr
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py",
                "labs/services/strategy_report.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)


def test_never_touches_an_exchange_order_api():
    import ast
    tree = ast.parse((ROOT / "labs" / "engine" / "crypto_trend_tracker.py").read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert names <= {"__future__", "sqlite3", "threading", "datetime", "pathlib", "numpy", "pandas", "json",
                     "labs.engine", "storage.db", "labs.engine.crypto_xs_tracker"}, names
