"""One-shot wiring of the "Sensex 2.5% V6 IV-half" paper book (proposer_v6_dt25_ivh: Sensex 2.5% V6 at half size on a
high-IV day) into the labs dashboard, the paper loop, the Overview cards and the strategy report, wired exactly as the
Sensex 2.5% V6 floor 125 book (scripts/patch_proposer_v6_dt25_f125_ui.py) and placed right after it.

The files it edits can carry other sessions' work, so every edit is an anchored insertion after the floor-125 wiring:
anchors are asserted, and re-running is a no-op.
`python scripts/patch_proposer_v6_dt25_ivh_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import patch_proposer_v5_dt25_ui as v5  # noqa: E402
from patch_proposer_v6_dt25_f125_ui import _f125  # noqa: E402
from patch_proposer_v6_dt25_ui import _v6  # noqa: E402

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
F125, F125_LABEL = "proposer_v6_dt25_f125", "Sensex 2.5% V6 · floor 125"
KEY, LABEL = "proposer_v6_dt25_ivh", "Sensex 2.5% V6 · IV half"


def _ivh(f125_text: str) -> str:
    return (f125_text.replace(F125, KEY).replace(F125.replace("_", "-"), KEY.replace("_", "-"))
                     .replace(F125_LABEL, LABEL))


def _after(v5_text: str) -> tuple[str, str]:
    """(the floor-125 copy of a V5 2.5% snippet, that copy followed by the IV-half copy)."""
    anchor = _f125(_v6(v5_text))
    return anchor, anchor + _ivh(anchor)


def main() -> int:
    tabs = '    "proposer_v5_dt25": "Sensex V5 2.5%",\n'
    vars_line = "    proposer_v5_dt25_rows, proposer_v5_dt25_trades, proposer_v5_dt25_stats = [], [], {}\n"
    kwargs = ("        proposer_v5_dt25_rows=proposer_v5_dt25_rows,\n"
              "        proposer_v5_dt25_trades=proposer_v5_dt25_trades,\n"
              "        proposer_v5_dt25_stats=proposer_v5_dt25_stats,\n")
    imp = "    from labs.engine.proposer_v5_dt25_book import run_day as run_proposer_v5_dt25_day\n"
    log_key = '        "proposer_v5_dt25": None,\n'
    runner = '                ("proposer_v5_dt25", run_proposer_v5_dt25_day),\n'
    meta = '    "proposer_v5_dt25": {"label": "Sensex V5 2.5%", "instrument": "SENSEX options", "size": "100 lots"},\n'
    builder = '        "proposer_v5_dt25": lambda: _proposer(conn, "proposer_v5_dt25", today),\n'
    report = f"SOURCES['{F125}'] = Source('{F125}_trades', 'SENSEX', 2000)\n"
    meta_ivh = _f125(_v6(meta))
    edits = {
        "labs/ui/routes.py": [_after(tabs), _after(vars_line), _after(v5.ROUTES_QUERY), _after(kwargs),
                              _after(v5.ROUTES_ENDPOINT)],
        "templates/live_strategy.html": [_after(v5.TEMPLATE_PANEL), _after(v5.TEMPLATE_SCRIPT)],
        "pa_paper_tracker_loop.py": [_after(imp), _after(log_key), _after(runner)],
        "labs/services/book_overview_more.py": [
            (meta_ivh, meta_ivh + _ivh(meta_ivh).replace('"size": "100 lots"', '"size": "100 or 50 lots"')),
            _after(builder)],
        "labs/services/strategy_report.py": [(report, report + f"SOURCES['{KEY}'] = Source('{KEY}_trades', 'SENSEX', 2000)\n")],
    }
    for rel, rel_edits in edits.items():
        print(f"{rel}: {v5.patch(ROOT / rel, rel_edits)} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
