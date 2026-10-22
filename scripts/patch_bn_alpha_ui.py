"""One-shot wiring of the BANKNIFTY Alpha paper book (bn_alpha) into the labs dashboard, the paper loop and
the Overview cards.

The files it edits (labs/ui/routes.py, templates/live_strategy.html, pa_paper_tracker_loop.py,
labs/services/book_overview_more.py) can carry other sessions' work, so the edits are small anchored
insertions next to the "Sensex V5" wiring: every anchor is asserted, a near-miss fails instead of writing
into the wrong place, and re-running is a no-op.
`python scripts/patch_bn_alpha_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # BANKNIFTY Alpha (paper).
        if active_live_tab == "bn_alpha":
            try:
                from labs.engine.bn_alpha_tracker import tab_data as bn_alpha_tab_data
                bn_alpha_rows, bn_alpha_trades, bn_alpha_stats = bn_alpha_tab_data(
                    conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    bn_alpha_stats = {"error": str(exc)}

'''

TEMPLATE_PANEL = """    {% elif active_live_tab == 'bn_alpha' %}
    {% include '_bn_alpha.html' %}
"""

OVERVIEW_CARD = '''def _bn_alpha(conn, today: str) -> dict:
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])} for r in _rows(
        conn, "SELECT trade_date, net_rs FROM bn_alpha_daily WHERE status IN ('traded','no_trade','live','partial_quotes')")]
    trades = _rows(conn, "SELECT trade_date, side, status, entry_ask, qty FROM bn_alpha_trades")
    held = [t for t in trades if t["trade_date"] == today and t["status"] == "open"]
    capital = max((_f(t["entry_ask"]) * _f(t["qty"]) for t in trades), default=0.0)
    return _card("bn_alpha", MORE_BOOKS["bn_alpha"], daily, today, capital=capital,
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
    tabs = '    "proposer_v5": "Sensex V5",\n'
    vars_line = "    proposer_v5_rows, proposer_v5_trades, proposer_v5_stats = [], [], {}\n"
    comment = "        # Sensex V5 (paper).\n"
    kwargs_line = "        proposer_v5_stats=proposer_v5_stats,\n"
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "bn_alpha": "BANKNIFTY Alpha",\n'),
        (vars_line, vars_line + "    bn_alpha_rows, bn_alpha_trades, bn_alpha_stats = [], [], {}\n"),
        (comment, ROUTES_QUERY + comment),
        (kwargs_line, kwargs_line + "        bn_alpha_rows=bn_alpha_rows,\n"
                                    "        bn_alpha_trades=bn_alpha_trades,\n"
                                    "        bn_alpha_stats=bn_alpha_stats,\n"),
    ])
    print(f"routes.py: {n} edits applied")

    panel = "    {% elif active_live_tab == 'proposer_v5' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [(panel, TEMPLATE_PANEL + panel)])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.proposer_v5_book import run_day as run_proposer_v5_day\n"
    log_key = '        "proposer_v5": None,\n'
    runner = '                ("proposer_v5", run_proposer_v5_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.bn_alpha_tracker import run_paper_day as run_bn_alpha_day\n"),
        (log_key, log_key + '        "bn_alpha": None,\n'),
        (runner, runner + '                ("bn_alpha", run_bn_alpha_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")

    meta = '    "proposer_v5": {"label": "Sensex V5", "instrument": "SENSEX options", "size": "100 lots"},\n'
    builder = '        "proposer_v5": lambda: _proposer(conn, "proposer_v5", today),\n'
    card_anchor = "def _expiry_sale(conn, book: str, today: str) -> dict:\n"
    n = patch(ROOT / "labs" / "services" / "book_overview_more.py", [
        (meta, meta + '    "bn_alpha": {"label": "BANKNIFTY Alpha", "instrument": "BANKNIFTY options", "size": "1 lot"},\n'),
        (builder, builder + '        "bn_alpha": lambda: _bn_alpha(conn, today),\n'),
        (card_anchor, OVERVIEW_CARD + card_anchor),
    ])
    print(f"book_overview_more.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
