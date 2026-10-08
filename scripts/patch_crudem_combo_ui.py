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
    <h1>CRUDEOILM consistent combination (paper)</h1>
    <p class="sub">Strategy Tester v2, crude oil mini, saved combination c8: six intraday rules on the front-month
      future run as <strong>one position of one lot</strong> (10 barrels) &mdash; the first rule to signal takes the
      trade, the others wait until it closes. A signal is read at the close of a 1-minute bar and enters at the next
      bar's open; each rule has its own stop and target and a 30-bar cooldown; flat at the session's last bar.</p>

    <div class="formula">
      Evidence status: a <strong>fit</strong>, not a tested edge. The combination was chosen on every session from
      1 Jun to 6 Oct 2026 (back test, one lot, after costs: 136 trades, &#8377;46,967, 19 of 19 weeks up). This book
      starts on <strong>{{ crudem_combo_stats.paper_start or '2026-10-07' }}</strong>, the first session it was not
      chosen on. Nothing is tuned from what it shows. Costs: MCX futures charges plus one tick (&#8377;1) of slippage
      each side, about &#8377;80 a round trip.
    </div>

    {% if crudem_combo_stats.get('error') %}
      <p class="empty">Combination book unavailable ({{ crudem_combo_stats.error }}).</p>
    {% elif not crudem_combo_rows %}
      <p class="empty">No sessions recorded in this date range.</p>
      <button type="button" id="crudem-combo-backfill" class="btn-secondary">Replay from 7 October</button>
      <span id="crudem-combo-backfill-status" class="muted"></span>
    {% else %}
      <div class="cards">
        <div class="card"><div class="k">Net P&amp;L</div>
          <div class="v {{ 'green' if crudem_combo_stats.net_total>0 else ('red' if crudem_combo_stats.net_total<0 else '') }}">&#8377;{{ "{:,.0f}".format(crudem_combo_stats.net_total) }}</div>
          <div class="sub-k">closed trades, after &#8377;{{ "{:,.0f}".format(crudem_combo_stats.costs_total) }} charges and slippage</div></div>
        <div class="card"><div class="k">Trades</div>
          <div class="v">{{ crudem_combo_stats.trades }}</div>
          <div class="sub-k">{{ crudem_combo_stats.wins }} winners ({{ crudem_combo_stats.win_pct }}%) over {{ crudem_combo_stats.days }} sessions</div></div>
        <div class="card"><div class="k">Profit factor</div>
          <div class="v">{{ crudem_combo_stats.pf if crudem_combo_stats.pf is not none else '&mdash;' }}</div>
          <div class="sub-k">back test: about 3.5 in every period</div></div>
        <div class="card"><div class="k">Winning days</div>
          <div class="v">{{ crudem_combo_stats.win_days }} / {{ crudem_combo_stats.traded_days }}</div>
          <div class="sub-k">worst day &#8377;{{ "{:,.0f}".format(crudem_combo_stats.worst_day) }}</div></div>
        <div class="card"><div class="k">Largest fall</div>
          <div class="v">&#8377;{{ "{:,.0f}".format(crudem_combo_stats.max_dd) }}</div>
          <div class="sub-k">on cumulative net, trade by trade (back test: &#8377;2,221)</div></div>
        {% if crudem_combo_stats.open_trades %}
        <div class="card"><div class="k">Open position</div>
          <div class="v">{{ crudem_combo_stats.open_trades[0].direction }}</div>
          <div class="sub-k">rule {{ crudem_combo_stats.open_trades[0].cid }} from {{ crudem_combo_stats.open_trades[0].entry_ts[11:16] }}, marked at the last 1-minute close</div></div>
        {% endif %}
      </div>

      <div style="display:flex;gap:10px;align-items:center;margin:12px 0;flex-wrap:wrap">
        <button type="button" id="crudem-combo-backfill" class="btn-secondary">Replay missing sessions</button>
        <span id="crudem-combo-backfill-status" class="muted"></span>
        <span class="muted">Latest {{ crudem_combo_stats.latest.trade_date }}: {{ crudem_combo_stats.latest.status }}, through {{ (crudem_combo_stats.latest.through_ts or '')[11:16] }}, {{ crudem_combo_stats.latest.tradingsymbol or '' }}</span>
      </div>

      <h2 style="margin-top:24px">By rule</h2>
      <table class="perf">
        <thead><tr><th>Rule</th><th>Side</th><th>Exit</th><th>Trades</th><th>Winners</th><th>Net &#8377;</th></tr></thead>
        <tbody>
        {% for b in crudem_combo_stats.by_member %}
          <tr><td>{{ b.cid }}</td><td>{{ b.direction }}</td><td class="muted">{{ b.rule }}</td><td>{{ b.n }}</td><td>{{ b.wins }}</td>
            <td class="{{ 'green' if b.net_rs>0 else ('red' if b.net_rs<0 else '') }}">&#8377;{{ "{:,.0f}".format(b.net_rs) }}</td></tr>
        {% endfor %}
        </tbody>
      </table>
      <p class="note">Signals in this range:
        {% for k, v in crudem_combo_stats.outcomes.items() %}{{ v }} {{ {'taken': 'taken', 'position_held': 'skipped, another rule held the position',
           'member_in_trade': 'skipped, the rule was in its own trade', 'cooldown': 'inside the 30-bar cooldown',
           'gate': 'gate off', 'ineligible': 'no next bar to enter on', 'no_stop_distance': 'no stop distance'}.get(k, k) }}{{ '; ' if not loop.last else '.' }}{% endfor %}</p>

      <h2 style="margin-top:24px">Trades</h2>
      {% if crudem_combo_trades %}
      <table class="perf">
        <thead><tr><th>Date</th><th>#</th><th>Rule</th><th>Side</th><th>Signal</th><th>Entry &rarr; Exit</th><th>Entry</th>
          <th>Stop / Target</th><th>Exit</th><th>R</th><th>Gross &#8377;</th><th>Costs</th><th>Net &#8377;</th><th>Reason</th></tr></thead>
        <tbody>
        {% for t in crudem_combo_trades %}
          <tr>
            <td>{{ t.trade_date }}</td><td>{{ t.seq }}</td><td>{{ t.cid }}</td><td>{{ t.direction }}</td>
            <td class="muted">{{ t.signal_ts[11:16] }}</td>
            <td>{{ t.entry_ts[11:16] }} &rarr; {{ t.exit_ts[11:16] if t.exit_ts else 'open' }}</td>
            <td>{{ "{:,.1f}".format(t.entry_price) }}</td>
            <td class="muted">{{ "{:,.1f}".format(t.stop_price) }} / {{ "{:,.1f}".format(t.target_price) }}</td>
            <td>{{ "{:,.1f}".format(t.exit_price) }}</td>
            <td>{{ "%.2f"|format(t.r_multiple or 0) }}</td>
            <td class="{{ 'green' if (t.gross_rs or 0)>0 else ('red' if (t.gross_rs or 0)<0 else '') }}">&#8377;{{ "{:,.0f}".format(t.gross_rs or 0) }}</td>
            <td class="muted">&#8377;{{ "{:,.0f}".format((t.charges_rs or 0) + (t.slippage_rs or 0)) }}</td>
            <td class="{{ 'green' if (t.net_rs or 0)>0 else ('red' if (t.net_rs or 0)<0 else '') }}">&#8377;{{ "{:,.0f}".format(t.net_rs or 0) }}</td>
            <td>{% if t.status == 'open' %}<span class="amber">open</span>{% else %}{{ t.exit_reason }}{% endif %}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      {% else %}
        <p class="empty">No trades in this date range.</p>
      {% endif %}

      <h2 style="margin-top:24px">Daily history</h2>
      <table class="perf">
        <thead><tr><th>Date</th><th>Status</th><th>Contract</th><th>Valid bars</th><th>Signals</th><th>Trades</th>
          <th>Gross &#8377;</th><th>Costs</th><th>Net &#8377;</th></tr></thead>
        <tbody>
        {% for r in crudem_combo_rows %}
          <tr>
            <td>{{ r.trade_date }}</td>
            <td class="{{ 'amber' if r.status == 'live' else 'muted' }}" title="{{ r.error or '' }}">{{ r.status }}</td>
            <td>{{ r.tradingsymbol or '&mdash;' }}</td><td class="muted">{{ r.valid_bars if r.valid_bars is not none else '&mdash;' }}</td>
            <td>{{ r.n_signals }}</td>
            <td>{{ r.n_trades }}{% if r.open_trades %} <span class="amber">(open)</span>{% endif %}</td>
            <td>&#8377;{{ "{:,.0f}".format(r.gross_rs or 0) }}</td>
            <td class="muted">&#8377;{{ "{:,.0f}".format((r.charges_rs or 0) + (r.slippage_rs or 0)) }}</td>
            <td class="{{ 'green' if (r.net_rs or 0)>0 else ('red' if (r.net_rs or 0)<0 else '') }}">&#8377;{{ "{:,.0f}".format(r.net_rs or 0) }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      <p class="note">Paper only; this book never calls a broker order API. Every signal of every rule is stored
        (table <code>crudem_combo_signals</code>) so a session can be compared with the Strategy Tester's replay.</p>
    {% endif %}

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
