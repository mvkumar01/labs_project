"""One-shot wiring of the NIFTY Tuesday put paper book into the labs dashboard and the paper loop.

The three files it edits (labs/ui/routes.py, templates/live_strategy.html, pa_paper_tracker_loop.py)
carry other sessions' uncommitted work, so the edits are small anchored insertions next to the
"NIFTY Expiry Straddle Sale" wiring: every anchor is asserted, a near-miss fails instead of writing
into the wrong place, and re-running is a no-op.
`python scripts/patch_nifty_tuesday_put_ui.py [root]` patches the files under `root` (default: this
repo). Run scripts/patch_nifty_expiry_sale_ui.py first.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # NIFTY Tuesday put (paper): its own ledger and tab.
        if active_live_tab == "nifty_tuesday_put":
            try:
                from labs.engine.nifty_tuesday_put_tracker import tab_data as nifty_tuesday_put_tab_data
                nifty_tuesday_put_rows, nifty_tuesday_put_summaries, nifty_tuesday_put_stats = nifty_tuesday_put_tab_data(
                    conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    nifty_tuesday_put_stats = {"error": str(exc)}

'''

ROUTES_ENDPOINT = '''@labs_bp.route("/api/nifty_tuesday_put/backfill", methods=["POST"])
def nifty_tuesday_put_backfill():
    """Replay bounded batches of Tuesdays for the paper-only NIFTY Tuesday put."""
    from labs.engine.nifty_tuesday_put_tracker import DEFAULT_START, run_backfill
    try:
        limit = min(max(int(request.args.get("limit", 10)), 1), 40)
    except (TypeError, ValueError):
        limit = 10
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

TEMPLATE_PANEL = """    {% elif active_live_tab == 'nifty_tuesday_put' %}
    {% include '_nifty_tuesday_put.html' %}

"""

TEMPLATE_SCRIPT = """{% if active_live_tab == 'nifty_tuesday_put' %}
<script>
  (function () {
    const status = document.getElementById('nifty-tuesday-put-backfill-status');
    async function run(button, rebuild) {
      button.disabled = true;
      let replayed = 0, first = rebuild;
      try {
        while (true) {
          status.textContent = 'Backfilling... ' + replayed + ' Tuesdays replayed';
          const response = await fetch('/labs/api/nifty_tuesday_put/backfill?limit=10' + (first ? '&rebuild=1' : ''),
                                       {method: 'POST'});
          first = false;
          const payload = await response.json();
          if (!response.ok || payload.error) {
            throw new Error(payload.error || ('HTTP ' + response.status));
          }
          replayed += payload.done.length;
          const errors = Object.keys(payload.errors || {});
          if (errors.length) {
            status.textContent = 'Replayed ' + replayed + '; stopped at ' + errors[0] + ': ' + payload.errors[errors[0]];
            button.disabled = false;
            break;
          }
          if (!payload.remaining) {
            status.textContent = 'Backfill complete: ' + replayed + ' Tuesdays replayed';
            window.location.reload();
            break;
          }
        }
      } catch (error) {
        status.textContent = 'Backfill failed: ' + error.message;
        button.disabled = false;
      }
    }
    const refresh = document.getElementById('nifty-tuesday-put-backfill');
    const rebuild = document.getElementById('nifty-tuesday-put-rebuild');
    if (refresh) refresh.addEventListener('click', () => run(refresh, false));
    if (rebuild) rebuild.addEventListener('click', () => {
      if (window.confirm('Clear this paper book and replay every Tuesday from 2 September 2025?')) run(rebuild, true);
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
    tabs = '    "nifty_expiry_sale": "NIFTY Expiry Straddle Sale",\n'
    vars_line = "    nifty_expiry_sale_rows, nifty_expiry_sale_books, nifty_expiry_sale_stats = [], [], {}\n"
    sale_comment = "        # NIFTY expiry-day straddle sale (paper): its own ledger and tab.\n"
    kwargs_line = "        nifty_expiry_sale_stats=nifty_expiry_sale_stats,\n"
    sale_route = '@labs_bp.route("/api/nifty_expiry_sale/backfill", methods=["POST"])\n'
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "nifty_tuesday_put": "NIFTY Tuesday Put",\n'),
        (vars_line, vars_line + "    nifty_tuesday_put_rows, nifty_tuesday_put_summaries, nifty_tuesday_put_stats = [], [], {}\n"),
        (sale_comment, ROUTES_QUERY + sale_comment),
        (kwargs_line, kwargs_line + "        nifty_tuesday_put_rows=nifty_tuesday_put_rows,\n"
                                    "        nifty_tuesday_put_summaries=nifty_tuesday_put_summaries,\n"
                                    "        nifty_tuesday_put_stats=nifty_tuesday_put_stats,\n"),
        (sale_route, ROUTES_ENDPOINT + sale_route),
    ])
    print(f"routes.py: {n} edits applied")

    panel_anchor = "    {% elif active_live_tab == 'nifty_expiry_sale' %}\n"
    script_anchor = "{% if active_live_tab == 'nifty_expiry_sale' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [
        (panel_anchor, TEMPLATE_PANEL + panel_anchor),
        (script_anchor, TEMPLATE_SCRIPT + script_anchor),
    ])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.nifty_expiry_sale_tracker import run_day as run_nifty_expiry_sale_day\n"
    log_key = '        "nifty_expiry_sale": None,\n'
    runner = '                ("nifty_expiry_sale", run_nifty_expiry_sale_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.nifty_tuesday_put_tracker import run_day as run_nifty_tuesday_put_day\n"),
        (log_key, log_key + '        "nifty_tuesday_put": None,\n'),
        (runner, runner + '                ("nifty_tuesday_put", run_nifty_tuesday_put_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
