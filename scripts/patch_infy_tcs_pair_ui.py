"""One-shot wiring of the Infosys / TCS pair paper book into the labs dashboard and the paper loop.

The three files it edits (labs/ui/routes.py, templates/live_strategy.html,
pa_paper_tracker_loop.py) carry other sessions' uncommitted work, so the edits are small anchored
insertions next to the "Sensex Proposer v3" wiring: every anchor is asserted, a near-miss fails
instead of writing into the wrong place, and re-running is a no-op.
`python scripts/patch_infy_tcs_pair_ui.py [root]` patches the files under `root` (default: this
repo). Run scripts/patch_proposer_v3_ui.py first (its lines are the anchors).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # Infosys / TCS divergence pair (paper, daily bars): its own ledger and tab.
        if active_live_tab == "infy_tcs_pair":
            try:
                from labs.engine.infy_tcs_pair_tracker import tab_data as infy_tcs_tab_data
                infy_tcs_rows, infy_tcs_trades, infy_tcs_stats = infy_tcs_tab_data(
                    conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    infy_tcs_stats = {"error": str(exc)}

'''

ROUTES_ENDPOINT = '''@labs_bp.route("/api/infy_tcs_pair/refresh", methods=["POST"])
def infy_tcs_pair_refresh():
    """Fetch the latest daily candles and rebuild the paper-only Infosys / TCS pair book."""
    from labs.engine.infy_tcs_pair_tracker import run_day
    try:
        return jsonify(run_day(force=True))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


'''

TEMPLATE_PANEL = """    {% elif active_live_tab == 'infy_tcs_pair' %}
    {% include '_infy_tcs_pair.html' %}

"""

TEMPLATE_SCRIPT = """{% if active_live_tab == 'infy_tcs_pair' %}
<script>
  (function () {
    const button = document.getElementById('infy-tcs-refresh');
    const status = document.getElementById('infy-tcs-refresh-status');
    if (!button) return;
    button.addEventListener('click', async () => {
      button.disabled = true;
      status.textContent = 'Fetching daily candles...';
      try {
        const response = await fetch('/labs/api/infy_tcs_pair/refresh', {method: 'POST'});
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
            continue                       # already wired
        if text.count(anchor) != 1:
            raise SystemExit(f"{path.name}: anchor found {text.count(anchor)} times -> {anchor[:70]!r}")
        text = text.replace(anchor, replacement, 1)
        applied += 1
    path.write_text(text, encoding="utf-8", newline="")
    return applied


def main() -> int:
    tabs = '    "proposer_v3": "Sensex Proposer v3",\n'
    vars_line = "    proposer_v3_rows, proposer_v3_trades, proposer_v3_stats = [], [], {}\n"
    v3_comment = "        # SENSEX Proposer v3 - only with the day (paper): its own ledger and tab.\n"
    kwargs_line = "        proposer_v3_stats=proposer_v3_stats,\n"
    v3_route = '@labs_bp.route("/api/proposer_v3/backfill", methods=["POST"])\n'
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "infy_tcs_pair": "INFY / TCS pair",\n'),
        (vars_line, vars_line + "    infy_tcs_rows, infy_tcs_trades, infy_tcs_stats = [], [], {}\n"),
        (v3_comment, ROUTES_QUERY + v3_comment),
        (kwargs_line, kwargs_line + "        infy_tcs_rows=infy_tcs_rows,\n"
                                    "        infy_tcs_trades=infy_tcs_trades,\n"
                                    "        infy_tcs_stats=infy_tcs_stats,\n"),
        (v3_route, ROUTES_ENDPOINT + v3_route),
    ])
    print(f"routes.py: {n} edits applied")

    panel_anchor = "    {% elif active_live_tab == 'proposer_v3' %}\n"
    script_anchor = "{% if active_live_tab == 'proposer_v3' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [
        (panel_anchor, TEMPLATE_PANEL + panel_anchor),
        (script_anchor, TEMPLATE_SCRIPT + script_anchor),
    ])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.proposer_v3_book import run_day as run_proposer_v3_day\n"
    log_key = '        "proposer_v3": None,\n'
    runner = '                ("proposer_v3", run_proposer_v3_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.infy_tcs_pair_tracker import run_day as run_infy_tcs_pair_day\n"),
        (log_key, log_key + '        "infy_tcs_pair": None,\n'),
        (runner, runner + '                ("infy_tcs_pair", run_infy_tcs_pair_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
