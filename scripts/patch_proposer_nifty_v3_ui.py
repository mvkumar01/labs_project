"""One-shot wiring of the two Nifty Scalper V3 paper books - proposer_nifty_v3 (prints every 3 minutes, the live
preset) and proposer_nifty_v3_pclock (Pramanaa's print timing, paper only) - as their own tabs in the labs
dashboard, the paper loop and the Overview cards, each wired exactly as the Nifty Scalper V1 book
(scripts/patch_proposer_nifty_v5_ui.py) and placed after it.

The files it edits can carry other sessions' work, so every edit is an anchored insertion after the Nifty
Scalper V1 wiring: anchors are asserted, and re-running is a no-op.
`python scripts/patch_proposer_nifty_v3_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import patch_proposer_nifty_v5_ui as v1  # noqa: E402

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
BOOKS = (("proposer_nifty_v3", "Nifty Scalper V3", "Rs 12L a trade, prints every 3 min"),
         ("proposer_nifty_v3_pclock", "Nifty Scalper V3 (Pramanaa timing)", "Rs 12L a trade, paper only"))


def _for(text: str, key: str, label: str) -> str:
    return (text.replace("proposer_nifty_v5", key).replace("proposer-nifty-v5", key.replace("_", "-"))
                .replace("NIFTY on Sensex V5 signals", label))


def main() -> int:
    tabs = '    "proposer_nifty_v5": "Nifty Scalper V1",\n'
    vars_line = "    proposer_nifty_v5_rows, proposer_nifty_v5_trades, proposer_nifty_v5_stats = [], [], {}\n"
    kwargs = ("        proposer_nifty_v5_rows=proposer_nifty_v5_rows,\n"
              "        proposer_nifty_v5_trades=proposer_nifty_v5_trades,\n"
              "        proposer_nifty_v5_stats=proposer_nifty_v5_stats,\n")
    imp = "    from labs.engine.proposer_nifty_v5_book import run_day as run_proposer_nifty_v5_day\n"
    log_key = '        "proposer_nifty_v5": None,\n'
    runner = '                ("proposer_nifty_v5", run_proposer_nifty_v5_day),\n'
    meta = '    "proposer_nifty_v5": {"label": "Nifty Scalper V1", "instrument": "NIFTY options", "size": "Rs 12L a trade"},\n'
    builder = '        "proposer_nifty_v5": lambda: _proposer(conn, "proposer_nifty_v5", today),\n'

    routes, page, loop, overview = [], [], [], []
    for key, label, size in reversed(BOOKS):          # each inserted right after V1, so V3 ends up first
        routes += [(tabs, tabs + f'    "{key}": "{label}",\n'),
                   (vars_line, vars_line + _for(vars_line, key, label)),
                   (v1.ROUTES_QUERY, v1.ROUTES_QUERY + _for(v1.ROUTES_QUERY, key, label)),
                   (kwargs, kwargs + _for(kwargs, key, label)),
                   (v1.ROUTES_ENDPOINT, v1.ROUTES_ENDPOINT + _for(v1.ROUTES_ENDPOINT, key, label))]
        page += [(v1.TEMPLATE_PANEL, v1.TEMPLATE_PANEL + _for(v1.TEMPLATE_PANEL, key, label)),
                 (v1.TEMPLATE_SCRIPT, v1.TEMPLATE_SCRIPT + _for(v1.TEMPLATE_SCRIPT, key, label))]
        loop += [(imp, imp + _for(imp, key, label)), (log_key, log_key + _for(log_key, key, label)),
                 (runner, runner + _for(runner, key, label))]
        overview += [(meta, meta + f'    "{key}": {{"label": "{label}", "instrument": "NIFTY options", "size": "{size}"}},\n'),
                     (builder, builder + _for(builder, key, label))]
    for rel, edits in (("labs/ui/routes.py", routes), ("templates/live_strategy.html", page),
                       ("pa_paper_tracker_loop.py", loop), ("labs/services/book_overview_more.py", overview)):
        print(f"{rel}: {v1.patch(ROOT / rel, edits)} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
