"""One-shot wiring of the two fixed-clock Nifty Scalper V4 paper books (proposer_nifty_v4_fc at 1.5%,
proposer_nifty_v4_dt25_fc at 2.5%: prints at a fixed 30 s, no entry after the 2nd loss) as their own tabs in the labs
dashboard, the paper loop, the Overview cards and the strategy report, wired exactly as the Nifty Scalper V4 2.5% book
(scripts/patch_proposer_nifty_v4_dt25_ui.py) and placed after it.

The files it edits can carry other sessions' work, so every edit is an anchored insertion after the V4 2.5% wiring:
anchors are asserted, and re-running is a no-op.
`python scripts/patch_proposer_nifty_v4_fc_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import patch_proposer_nifty_v5_ui as v1  # noqa: E402
from patch_proposer_nifty_v4_ui import _for  # noqa: E402

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
AFTER = ("proposer_nifty_v4_dt25", "Nifty Scalper V4 2.5%")
BOOKS = (("proposer_nifty_v4_fc", "Nifty Scalper V4 1.5% · fixed clock",
          "Rs 12L a trade, 1.5% day target, prints at 30 s, stop after 2 losses"),
         ("proposer_nifty_v4_dt25_fc", "Nifty Scalper V4 2.5% · fixed clock",
          "Rs 12L a trade, 2.5% day target, prints at 30 s, stop after 2 losses"))


def _after(text: str) -> tuple[str, str]:
    """(the V4 2.5% copy of a V1 snippet, that copy followed by the two fixed-clock copies)."""
    anchor = _for(text, *AFTER)
    return anchor, anchor + "".join(_for(text, key, label) for key, label, _ in BOOKS)


def main() -> int:
    vars_line = "    proposer_nifty_v5_rows, proposer_nifty_v5_trades, proposer_nifty_v5_stats = [], [], {}\n"
    kwargs = ("        proposer_nifty_v5_rows=proposer_nifty_v5_rows,\n"
              "        proposer_nifty_v5_trades=proposer_nifty_v5_trades,\n"
              "        proposer_nifty_v5_stats=proposer_nifty_v5_stats,\n")
    imp = "    from labs.engine.proposer_nifty_v5_book import run_day as run_proposer_nifty_v5_day\n"
    log_key = '        "proposer_nifty_v5": None,\n'
    runner = '                ("proposer_nifty_v5", run_proposer_nifty_v5_day),\n'
    builder = '        "proposer_nifty_v5": lambda: _proposer(conn, "proposer_nifty_v5", today),\n'
    tab = f'    "{AFTER[0]}": "{AFTER[1]}",\n'
    meta = (f'    "{AFTER[0]}": {{"label": "{AFTER[1]}", "instrument": "NIFTY options", "size": "Rs 12L a trade, '
            '2.5% day target"},\n')
    report = "NIFTY_SCALPER_KEYS += ('proposer_nifty_v4_dt25',)\n"
    edits = {
        "labs/ui/routes.py": [(tab, tab + "".join(f'    "{k}": "{lb}",\n' for k, lb, _ in BOOKS)), _after(vars_line),
                              _after(v1.ROUTES_QUERY), _after(kwargs), _after(v1.ROUTES_ENDPOINT)],
        "templates/live_strategy.html": [_after(v1.TEMPLATE_PANEL), _after(v1.TEMPLATE_SCRIPT)],
        "pa_paper_tracker_loop.py": [_after(imp), _after(log_key), _after(runner)],
        "labs/services/book_overview_more.py": [
            (meta, meta + "".join(f'    "{k}": {{"label": "{lb}", "instrument": "NIFTY options", "size": "{sz}"}},\n'
                                  for k, lb, sz in BOOKS)),
            _after(builder)],
        "labs/services/strategy_report.py": [(report, report + "".join(f"NIFTY_SCALPER_KEYS += ('{k}',)\n"
                                                                      for k, _, _ in BOOKS))],
    }
    for rel, rel_edits in edits.items():
        print(f"{rel}: {v1.patch(ROOT / rel, rel_edits)} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
