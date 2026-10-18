"""Replay and independently reconcile the isolated crypto repair snapshot."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labs.engine import crypto_xs_tracker as tracker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/crypto_repair"))
    parser.add_argument("--store", type=Path, default=tracker.data.STORE)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.output / "repaired.db")
    tracker.rebuild(conn, store=args.store, offline=True)
    rows, _, stats = tracker.tab_data(conn)
    reference = tracker.tab_data(conn, reference=True)[0]
    by_day = defaultdict(list)
    for (payload,) in conn.execute("SELECT payload FROM crypto_xs_account_weights"):
        line = json.loads(payload)
        by_day[line["decision_date"]].append(line)
    # Independent ledger checks: quantities/prices and fees, rather than the
    # engine's precomputed aggregate return. Funding pieces must also reconcile.
    balance = tracker.EQUITY_START
    max_error = 0.
    for row in reversed(rows):
        if not row["complete"]:
            continue
        lines = by_day[row["decision_date"]]
        price = sum(r["quantity"] * (r["exit_price"] - r["entry_price"])
                    for r in lines if r["entry_price"] is not None and r["exit_price"] is not None)
        funding = sum(r["funding_pnl"] for r in lines) * balance
        cost = sum(r["traded"] for r in lines) * balance * tracker.engine.COST_BPS / 1e4
        calculated = (price + funding - cost) / balance
        max_error = max(max_error, abs(calculated - row["net"]))
        assert abs(calculated - row["net"]) < 1e-12
        balance += price + funding - cost
        assert abs(balance - row["equity"]) < 1e-7
    with (args.output / "account_daily.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(reversed(rows))
    (args.output / "repaired_stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    (args.output / "account_contracts.json").write_text(json.dumps(dict(by_day), indent=1), encoding="utf-8")
    def periods(series):
        months = defaultdict(list)
        for row in series:
            if row["complete"]:
                months[row["decision_date"][:7]].append(row["net"])
        return {m: (len(v), 100 * (float(np.prod([1 + r for r in v])) - 1)) for m, v in sorted(months.items())}
    baseline = json.loads((args.output / "baseline_stats.json").read_text())
    old = {m["month"]: (m["days"], m["net_pct"]) for key in ["research", "unseen"] for m in baseline[key]["months"]}
    ref, account = periods(reference), periods(rows)
    text = ["# Crypto taker-imbalance repair and replay", "",
            "**Share with caveats:** data and accounting repairs validated locally; not deployed. "
            "The quantity account includes estimated TON and VANRY settlement prices. "
            "Both months after the research period remain untuned.", "",
            "| Month | Original published model | Repaired reference model | Quantity paper account | Completed days |",
            "|---|---:|---:|---:|---:|"]
    for month, (days, net) in account.items():
        text.append(f"| {month} | {old.get(month, (0, 0))[1]:+.2f}% ({old.get(month, (0, 0))[0]} days) | "
                    f"{ref[month][1]:+.2f}% | {net:+.2f}% | {days} |")
    text += ["", f"Original snapshot ends {baseline['last_done']['decision_date']}; repaired completed holds end {stats['last_done']['decision_date']}. "
             "October rows with different day counts are not a pure repair comparison.", "",
             f"Quantity-account equity: **${stats['equity']:,.2f}** from $100,000; still provisional. "
             f"Reference-model equity: **${reference[0]['equity'] if reference[0]['complete'] else reference[1]['equity']:,.2f}**.", "",
             "The original research engine is unchanged. The added account starts flat on June 1, charges opening costs, "
             "tracks quantities and compounds actual simulated P&L. It retains unavailable holdings without inventing fills, "
             "and closes delisted contracts at exchange-confirmed times using labelled valuation estimates. "
             "Funding uses settlement-time hourly valuation prices; mark-price differences, size rounding, minimum orders, depth and liquidation are not modelled.", "",
             "The universe is the original Binance USDT perpetual archive universe, including stock and commodity perpetuals; "
             "this is broader than crypto assets. Changing that universe would be a separate research experiment.", "",
             "Actual API funding coverage suppresses premium estimates within the covered interval, including hours with no settlement. "
             "Funding after the coverage endpoint remains estimated/incomplete. The API repair uses completed UTC days and paginates history.", "",
             f"The original June–August monthly reference check: **{'passed' if stats['reconcile']['ok'] else 'different or incomplete'}**, "
             f"through {stats['reconcile']['through']}; June also has an unchanged daily fixture test. "
             f"Independent quantity/price/fee replay max daily return difference: {max_error:.3g}.", "",
             "Reproduce: `python -m labs.engine.crypto_xs_public_api` on a permitted API host, then "
             "`python scripts/crypto_xs_repair_report.py`. The original downloaded panels, manifest hashes, baseline DB, "
             "daily account CSV and contract-level JSON are retained in this report directory.", "",
             "Deployment: source and panels are isolated in the crypto-xs-repair worktree. "
             "Do not overwrite a running panel writer. Stop only the paper loop after market hours, back up the crypto panels/source, "
             "publish verified repairs, rebuild only crypto tables, reload the web app and resume the paper loop. "
             "The real-order runner and its database must remain untouched. No commit or push has been made.", "",
             "Sources: [Binance public API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data), "
             "[Binance archive](https://github.com/binance/binance-public-data)."]
    (args.output / "RESULTS.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    print(json.dumps(dict(equity=stats["equity"], months=account, max_replay_error=max_error), indent=2))


if __name__ == "__main__":
    main()
