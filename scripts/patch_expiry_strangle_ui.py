"""One-shot wiring of the "Far-OTM expiry strangle (NIFTY + SENSEX)" paper book (expiry_strangle) into the labs
dashboard, the paper loop and the Overview cards.

The files it edits (labs/ui/routes.py, templates/live_strategy.html, pa_paper_tracker_loop.py,
labs/services/book_overview_more.py) can carry other sessions' work, so the edits are small anchored
insertions next to the NIFTY expiry straddle / Tuesday put wiring: every anchor is asserted, a near-miss
fails instead of writing into the wrong place, and re-running is a no-op.
`python scripts/patch_expiry_strangle_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # Far-OTM expiry strangle, NIFTY + SENSEX (paper): its own ledger and tab.
        if active_live_tab == "expiry_strangle":
            try:
                from labs.engine.expiry_strangle_tracker import tab_data as expiry_strangle_tab_data
                expiry_strangle_rows, expiry_strangle_all, expiry_strangle_stats = expiry_strangle_tab_data(
                    conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    expiry_strangle_stats = {"error": str(exc)}

'''

ROUTES_ENDPOINT = '''@labs_bp.route("/api/expiry_strangle/backfill", methods=["POST"])
def expiry_strangle_backfill():
    """Replay bounded batches of expiries for the paper-only far-OTM expiry strangle."""
    from labs.engine.expiry_strangle_tracker import run_backfill
    try:
        limit = min(max(int(request.args.get("limit", 6)), 1), 20)
    except (TypeError, ValueError):
        limit = 6
    try:
        return jsonify(run_backfill(limit=limit, rebuild=request.args.get("rebuild", "0") == "1"))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


'''

TEMPLATE_PANEL = """    {% elif active_live_tab == 'expiry_strangle' %}
    {% include '_expiry_strangle.html' %}
"""

TEMPLATE_SCRIPT = """{% if active_live_tab == 'expiry_strangle' %}
<script>
  (function () {
    const button = document.getElementById('expiry-strangle-backfill');
    const status = document.getElementById('expiry-strangle-backfill-status');
    if (!button) return;
    button.addEventListener('click', async () => {
      button.disabled = true;
      let done = 0;
      try {
        while (true) {
          status.textContent = 'Replaying expiries... ' + done + ' done';
          const response = await fetch('/labs/api/expiry_strangle/backfill?limit=6', {method: 'POST'});
          const payload = await response.json();
          if (!response.ok || payload.error) throw new Error(payload.error || ('HTTP ' + response.status));
          done += payload.done.length;
          const errors = Object.keys(payload.errors || {});
          if (errors.length) { status.textContent = 'Stopped at ' + errors[0] + ': ' + payload.errors[errors[0]]; button.disabled = false; break; }
          if (!payload.remaining) { window.location.reload(); break; }
        }
      } catch (error) {
        status.textContent = 'Backfill failed: ' + error.message;
        button.disabled = false;
      }
    });
  })();
</script>
{% endif %}
"""

CARD_FN = '''def _expiry_strangle(conn, today: str) -> dict:
    from labs.engine.expiry_strangle_tracker import PRIMARY, retirement
    rows = _rows(conn, "SELECT und, expiry, variant, fill, status, period, net_rs, notional_rs, margin_rs, net_bps, entry_date "
                       "FROM expiry_strangle_events")
    if not rows:
        raise sqlite3.OperationalError("expiry_strangle not built yet")
    book = [r for r in rows if (r["variant"], r["fill"]) == PRIMARY and r["status"] == "closed"]
    by_day: dict = {}
    for r in book:
        by_day[r["expiry"]] = by_day.get(r["expiry"], 0.0) + _f(r["net_rs"])
    ret = retirement(rows)
    research = [r for r in book if r["period"] == "research"]
    forward = [r for r in book if r["period"] == "forward"]
    worst = min(book, key=lambda r: _f(r["net_rs"])) if book else None
    note = (f"Research {len(research)} events, Rs {sum(_f(r['net_rs']) for r in research):,.0f}; forward {len(forward)}, "
            f"Rs {sum(_f(r['net_rs']) for r in forward):,.0f}. "
            + (f"Worst {worst['und']} {worst['expiry']} Rs {_f(worst['net_rs']):,.0f} ({_f(worst['net_bps']):+.1f} bps). " if worst else "")
            + f"Retirement: {ret['verdict']} ({ret['n']}/{ret['of']} forward events).")
    margin = max((_f(r["margin_rs"]) for r in book), default=0.0)
    return _card("expiry_strangle", MORE_BOOKS["expiry_strangle"], [{"trade_date": d, "net": n} for d, n in sorted(by_day.items())],
                 today, capital=margin, position="Flat between expiries", trades=len(book), note=note)


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
    tabs = '    "nifty_expiry_sale": "NIFTY Expiry Straddle Sale",\n'
    vars_line = "    nifty_expiry_sale_rows, nifty_expiry_sale_books, nifty_expiry_sale_stats = [], [], {}\n"
    comment = "        # NIFTY expiry-day straddle sale (paper): its own ledger and tab.\n"
    kwargs_line = "        nifty_expiry_sale_stats=nifty_expiry_sale_stats,\n"
    route = '@labs_bp.route("/api/nifty_expiry_sale/backfill", methods=["POST"])\n'
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "expiry_strangle": "Expiry strangle · NIFTY + SENSEX",\n'),
        (vars_line, vars_line + "    expiry_strangle_rows, expiry_strangle_all, expiry_strangle_stats = [], [], {}\n"),
        (comment, ROUTES_QUERY + comment),
        (kwargs_line, kwargs_line + "        expiry_strangle_rows=expiry_strangle_rows,\n"
                                    "        expiry_strangle_all=expiry_strangle_all,\n"
                                    "        expiry_strangle_stats=expiry_strangle_stats,\n"),
        (route, ROUTES_ENDPOINT + route),
    ])
    print(f"routes.py: {n} edits applied")

    panel = "    {% elif active_live_tab == 'nifty_expiry_sale' %}\n"
    script = "{% if active_live_tab == 'nifty_expiry_sale' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [(panel, TEMPLATE_PANEL + panel), (script, TEMPLATE_SCRIPT + script)])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.nifty_expiry_sale_tracker import run_day as run_nifty_expiry_sale_day\n"
    log_key = '        "nifty_expiry_sale": None,\n'
    runner = '                ("nifty_expiry_sale", run_nifty_expiry_sale_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.expiry_strangle_tracker import run_day as run_expiry_strangle_day\n"),
        (log_key, log_key + '        "expiry_strangle": None,\n'),
        (runner, runner + '                ("expiry_strangle", run_expiry_strangle_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")

    meta = '    "nifty_tuesday_put": {"label": "NIFTY Tuesday put", "instrument": "NIFTY options, Tuesdays", "size": "1 lot"},\n'
    builder = '        "nifty_tuesday_put": lambda: _tuesday_put(conn, today),\n'
    fn_anchor = "def _tuesday_put(conn, today: str) -> dict:\n"
    n = patch(ROOT / "labs" / "services" / "book_overview_more.py", [
        (meta, meta + '    "expiry_strangle": {"label": "Far-OTM expiry strangle", "instrument": "NIFTY + SENSEX options, expiries",\n'
                      '                        "size": "1 lot a leg"},\n'),
        (builder, builder + '        "expiry_strangle": lambda: _expiry_strangle(conn, today),\n'),
        (fn_anchor, CARD_FN + fn_anchor),
    ])
    print(f"book_overview_more.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
