"""One-shot wiring of the Nifty New Alpha V1 paper book (nna_v1) into the labs dashboard, the paper loop, the
Overview cards and the strategy report.

The files it edits (labs/ui/routes.py, templates/live_strategy.html, pa_paper_tracker_loop.py,
labs/services/book_overview_more.py, labs/services/strategy_report.py) can carry other sessions' work, so the
edits are small anchored insertions next to the "SENSEX Alpha" wiring: every anchor is asserted, a near-miss
fails instead of writing into the wrong place, and re-running is a no-op.
`python scripts/patch_nna_ui.py [root]` patches the files under `root` (default: this repo).
Run scripts/patch_bn_alpha_ui.py and scripts/patch_sx_alpha_ui.py first (their lines are the anchors).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # Nifty New Alpha V1 (paper).
        if active_live_tab == "nna_v1":
            try:
                from labs.engine.nna_tracker import tab_data as nna_tab_data
                nna_rows, nna_trades, nna_stats = nna_tab_data(conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    nna_stats = {"error": str(exc)}

'''

TEMPLATE_PANEL = """    {% elif active_live_tab == 'nna_v1' %}
    {% include '_nna_alpha.html' %}
"""


def patch(path: Path, edits: list[tuple[str, str]]) -> int:
    """Each edit is (anchor, the anchor with new text before or after it). An edit whose new text is
    already in the file is skipped, so later wiring next to the same anchor does not undo this one."""
    text = path.read_text(encoding="utf-8")
    applied = 0
    for anchor, replacement in edits:
        if replacement.startswith(anchor):
            added = replacement[len(anchor):]
        elif replacement.endswith(anchor):
            added = replacement[:-len(anchor)]
        else:
            raise SystemExit(f"{path.name}: an edit must keep its anchor -> {anchor[:70]!r}")
        if added in text:
            continue
        if text.count(anchor) != 1:
            raise SystemExit(f"{path.name}: anchor found {text.count(anchor)} times -> {anchor[:70]!r}")
        text = text.replace(anchor, replacement, 1)
        applied += 1
    path.write_text(text, encoding="utf-8", newline="")
    return applied


def main() -> int:
    tabs = '    "sx_open1400": "SENSEX Alpha OPEN1400",\n'
    vars_line = "    sx_rows, sx_trades, sx_stats = [], [], {}\n"
    comment = "        # SENSEX Alpha (paper): two books, one template.\n"
    kwargs_line = "        sx_stats=sx_stats,\n"
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "nna_v1": "Nifty New Alpha V1",\n'),
        (vars_line, vars_line + "    nna_rows, nna_trades, nna_stats = [], [], {}\n"),
        (comment, ROUTES_QUERY + comment),
        (kwargs_line, kwargs_line + "        nna_rows=nna_rows,\n        nna_trades=nna_trades,\n        nna_stats=nna_stats,\n"),
    ])
    print(f"routes.py: {n} edits applied")

    panel = "    {% elif active_live_tab in ['sx_pc400', 'sx_open1400'] %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [(panel, TEMPLATE_PANEL + panel)])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.sx_alpha_tracker import run_paper_day as run_sx_alpha_day\n"
    log_key = '        "sx_alpha": None,\n'
    runner = '                ("sx_alpha", run_sx_alpha_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.nna_tracker import run_paper_day as run_nna_day\n"),
        (log_key, log_key + '        "nna_v1": None,\n'),
        (runner, runner + '                ("nna_v1", run_nna_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")

    meta = '    "sx_open1400": {"label": "SENSEX Alpha OPEN1400", "instrument": "SENSEX options", "size": "1 lot"},\n'
    builder = '        "sx_open1400": lambda: _sx_alpha(conn, "sx_open1400", today),\n'
    n = patch(ROOT / "labs" / "services" / "book_overview_more.py", [
        (meta, meta + '    "nna_v1": {"label": "Nifty New Alpha V1", "instrument": "NIFTY options", "size": "1 lot"},\n'),
        # the SENSEX card reads any bn_alpha-style ledger by its table prefix
        (builder, builder + '        "nna_v1": lambda: _sx_alpha(conn, "nna_v1", today),\n'),
    ])
    print(f"book_overview_more.py: {n} edits applied")

    source = "SOURCES['sx_open1400'] = Source('sx_open1400_trades', 'SENSEX', 20)\n"
    n = patch(ROOT / "labs" / "services" / "strategy_report.py", [
        (source, source + "SOURCES['nna_v1'] = Source('nna_v1_trades', 'NIFTY', 65)\n"),
    ])
    print(f"strategy_report.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
