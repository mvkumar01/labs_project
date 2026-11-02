"""One-shot wiring of the Nifty Scalper V4 paper book (proposer_nifty_v4: V3 with the entry blocks of
live/engine/entry_blocks.py, the live preset) as its own tab in the labs dashboard, the paper loop, the Overview cards
and the strategy report, wired exactly as the Nifty Scalper V3 books (scripts/patch_proposer_nifty_v3_ui.py) and placed
after them.

The files it edits can carry other sessions' work, so every edit is an anchored insertion after the V3 (Pramanaa timing)
wiring: anchors are asserted, and re-running is a no-op.
`python scripts/patch_proposer_nifty_v4_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import patch_proposer_nifty_v5_ui as v1  # noqa: E402

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
AFTER = ("proposer_nifty_v3_pclock", "Nifty Scalper V3 (Pramanaa timing)")
KEY, LABEL, SIZE = "proposer_nifty_v4", "Nifty Scalper V4", "Rs 12L a trade, prints every 3 min, entry blocks"


def _for(text: str, key: str, label: str) -> str:
    return (text.replace("proposer_nifty_v5", key).replace("proposer-nifty-v5", key.replace("_", "-"))
                .replace("NIFTY on Sensex V5 signals", label))


def _after(text: str) -> tuple[str, str]:
    """(the V3 Pramanaa-timing copy of a V1 snippet, that copy followed by the V4 copy)."""
    anchor = _for(text, *AFTER)
    return anchor, anchor + _for(text, KEY, LABEL)


def main() -> int:
    tabs = '    "proposer_nifty_v5": "Nifty Scalper V1",\n'
    vars_line = "    proposer_nifty_v5_rows, proposer_nifty_v5_trades, proposer_nifty_v5_stats = [], [], {}\n"
    kwargs = ("        proposer_nifty_v5_rows=proposer_nifty_v5_rows,\n"
              "        proposer_nifty_v5_trades=proposer_nifty_v5_trades,\n"
              "        proposer_nifty_v5_stats=proposer_nifty_v5_stats,\n")
    imp = "    from labs.engine.proposer_nifty_v5_book import run_day as run_proposer_nifty_v5_day\n"
    log_key = '        "proposer_nifty_v5": None,\n'
    runner = '                ("proposer_nifty_v5", run_proposer_nifty_v5_day),\n'
    builder = '        "proposer_nifty_v5": lambda: _proposer(conn, "proposer_nifty_v5", today),\n'
    pc_tab = f'    "{AFTER[0]}": "{AFTER[1]}",\n'
    pc_meta = (f'    "{AFTER[0]}": {{"label": "{AFTER[1]}", "instrument": "NIFTY options", "size": "Rs 12L a trade, '
               'paper only"},\n')
    report = "NIFTY_SCALPER_KEYS = ('proposer_nifty_v5', 'proposer_nifty_v3', 'proposer_nifty_v3_pclock')\n"
    edits = {
        "labs/ui/routes.py": [(pc_tab, pc_tab + f'    "{KEY}": "{LABEL}",\n'), _after(vars_line), _after(v1.ROUTES_QUERY),
                              _after(kwargs), _after(v1.ROUTES_ENDPOINT)],
        "templates/live_strategy.html": [_after(v1.TEMPLATE_PANEL), _after(v1.TEMPLATE_SCRIPT)],
        "pa_paper_tracker_loop.py": [_after(imp), _after(log_key), _after(runner)],
        "labs/services/book_overview_more.py": [
            (pc_meta, pc_meta + f'    "{KEY}": {{"label": "{LABEL}", "instrument": "NIFTY options", "size": "{SIZE}"}},\n'),
            _after(builder)],
        "labs/services/strategy_report.py": [(report, report + f"NIFTY_SCALPER_KEYS += ('{KEY}',)\n")],
    }
    for rel, rel_edits in edits.items():
        print(f"{rel}: {v1.patch(ROOT / rel, rel_edits)} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
