"""One-shot wiring of the OI Unwind paper books (oi_unwind_nifty, oi_unwind_sensex) into the labs dashboard, the
paper loop, the Overview cards and the strategy report.

The files it edits can carry other sessions' work, so the edits are small anchored insertions next to the "Nifty New
Alpha V1" wiring, through patch_nna_ui.patch: every anchor is asserted and re-running is a no-op.
`python scripts/patch_oi_unwind_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patch_nna_ui import ROOT, patch  # noqa: E402

ROUTES_QUERY = '''        # OI Unwind (paper): two books, one template.
        if active_live_tab in ("oi_unwind_nifty", "oi_unwind_sensex"):
            try:
                from labs.engine.oi_unwind_tracker import tab_data as oiu_tab_data
                oiu_rows, oiu_trades, oiu_stats = oiu_tab_data(conn, active_live_tab, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    oiu_stats = {"error": str(exc)}

'''

ROUTES_BACKFILL = '''@labs_bp.route("/api/<any(oi_unwind_nifty, oi_unwind_sensex):key>/backfill", methods=["POST"])
def oi_unwind_backfill(key):
    """Replay bounded batches of sessions for one paper-only OI Unwind book."""
    from labs.engine.oi_unwind_tracker import BOOKS, PAPER_START, run_backfill
    try:
        limit = min(max(int(request.args.get("limit", 3)), 1), 20)
    except (TypeError, ValueError):
        limit = 3
    try:
        return jsonify(run_backfill(
            request.args.get("start", PAPER_START),
            request.args.get("end"),
            books=[BOOKS[key]],
            limit=limit,
            rebuild=request.args.get("rebuild", "0") == "1",
        ))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


'''

TEMPLATE_PANEL = """    {% elif active_live_tab in ['oi_unwind_nifty', 'oi_unwind_sensex'] %}
    {% include '_oi_unwind.html' %}
"""


def main() -> int:
    tabs = '    "nna_v1": "Nifty New Alpha V1",\n'
    vars_line = "    nna_rows, nna_trades, nna_stats = [], [], {}\n"
    comment = "        # Nifty New Alpha V1 (paper).\n"
    kwargs_line = "        nna_stats=nna_stats,\n"
    backfill_route = '@labs_bp.route("/api/nifty_tuesday_put/backfill", methods=["POST"])\n'
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "oi_unwind_nifty": "OI Unwind NIFTY",\n    "oi_unwind_sensex": "OI Unwind SENSEX",\n'),
        (vars_line, vars_line + "    oiu_rows, oiu_trades, oiu_stats = [], [], {}\n"),
        (comment, ROUTES_QUERY + comment),
        (kwargs_line, kwargs_line + "        oiu_rows=oiu_rows,\n        oiu_trades=oiu_trades,\n        oiu_stats=oiu_stats,\n"),
        (backfill_route, ROUTES_BACKFILL + backfill_route),
    ])
    print(f"routes.py: {n} edits applied")

    panel = "    {% elif active_live_tab == 'nna_v1' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [(panel, TEMPLATE_PANEL + panel)])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.nna_tracker import run_paper_day as run_nna_day\n"
    log_key = '        "nna_v1": None,\n'
    runner = '                ("nna_v1", run_nna_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.oi_unwind_tracker import run_paper_day as run_oi_unwind_day\n"),
        (log_key, log_key + '        "oi_unwind": None,\n'),
        (runner, runner + '                ("oi_unwind", run_oi_unwind_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")

    meta = '    "nna_v1": {"label": "Nifty New Alpha V1", "instrument": "NIFTY options", "size": "1 lot"},\n'
    builder = '        "nna_v1": lambda: _sx_alpha(conn, "nna_v1", today),\n'
    n = patch(ROOT / "labs" / "services" / "book_overview_more.py", [
        (meta, meta + '    "oi_unwind_nifty": {"label": "OI Unwind NIFTY", "instrument": "NIFTY options", "size": "1 lot"},\n'
                      '    "oi_unwind_sensex": {"label": "OI Unwind SENSEX", "instrument": "SENSEX options", "size": "1 lot"},\n'),
        (builder, builder + '        "oi_unwind_nifty": lambda: _sx_alpha(conn, "oi_unwind_nifty", today),\n'
                            '        "oi_unwind_sensex": lambda: _sx_alpha(conn, "oi_unwind_sensex", today),\n'),
    ])
    print(f"book_overview_more.py: {n} edits applied")

    source = "SOURCES['nna_v1'] = Source('nna_v1_trades', 'NIFTY', 65)\n"
    n = patch(ROOT / "labs" / "services" / "strategy_report.py", [
        (source, source + "SOURCES['oi_unwind_nifty'] = Source('oi_unwind_nifty_trades', 'NIFTY', 65)\n"
                          "SOURCES['oi_unwind_sensex'] = Source('oi_unwind_sensex_trades', 'SENSEX', 20)\n"),
    ])
    print(f"strategy_report.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
