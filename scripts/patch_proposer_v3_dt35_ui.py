"""Wire the independent "Sensex V3 3.5%" paper bot into shared dashboard/loop files.

The edited files may contain unrelated local work. Edits are anchored, asserted and idempotent so
this script can also be applied to a clean checkout when staging or deploying only these changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # Independent SENSEX Proposer v3 3.5% paper bot.
        if active_live_tab == "proposer_v3_dt35":
            try:
                from labs.engine.proposer_v3_dt35_book import tab_data as proposer_v3_dt35_tab_data
                proposer_v3_dt35_rows, proposer_v3_dt35_trades, proposer_v3_dt35_stats = proposer_v3_dt35_tab_data(
                    conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    proposer_v3_dt35_stats = {"error": str(exc)}

'''

ROUTES_ENDPOINT = '''@labs_bp.route("/api/proposer_v3_dt35/backfill", methods=["POST"])
def proposer_v3_dt35_backfill():
    """Backfill bounded batches of only the independent Sensex V3 3.5% paper bot."""
    from labs.engine.proposer_v3_dt35_book import DEFAULT_START, run_backfill
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

TEMPLATE_PANEL = """    {% elif active_live_tab == 'proposer_v3_dt35' %}
    {% include '_proposer_v3_dt35.html' %}

"""

TEMPLATE_SCRIPT = """{% if active_live_tab == 'proposer_v3_dt35' %}
<script>
  (function () {
    const status = document.getElementById('proposer-v3-dt35-backfill-status');
    async function run(button, rebuild) {
      button.disabled = true;
      let completed = 0, first = rebuild;
      try {
        while (true) {
          status.textContent = 'Backfilling... ' + completed + ' sessions completed';
          const response = await fetch('/labs/api/proposer_v3_dt35/backfill?limit=5' + (first ? '&rebuild=1' : ''),
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
    const refresh = document.getElementById('proposer-v3-dt35-backfill');
    const rebuild = document.getElementById('proposer-v3-dt35-rebuild');
    if (refresh) refresh.addEventListener('click', () => run(refresh, false));
    if (rebuild) rebuild.addEventListener('click', () => {
      if (window.confirm('Clear only the Sensex V3 3.5% paper book and replay every session from 1 June?')) run(rebuild, true);
    });
  })();
</script>
{% endif %}
"""


def patch(path: Path, edits: list[tuple[str, str]]) -> int:
    text = path.read_text(encoding="utf-8")
    applied = 0
    for anchor, replacement in edits:
        if replacement.startswith(anchor):
            added = replacement[len(anchor):]
        elif replacement.endswith(anchor):
            added = replacement[:-len(anchor)]
        else:
            raise SystemExit(f"{path.name}: edit must retain its anchor")
        if added in text:
            continue
        if text.count(anchor) != 1:
            raise SystemExit(f"{path.name}: anchor found {text.count(anchor)} times -> {anchor[:70]!r}")
        text = text.replace(anchor, replacement, 1)
        applied += 1
    path.write_text(text, encoding="utf-8", newline="")
    return applied


def main() -> int:
    routes = ROOT / "labs" / "ui" / "routes.py"
    n = patch(routes, [
        ('    "proposer_v3": "Sensex Proposer v3",\n',
         '    "proposer_v3": "Sensex Proposer v3",\n    "proposer_v3_dt35": "Sensex V3 3.5%",\n'),
        ('    proposer_v3_rows, proposer_v3_trades, proposer_v3_stats = [], [], {}\n',
         '    proposer_v3_rows, proposer_v3_trades, proposer_v3_stats = [], [], {}\n'
         '    proposer_v3_dt35_rows, proposer_v3_dt35_trades, proposer_v3_dt35_stats = [], [], {}\n'),
        ('        # SENSEX Proposer v3 - only with the day (paper): its own ledger and tab.\n',
         ROUTES_QUERY + '        # SENSEX Proposer v3 - only with the day (paper): its own ledger and tab.\n'),
        ('        proposer_v3_stats=proposer_v3_stats,\n',
         '        proposer_v3_stats=proposer_v3_stats,\n'
         '        proposer_v3_dt35_rows=proposer_v3_dt35_rows,\n'
         '        proposer_v3_dt35_trades=proposer_v3_dt35_trades,\n'
         '        proposer_v3_dt35_stats=proposer_v3_dt35_stats,\n'),
        ('@labs_bp.route("/api/proposer_v3/backfill", methods=["POST"])\n',
         ROUTES_ENDPOINT + '@labs_bp.route("/api/proposer_v3/backfill", methods=["POST"])\n'),
    ])
    print(f"routes.py: {n} edits applied")

    template = ROOT / "templates" / "live_strategy.html"
    n = patch(template, [
        ("    {% elif active_live_tab == 'proposer_v3' %}\n", TEMPLATE_PANEL + "    {% elif active_live_tab == 'proposer_v3' %}\n"),
        ("{% if active_live_tab == 'proposer_v3' %}\n", TEMPLATE_SCRIPT + "{% if active_live_tab == 'proposer_v3' %}\n"),
    ])
    print(f"live_strategy.html: {n} edits applied")

    loop = ROOT / "pa_paper_tracker_loop.py"
    n = patch(loop, [
        ("    from labs.engine.proposer_v3_book import run_day as run_proposer_v3_day\n",
         "    from labs.engine.proposer_v3_book import run_day as run_proposer_v3_day\n"
         "    from labs.engine.proposer_v3_dt35_book import run_day as run_proposer_v3_dt35_day\n"),
        ('        "proposer_v3": None,\n', '        "proposer_v3": None,\n        "proposer_v3_dt35": None,\n'),
        ('                ("proposer_v3", run_proposer_v3_day),\n',
         '                ("proposer_v3", run_proposer_v3_day),\n'
         '                ("proposer_v3_dt35", run_proposer_v3_dt35_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
