#!/usr/bin/env python3
"""One-off ledger correction for the 2026-07-10 tick-stop double-fire.

Reality (broker order ledger): the 09:27:19 BUY @259.85 was actually closed
by the 09:28 SELL @270.05 (order 260710000096524). The two same-minute tick
stops in between recorded:
  - a trade "exit @257.5" that placed NO broker order (echoed the earlier
    exit's fill), and
  - two phantom rows with entry_price=0 (+16,658.76 and +17,472.97 net).

Correction: rewrite the 259.85 trade to exit @270.05, delete the two phantom
rows, rebuild live_day_pnl for the date. Idempotent; run ONCE after market
close from ~/labs_project:  python3 fix_ledger_20260710.py
"""
import json
import sqlite3

import live.live_service as svc

USER = "3adc83aa2b3144328793afbc7cd9ee65"
CONN = "3adc83aa2b3144328793afbc7cd9ee65:angel"
DATE = "2026-07-10"
QTY = 65
REAL_ENTRY, WRONG_EXIT, REAL_EXIT = 259.85, 257.5, 270.05
REAL_EXIT_TIME = "2026-07-10T03:58:20+00:00"

conn = svc.get_live_conn()
conn.row_factory = sqlite3.Row
try:
    phantoms = conn.execute(
        "SELECT trade_id, net_pnl FROM live_trades WHERE user_id=? AND conn_id=? "
        "AND dry_run=0 AND exit_time LIKE ? AND entry_price=0.0 AND side IS NULL",
        (USER, CONN, DATE + "%"),
    ).fetchall()
    fix_row = conn.execute(
        "SELECT trade_id, entry_price, exit_price FROM live_trades "
        "WHERE user_id=? AND conn_id=? AND dry_run=0 AND exit_time LIKE ? "
        "AND ABS(entry_price-?)<0.01 AND ABS(exit_price-?)<0.01 "
        "AND reason='ENTRY_SPOT_SL_TICK'",
        (USER, CONN, DATE + "%", REAL_ENTRY, WRONG_EXIT),
    ).fetchone()

    if not phantoms and fix_row is None:
        print("Nothing to correct — already fixed or rows not found. No-op.")
        raise SystemExit(0)

    print(f"phantom rows to delete: {len(phantoms)} "
          f"(net {sum(float(r['net_pnl'] or 0) for r in phantoms):+,.2f})")
    for r in phantoms:
        conn.execute("DELETE FROM live_trades WHERE trade_id=?", (r["trade_id"],))

    if fix_row is not None:
        p = svc.calc_net_option_pnl(REAL_ENTRY, REAL_EXIT, QTY)
        conn.execute(
            "UPDATE live_trades SET exit_price=?, exit_time=?, pnl=?, gross_pnl=?, "
            "charges_total=?, net_pnl=?, charges_json=? WHERE trade_id=?",
            (REAL_EXIT, REAL_EXIT_TIME, float(p["gross_pnl"]), float(p["gross_pnl"]),
             float(p["charges"]["total_charges"]), float(p["net_pnl"]),
             json.dumps(p["charges"]), fix_row["trade_id"]),
        )
        print(f"rewrote {REAL_ENTRY} -> {REAL_EXIT}: net {p['net_pnl']:+,.2f}")

    tot = conn.execute(
        "SELECT COALESCE(SUM(net_pnl),0), COUNT(*) FROM live_trades "
        "WHERE user_id=? AND conn_id=? AND dry_run=0 AND exit_time LIKE ?",
        (USER, CONN, DATE + "%"),
    ).fetchone()
    conn.execute(
        "UPDATE live_day_pnl SET realized_pnl=?, trade_count=? "
        "WHERE trade_date=? AND conn_id=?",
        (float(tot[0]), int(tot[1]), DATE, CONN),
    )
    conn.commit()
    print(f"live_day_pnl -> {float(tot[0]):+,.2f} over {int(tot[1])} trades")
finally:
    conn.close()
