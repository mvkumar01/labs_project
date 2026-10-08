"""One-shot wiring of the CRUDEOILM "consistent combination" paper book into the labs dashboard and
the paper loop.

The three files it edits (labs/ui/routes.py, templates/live_strategy.html,
pa_paper_tracker_loop.py) carry other sessions' uncommitted work, so the edits are small anchored
insertions: every anchor is asserted, a near-miss fails instead of writing into the wrong place,
and re-running is a no-op. `python scripts/patch_crudem_combo_ui.py [root]` patches the files
under `root` (default: this repo). Run scripts/patch_proposer_px_ui.py first (its tab is an anchor).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # CRUDEOILM consistent six-member combination (paper): its own ledger and tab.
        if active_live_tab == "crudem_combo":
            try:
                from labs.engine.crudem_combo_tracker import tab_data as crudem_combo_tab_data
                crudem_combo_rows, crudem_combo_trades, crudem_combo_stats = crudem_combo_tab_data(
                    conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    crudem_combo_stats = {"error": str(exc)}

'''

ROUTES_ENDPOINT = '''@labs_bp.route("/api/crudem_combo/backfill", methods=["POST"])
def crudem_combo_backfill():
    """Replay sessions of the paper-only CRUDEOILM consistent combination that have no final row."""
    from labs.engine.crudem_combo_tracker import PAPER_START, run_backfill
    try:
        limit = min(max(int(request.args.get("limit", 10)), 1), 30)
    except (TypeError, ValueError):
        limit = 10
    try:
        return jsonify(run_backfill(
            start_date=request.args.get("start", PAPER_START),
            end_date=request.args.get("end"),
            limit=limit,
            rebuild=request.args.get("rebuild", "0") == "1",
        ))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


'''

TEMPLATE_PANEL = """    {% elif active_live_tab == 'crudem_combo' %}
    {% include '_crudem_combo.html' %}

"""

TEMPLATE_SCRIPT = """{% if active_live_tab == 'crudem_combo' %}
<script>
  (function () {
    const button = document.getElementById('crudem-combo-backfill');
    const status = document.getElementById('crudem-combo-backfill-status');
    if (!button) return;
    button.addEventListener('click', async () => {
      button.disabled = true;
      let completed = 0;
      try {
        while (true) {
          status.textContent = 'Replaying... ' + completed + ' sessions done';
          const response = await fetch('/labs/api/crudem_combo/backfill?limit=5', {method: 'POST'});
          const payload = await response.json();
          if (!response.ok || payload.error) {
            throw new Error(payload.error || ('HTTP ' + response.status));
          }
          completed += payload.done.length;
          const errors = Object.keys(payload.errors || {});
          if (errors.length) {
            status.textContent = 'Done ' + completed + '; ' + errors[0] + ': ' + payload.errors[errors[0]];
            button.disabled = false;
            break;
          }
          if (!payload.remaining) {
            window.location.reload();
            break;
          }
        }
      } catch (error) {
        status.textContent = 'Replay failed: ' + error.message;
        button.disabled = false;
      }
    });
  })();
</script>
{% endif %}
"""

LOOP_BLOCK = '''            # CRUDEOILM consistent combination (paper); isolated like the other MCX books.
            try:
                res = run_crudem_combo_live(now)
                if res != last_log["crudem_combo"]:
                    print(f"[paper-loop:crudem_combo] {now.strftime('%H:%M')} {res}", flush=True)
                    last_log["crudem_combo"] = res
            except Exception as exc:
                print(f"[paper-loop:crudem_combo] error: {type(exc).__name__}: {exc}", flush=True)
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
            continue                       # already wired
        if text.count(anchor) != 1:
            raise SystemExit(f"{path.name}: anchor found {text.count(anchor)} times -> {anchor[:70]!r}")
        text = text.replace(anchor, replacement, 1)
        applied += 1
    path.write_text(text, encoding="utf-8", newline="")
    return applied


def main() -> int:
    tabs = '    "proposer_px": "Sensex Proposer + Renko",\n'
    vars_line = "    btc_rows, btc_trades, btc_stats = [], [], {}\n"
    btc_comment = "        # BTCUSDT RSI/ROC short (paper, 24/7): its own ledger and tab.\n"
    kwargs_line = "        btc_stats=btc_stats,\n"
    theta_route = '@labs_bp.route("/api/theta_straddle/backfill", methods=["POST"])\n'
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "crudem_combo": "CRUDEOILM Combo (6 rules)",\n'),
        (vars_line, vars_line + "    crudem_combo_rows, crudem_combo_trades, crudem_combo_stats = [], [], {}\n"),
        (btc_comment, ROUTES_QUERY + btc_comment),
        (kwargs_line, kwargs_line + "        crudem_combo_rows=crudem_combo_rows,\n"
                                    "        crudem_combo_trades=crudem_combo_trades,\n"
                                    "        crudem_combo_stats=crudem_combo_stats,\n"),
        (theta_route, ROUTES_ENDPOINT + theta_route),
    ])
    print(f"routes.py: {n} edits applied")

    panel_anchor = "    {% elif active_live_tab == 'btc_rsi_roc' %}\n"
    script_anchor = "{% if active_live_tab in ['theta_straddle', 'theta_iron_fly'] %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [
        (panel_anchor, TEMPLATE_PANEL + panel_anchor),
        (script_anchor, TEMPLATE_SCRIPT + script_anchor),
    ])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.crude_macd_st_tracker import run_live as run_crude_macd_st_live\n"
    log_key = '        "crude_macd_st": None,\n'
    mcx_if = "        if _in_mcx_session(now):\n"
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.crudem_combo_tracker import run_live as run_crudem_combo_live\n"),
        (log_key, log_key + '        "crudem_combo": None,\n'),
        (mcx_if, mcx_if + LOOP_BLOCK),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
