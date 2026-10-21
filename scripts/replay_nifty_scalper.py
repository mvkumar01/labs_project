"""Compare the real DRY_RUN connection path with the NIFTY paper book on PA archives.

Uses an isolated database and fake broker. Market samples follow the paper book's
documented OHLC price model; they are not recorded ticks or evidence of live fills.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from labs.engine import proposer_nifty_tracker as nt, proposer_px_tracker as tr
from live import live_executor as ex, live_runner as lr, live_service as svc, proposer_runner as pr
from live.engine import proposer_engine as pe, proposer_strike as ps
from storage import live_db
from tests.test_proposer_live import FakeAdapter, FakeFeed


def replay(day: str, root: Path) -> dict:
    """Walk recorded minutes using the reference book's dense OHLC path; compare within one minute."""
    user, connection = "replay", "replay:zerodha"
    dbpath = root / f"replay-{day}.db"
    if dbpath.exists():
        # A fresh, separate filename keeps previous validation runs inspectable.
        import uuid
        dbpath = root / f"replay-{day}-{uuid.uuid4().hex[:8]}.db"
    with ExitStack() as stack:
        def replace(obj, key, value):
            stack.enter_context(patch.object(obj, key, value))
        for obj in (tr, nt):
            replace(obj, "ARCHIVE_DIR", root / "spots/archive")
            replace(obj, "DATA_DIR", root / "spots/live")
            replace(obj, "SHARED_LIVE_DIR", root / "shared/live")
            replace(obj, "SHARED_ARCHIVE_DIR", root / "shared/archive")
        replace(live_db, "LIVE_DB_PATH", dbpath)
        live_db.init_live_db()
        pr.ensure_schema()
        svc.upsert_connection(user, connection, broker="zerodha", account_label="replay",
                              account_ref="zerodha:T", status="connected")
        for k, v in (("strategy_version", pe.STRATEGY_VERSION_NIFTY_SCALPER_V1),
                     ("decision_engine", "proposer"), ("lots", "1"), ("kill_switch", "0"),
                     ("daily_loss_cap", "99999999")):
            svc.set_config(user, connection, k, v)
        ex.set_mode(user, connection, ex.Mode.DRY_RUN)
        prints = []
        original = tr._prints
        def capture(*args, **kw):
            result = original(*args, **kw)
            prints.extend(result[0])
            return result
        with patch.object(tr, "_prints", capture):
            paper = nt.simulate_day(day, book=tr.NIFTY_V5)
        sbars, nbars = tr.spot_ohlc(day), nt.nifty_bars(day)
        sbars = [b for b in sbars if b[0].time() >= tr.SESSION_OPEN]
        nbars = [b for b in nbars if b[0].time() >= tr.SESSION_OPEN]
        s_at = {b[0]: b for b in sbars}
        ratio = nbars[0][1] / sbars[0][1]
        frame = nt.nifty_frame(day)
        code = ps.nifty_expiry_code(frame.expiry.unique(), datetime.fromisoformat(day).date())
        frame = frame[frame.expiry == code]
        snapshots = {t.to_pydatetime(): g for t, g in frame.groupby("timestamp")}
        spot_snap = {t.to_pydatetime(): float(g.spot.iloc[0]) for t, g in frame.groupby("timestamp")}
        contracts = {str(sym): tr._Contract(g.drop_duplicates("timestamp", keep="last").set_index("timestamp"), spot_snap)
                     for sym, g in frame.groupby("tradingsymbol")}
        by_minute = {p["t"]: p for p in prints}

        class Market:
            now = nbars[0][0]
            nifty = nbars[0][1]
            sensex = sbars[0][1]
            bar = nbars[0]
            def spots(self):
                return {"SENSEX": self.sensex, "NIFTY": self.nifty}
            def quote(self, symbol, profile=pr.NIFTY_I):
                c = contracts.get(symbol)
                if c is None:
                    return None
                minute = self.now.replace(second=0, microsecond=0)
                q = c.quote(minute)
                if q is None:
                    return None
                # Same fitted-delta interpolation and spread as the paper reference.
                ltp = max(.05, q[0] + (1 if symbol.endswith("CE") else -1) * c.delta(minute)
                          * (self.nifty - self.bar[1]))
                return {"ltp": ltp, "bid": max(.05, ltp - q[1]), "ask": ltp + q[1]}
        market = Market()
        feed = FakeFeed({})
        feed.open_ref = sbars[0][4]
        ctx = {"task_id": "archive-replay", "adapters": {}, "reconciled": set(), "conns": {},
               "bars": pr.SessionBars(bars_fn=lambda d: [(b[0], b[4]) for b in sbars])}
        replace(pr, "opening_ratio", lambda *a: ratio)
        replace(pr, "notify_telegram", lambda *a: None)
        replace(lr, "notify_telegram", lambda *a: None)
        replace(svc, "today_ist_iso", lambda: day)
        replace(svc, "_now_iso", lambda: market.now.replace(tzinfo=pr.IST).astimezone(timezone.utc).isoformat())
        replace(lr, "_now_iso", lambda: market.now.replace(tzinfo=pr.IST).astimezone(timezone.utc).isoformat())
        replace(pr, "option_chain", lambda *a: snapshots.get(market.now.replace(second=0, microsecond=0), pd.DataFrame()))
        # Keep live configured sizing at one lot. The paper book uses a fixed premium budget;
        # record any resulting recovery-book differences rather than hiding them.
        actual = []
        banked = False
        for bar in nbars:
            t = bar[0]
            if t not in s_at or t.time() > pr.EOD_FLAT:
                continue
            market.bar = bar
            samples = tr._dense(tr._nodes(bar))
            for i, (sec, spot) in enumerate(samples):
                market.now = t + timedelta(seconds=sec)
                market.nifty, market.sensex = spot, s_at[t][1]
                if t in by_minute and sec >= by_minute[t]["sec"]:
                    feed.snap = by_minute[t]
                before = svc.get_trade_state(user, connection)
                pr.process_connection(user, connection, ctx=ctx, feed=feed, market=market,
                                      now=market.now, adapter_factory=FakeAdapter)
                after = svc.get_trade_state(user, connection)
                if before["position"] != "OPEN" and after["position"] == "OPEN":
                    actual.append({"entry_ts": market.now.isoformat(), "side": "CE" if after["side"] == "CALL" else "PE",
                                   "tradingsymbol": after["symbol"]})
                    # The reference removes the already-visited extreme after entry.
                    samples[:] = samples[:i+1] + tr._dense(tr._rest_of_minute(sec, spot, bar))[1:]
                elif before["position"] == "OPEN" and after["position"] != "OPEN":
                    with live_db.get_live_conn() as c:
                        row = c.execute("SELECT reason FROM live_trades ORDER BY rowid DESC LIMIT 1").fetchone()
                    actual[-1].update(exit_ts=market.now.isoformat(), exit_rule=row[0])
                pc = ctx["conns"].get(connection)
                if (pc is not None and after["position"] != "OPEN" and not pc.engine.prev_open
                        and pc.engine.banked()):
                    banked = True
                    break
            if banked:
                break
        expected = paper["trades"]
        differences = []
        if len(actual) != len(expected):
            differences.append(f"trade count runner={len(actual)} paper={len(expected)}")
        for i, (a, b) in enumerate(zip(actual, expected)):
            for key in ("side", "tradingsymbol", "exit_rule"):
                if a.get(key) != b.get(key):
                    differences.append(f"trade {i+1} {key}: runner={a.get(key)} paper={b.get(key)}")
            for key in ("entry_ts", "exit_ts"):
                if a.get(key) and b.get(key) and abs((datetime.fromisoformat(a[key])-datetime.fromisoformat(b[key])).total_seconds()) > 60:
                    differences.append(f"trade {i+1} {key}: runner={a[key]} paper={b[key]}")
        return {"day": day, "ratio": ratio, "paper_trades": expected, "runner_trades": actual,
                "differences": differences, "sampling": "paper dense OHLC/fitted-delta model; configured 1 lot"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "storage/state/nifty_scalper_preflight")
    parser.add_argument("--days", nargs="+", default=["2026-10-13", "2026-10-16", "2026-10-19", "2026-10-21"])
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    results = []
    for day in args.days:
        result = replay(day, args.root)
        results.append(result)
        print(day, f"runner={len(result['runner_trades'])} paper={len(result['paper_trades'])}", result["differences"], flush=True)
        (args.root / "replay-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return int(any(r["differences"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
