"""One-shot wiring of the "Sensex V4" paper book (proposer_v4) into the labs dashboard, the paper loop
and the Overview cards.

The files it edits (labs/ui/routes.py, templates/live_strategy.html, pa_paper_tracker_loop.py,
labs/services/book_overview_more.py) can carry other sessions' work, so the edits are small anchored
insertions next to the "Sensex V3 3.5%" wiring: every anchor is asserted, a near-miss fails instead
of writing into the wrong place, and re-running is a no-op.
`python scripts/patch_proposer_v4_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # Sensex V4 (paper).
        if active_live_tab == "proposer_v4":
            try:
                from labs.engine.proposer_v4_book import tab_data as proposer_v4_tab_data
                proposer_v4_rows, proposer_v4_trades, proposer_v4_stats = (
                    proposer_v4_tab_data(conn, date_clause, date_params))
            except Exception as exc:
                if "no such table" not in str(exc):
                    proposer_v4_stats = {"error": str(exc)}

'''

ROUTES_ENDPOINT = '''@labs_bp.route("/api/proposer_v4/backfill", methods=["POST"])
def proposer_v4_backfill():
    """Backfill bounded batches of the Sensex V4 paper book."""
    from labs.engine.proposer_v4_book import DEFAULT_START, run_backfill
    try:
        limit = min(max(int(request.args.get("limit", 5)), 1), 20)
    except (TypeError, ValueError):
        limit = 5
    try:
        return jsonify(run_backfill(
            start_date=request.args.get("start", DEFAULT_START),
            end_date=request.args.get("end"),
            limit=limit,
            rebuild=request.args.get("rebuild", "0") == "1",
        ))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


'''

TEMPLATE_PANEL = """    {% elif active_live_tab == 'proposer_v4' %}
    {% include '_proposer_v4.html' %}
"""

TEMPLATE_SCRIPT = """{% if active_live_tab == 'proposer_v4' %}
<script>
  (function () {
    const status = document.getElementById('proposer-v4-backfill-status');
    async function run(button, rebuild) {
      button.disabled = true;
      let completed = 0, first = rebuild;
      try {
        while (true) {
          status.textContent = 'Backfilling... ' + completed + ' sessions completed';
          const response = await fetch('/labs/api/proposer_v4/backfill?limit=5' + (first ? '&rebuild=1' : ''),
                                       {method: 'POST'});
          first = false;
          const payload = await response.json();
          if (!response.ok || payload.error) throw new Error(payload.error || ('HTTP ' + response.status));
          completed += payload.done.length + (payload.unavailable || []).length;
          const errors = Object.keys(payload.errors || {});
          if (errors.length) {
            status.textContent = 'Completed ' + completed + '; stopped at ' + errors[0] + ': ' + payload.errors[errors[0]];
            button.disabled = false;
            break;
          }
          if (!payload.remaining) {
            status.textContent = 'Backfill complete: ' + completed + ' sessions';
            window.location.reload();
            break;
          }
        }
      } catch (error) {
        status.textContent = 'Backfill failed: ' + error.message;
        button.disabled = false;
      }
    }
    const refresh = document.getElementById('proposer-v4-backfill');
    const rebuild = document.getElementById('proposer-v4-rebuild');
    if (refresh) refresh.addEventListener('click', () => run(refresh, false));
    if (rebuild) rebuild.addEventListener('click', () => {
      if (window.confirm('Clear only this paper book and replay every session from 1 July?')) run(rebuild, true);
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
            continue
        if text.count(anchor) != 1:
            raise SystemExit(f"{path.name}: anchor found {text.count(anchor)} times -> {anchor[:70]!r}")
        text = text.replace(anchor, replacement, 1)
        applied += 1
    path.write_text(text, encoding="utf-8", newline="")
    return applied


def main() -> int:
    tabs = '    "proposer_v3_dt35": "Sensex V3 3.5%",\n'
    vars_line = "    proposer_v3_dt35_rows, proposer_v3_dt35_trades, proposer_v3_dt35_stats = [], [], {}\n"
    comment = "        # Independent SENSEX Proposer v3 3.5% paper bot.\n"
    kwargs_line = "        proposer_v3_dt35_stats=proposer_v3_dt35_stats,\n"
    route = '@labs_bp.route("/api/proposer_v3_dt35/backfill", methods=["POST"])\n'
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "proposer_v4": "Sensex V4",\n'),
        (vars_line, vars_line + "    proposer_v4_rows, proposer_v4_trades, proposer_v4_stats = [], [], {}\n"),
        (comment, ROUTES_QUERY + comment),
        (kwargs_line, kwargs_line + "        proposer_v4_rows=proposer_v4_rows,\n"
                                    "        proposer_v4_trades=proposer_v4_trades,\n"
                                    "        proposer_v4_stats=proposer_v4_stats,\n"),
        (route, ROUTES_ENDPOINT + route),
    ])
    print(f"routes.py: {n} edits applied")

    panel = "    {% elif active_live_tab == 'proposer_v3_dt35' %}\n"
    script = "{% if active_live_tab == 'proposer_v3_dt35' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [
        (panel, TEMPLATE_PANEL + panel),
        (script, TEMPLATE_SCRIPT + script),
    ])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.proposer_v3_dt35_book import run_day as run_proposer_v3_dt35_day\n"
    log_key = '        "proposer_v3_dt35": None,\n'
    runner = '                ("proposer_v3_dt35", run_proposer_v3_dt35_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.proposer_v4_book import run_day as run_proposer_v4_day\n"),
        (log_key, log_key + '        "proposer_v4": None,\n'),
        (runner, runner + '                ("proposer_v4", run_proposer_v4_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")

    meta = '    "proposer_v3_dt35": {"label": "Sensex V3 3.5%", "instrument": "SENSEX options", "size": "100 lots"},\n'
    builder = '        "proposer_v3_dt35": lambda: _proposer(conn, "proposer_v3_dt35", today),\n'
    n = patch(ROOT / "labs" / "services" / "book_overview_more.py", [
        (meta, meta + '    "proposer_v4": {"label": "Sensex V4", "instrument": "SENSEX options", "size": "100 lots"},\n'),
        (builder, builder + '        "proposer_v4": lambda: _proposer(conn, "proposer_v4", today),\n'),
    ])
    print(f"book_overview_more.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
