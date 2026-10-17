"""One-shot wiring of the "Risk core (trend x vol)" paper book (risk_core) into the labs dashboard, the paper
loop and the Overview cards.

The files it edits (labs/ui/routes.py, templates/live_strategy.html, pa_paper_tracker_loop.py,
labs/services/book_overview_more.py) can carry other sessions' work, so the edits are small anchored
insertions next to the "INFY / TCS pair" wiring: every anchor is asserted, a near-miss fails instead of
writing into the wrong place, and re-running is a no-op.
`python scripts/patch_risk_core_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # Risk core, trend x vol (paper, daily, five markets): its own ledger and tab.
        if active_live_tab == "risk_core":
            try:
                from labs.engine.risk_core_tracker import tab_data as risk_core_tab_data
                risk_core_months, risk_core_years, risk_core_stats = risk_core_tab_data(conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    risk_core_stats = {"error": str(exc)}

'''

ROUTES_ENDPOINT = '''@labs_bp.route("/api/risk_core/refresh", methods=["POST"])
def risk_core_refresh():
    """Fetch the latest daily inputs and rebuild the paper-only Risk core (trend x vol) book."""
    from labs.engine.risk_core_tracker import run_day
    try:
        return jsonify(run_day(force=True))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


'''

TEMPLATE_PANEL = """    {% elif active_live_tab == 'risk_core' %}
    {% include '_risk_core.html' %}
"""

TEMPLATE_SCRIPT = """{% if active_live_tab == 'risk_core' %}
<script>
  (function () {
    const button = document.getElementById('risk-core-refresh');
    const status = document.getElementById('risk-core-refresh-status');
    if (!button) return;
    button.addEventListener('click', async () => {
      button.disabled = true;
      status.textContent = 'Fetching daily inputs and rebuilding...';
      try {
        const response = await fetch('/labs/api/risk_core/refresh', {method: 'POST'});
        const payload = await response.json();
        if (!response.ok || payload.error) {
          throw new Error(payload.error || ('HTTP ' + response.status));
        }
        window.location.reload();
      } catch (error) {
        status.textContent = 'Refresh failed: ' + error.message;
        button.disabled = false;
      }
    });
  })();
</script>
{% endif %}
"""

CARD_FN = '''def _risk_core(conn, today: str) -> dict:
    from labs.engine.risk_core_tracker import CAPITAL, summary
    s = summary(conn)
    if not s:
        raise sqlite3.OperationalError("risk_core not built yet")
    rows = _rows(conn, "SELECT trade_date, net_rs FROM risk_core_days ORDER BY trade_date")
    held = " · ".join(f"{x['label']} {x.get('held') or 0:.2f}" for x in s["sleeves"])
    r, f = s["research"], s["forward"]
    note = (f"Research to {r['to']}: Rs {r['net_rs']:,.0f}, worst fall {100 * r['worst_fall']:.1f}%. "
            f"Forward from {f['from']}: Rs {f['net_rs']:,.0f}, worst fall {100 * f['worst_fall']:.1f}%. "
            f"Rolling 2-year Sharpe {s['rolling_2y_sharpe']:.2f}. Protection {s['status']} "
            f"(broken beyond {100 * s['broken_at']:.0f}%). Through {s['through']}.")
    return _card("risk_core", MORE_BOOKS["risk_core"], [{"trade_date": x["trade_date"], "net": _f(x["net_rs"])} for x in rows],
                 today, capital=CAPITAL, position=f"Held {held}", note=note)


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
    tabs = '    "infy_tcs_pair": "INFY / TCS pair",\n'
    vars_line = "    infy_tcs_rows, infy_tcs_trades, infy_tcs_stats = [], [], {}\n"
    comment = "        # Infosys / TCS divergence pair (paper, daily bars): its own ledger and tab.\n"
    kwargs_line = "        infy_tcs_stats=infy_tcs_stats,\n"
    route = '@labs_bp.route("/api/infy_tcs_pair/refresh", methods=["POST"])\n'
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "risk_core": "Risk core · trend × vol",\n'),
        (vars_line, vars_line + "    risk_core_months, risk_core_years, risk_core_stats = [], [], {}\n"),
        (comment, ROUTES_QUERY + comment),
        (kwargs_line, kwargs_line + "        risk_core_months=risk_core_months,\n"
                                    "        risk_core_years=risk_core_years,\n"
                                    "        risk_core_stats=risk_core_stats,\n"),
        (route, ROUTES_ENDPOINT + route),
    ])
    print(f"routes.py: {n} edits applied")

    panel = "    {% elif active_live_tab == 'infy_tcs_pair' %}\n"
    script = "{% if active_live_tab == 'infy_tcs_pair' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [
        (panel, TEMPLATE_PANEL + panel),
        (script, TEMPLATE_SCRIPT + script),
    ])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.infy_tcs_pair_tracker import run_day as run_infy_tcs_pair_day\n"
    log_key = '        "infy_tcs_pair": None,\n'
    runner = '                ("infy_tcs_pair", run_infy_tcs_pair_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.risk_core_tracker import run_day as run_risk_core_day\n"),
        (log_key, log_key + '        "risk_core": None,\n'),
        (runner, runner + '                ("risk_core", run_risk_core_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")

    meta = '    "infy_tcs_pair": {"label": "INFY / TCS pair", "instrument": "INFY and TCS shares", "size": "Rs 10 lakh a leg"},\n'
    builder = '        "infy_tcs_pair": lambda: _pair(conn, today),\n'
    fn_anchor = "def _crypto(conn, today: str) -> dict:\n"
    n = patch(ROOT / "labs" / "services" / "book_overview_more.py", [
        (meta, meta + '    "risk_core": {"label": "Risk core · trend × vol", "instrument": "NIFTY, BANK NIFTY, BTC, ETH, crude",\n'
                      '                  "size": "Rs 10 lakh, five sleeves"},\n'),
        (builder, builder + '        "risk_core": lambda: _risk_core(conn, today),\n'),
        (fn_anchor, CARD_FN + fn_anchor),
    ])
    print(f"book_overview_more.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
