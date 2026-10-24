"""One-shot wiring of the SENSEX Alpha paper books (sx_pc400, sx_open1400) into the labs dashboard, the paper
loop, the Overview cards and the strategy report.

The files it edits (labs/ui/routes.py, templates/live_strategy.html, pa_paper_tracker_loop.py,
labs/services/book_overview_more.py, labs/services/strategy_report.py) can carry other sessions' work, so the
edits are small anchored insertions next to the "BANKNIFTY Alpha" wiring: every anchor is asserted, a near-miss
fails instead of writing into the wrong place, and re-running is a no-op.
`python scripts/patch_sx_alpha_ui.py [root]` patches the files under `root` (default: this repo).
Run scripts/patch_bn_alpha_ui.py first (its lines are the anchors).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # SENSEX Alpha (paper): two books, one template.
        if active_live_tab in ("sx_pc400", "sx_open1400"):
            try:
                from labs.engine.sx_alpha_tracker import tab_data as sx_tab_data
                sx_rows, sx_trades, sx_stats = sx_tab_data(conn, active_live_tab, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    sx_stats = {"error": str(exc)}

'''

TEMPLATE_PANEL = """    {% elif active_live_tab in ['sx_pc400', 'sx_open1400'] %}
    {% include '_sx_alpha.html' %}
"""

OVERVIEW_CARD = '''def _sx_alpha(conn, key: str, today: str) -> dict:
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])} for r in _rows(
        conn, f"SELECT trade_date, net_rs FROM {key}_daily WHERE status IN ('traded','no_trade','live','partial_quotes')")]
    trades = _rows(conn, f"SELECT trade_date, side, status, entry_ask, qty FROM {key}_trades")
    held = [t for t in trades if t["trade_date"] == today and t["status"] == "open"]
    capital = max((_f(t["entry_ask"]) * _f(t["qty"]) for t in trades), default=0.0)
    return _card(key, MORE_BOOKS[key], daily, today, capital=capital,
                 position=f"Long {held[-1]['side']}" if held else "Flat",
                 trades=sum(1 for t in trades if t["status"] == "closed"))


'''


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
    tabs = '    "bn_alpha": "BANKNIFTY Alpha",\n'
    vars_line = "    bn_alpha_rows, bn_alpha_trades, bn_alpha_stats = [], [], {}\n"
    comment = "        # BANKNIFTY Alpha (paper).\n"
    kwargs_line = "        bn_alpha_stats=bn_alpha_stats,\n"
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "sx_pc400": "SENSEX Alpha PC400",\n    "sx_open1400": "SENSEX Alpha OPEN1400",\n'),
        (vars_line, vars_line + "    sx_rows, sx_trades, sx_stats = [], [], {}\n"),
        (comment, ROUTES_QUERY + comment),
        (kwargs_line, kwargs_line + "        sx_rows=sx_rows,\n        sx_trades=sx_trades,\n        sx_stats=sx_stats,\n"),
    ])
    print(f"routes.py: {n} edits applied")

    panel = "    {% elif active_live_tab == 'bn_alpha' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [(panel, TEMPLATE_PANEL + panel)])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.bn_alpha_tracker import run_paper_day as run_bn_alpha_day\n"
    log_key = '        "bn_alpha": None,\n'
    runner = '                ("bn_alpha", run_bn_alpha_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.sx_alpha_tracker import run_paper_day as run_sx_alpha_day\n"),
        (log_key, log_key + '        "sx_alpha": None,\n'),
        (runner, runner + '                ("sx_alpha", run_sx_alpha_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")

    meta = '    "bn_alpha": {"label": "BANKNIFTY Alpha", "instrument": "BANKNIFTY options", "size": "1 lot"},\n'
    builder = '        "bn_alpha": lambda: _bn_alpha(conn, today),\n'
    card_anchor = "def _bn_alpha(conn, today: str) -> dict:\n"
    n = patch(ROOT / "labs" / "services" / "book_overview_more.py", [
        (meta, meta + '    "sx_pc400": {"label": "SENSEX Alpha PC400", "instrument": "SENSEX options", "size": "1 lot"},\n'
                      '    "sx_open1400": {"label": "SENSEX Alpha OPEN1400", "instrument": "SENSEX options", "size": "1 lot"},\n'),
        (builder, builder + '        "sx_pc400": lambda: _sx_alpha(conn, "sx_pc400", today),\n'
                            '        "sx_open1400": lambda: _sx_alpha(conn, "sx_open1400", today),\n'),
        (card_anchor, OVERVIEW_CARD + card_anchor),
    ])
    print(f"book_overview_more.py: {n} edits applied")

    source = "SOURCES['bn_alpha'] = Source('bn_alpha_trades', 'BANKNIFTY', 30)\n"
    n = patch(ROOT / "labs" / "services" / "strategy_report.py", [
        (source, source + "SOURCES['sx_pc400'] = Source('sx_pc400_trades', 'SENSEX', 20)\n"
                          "SOURCES['sx_open1400'] = Source('sx_open1400_trades', 'SENSEX', 20)\n"),
    ])
    print(f"strategy_report.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
