"""One-shot wiring of the "Sensex Proposer + Renko" paper book into the labs dashboard and the
paper loop.

The three files it edits (labs/ui/routes.py, templates/live_strategy.html,
pa_paper_tracker_loop.py) carry other sessions' uncommitted work, so the edits are small anchored
insertions: every anchor is asserted, a near-miss fails instead of writing into the wrong place,
and re-running is a no-op. `python scripts/patch_proposer_px_ui.py [root]` patches the files under
`root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # SENSEX Proposer + price-action exit (paper): its own ledger and tab.
        if active_live_tab == "proposer_px":
            try:
                from labs.engine.proposer_px_view import tab_data as proposer_px_tab_data
                proposer_px_rows, proposer_px_trades, proposer_px_stats = proposer_px_tab_data(
                    conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    proposer_px_stats = {"error": str(exc)}

'''

ROUTES_ENDPOINT = '''@labs_bp.route("/api/proposer_px/backfill", methods=["POST"])
def proposer_px_backfill():
    """Backfill bounded batches of the paper-only SENSEX Proposer + price-action exit book."""
    from labs.engine.proposer_px_backfill import DEFAULT_START, run_backfill
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

TEMPLATE_PANEL = """    {% elif active_live_tab == 'proposer_px' %}
    <h1>SENSEX Proposer + Renko exit (paper)</h1>
    <p class="sub">The live SENSEX Proposer's own code, replayed on paper at {{ proposer_px_stats.lots or 100 }} lots:
      gap-rule regime, 5-class print every five minutes, nearest weekly ATM &#8723;200 ITM. Exits: &minus;30% premium
      floor, +100 / +40 spot points, opposite print above 0.45, 2.5% day target, 15:25 &mdash; plus the
      <strong>Renko exit</strong> (50-point bricks on 1-minute SENSEX closes; out when a brick prints against the
      trade after entry) and <strong>one loss per day</strong> (no new entry once a trade has closed at a loss).</p>

    <div class="formula">
      Fills inside a minute are estimates: the option is priced as its recorded quote at the minute plus a fitted
      delta times the SENSEX move along the 1-minute bar's open &rarr; extreme &rarr; extreme &rarr; close path, bought at
      +half the recorded spread and sold at &minus;half, then SENSEX charges. Results move by lakhs with the second of the
      minute the entry lands on, so read this as a paper comparison, not a forecast. Each session starts from the
      book the earlier ones left (recovery mode), so the book is built in date order.
    </div>

    {% if proposer_px_stats.get('error') %}
      <p class="empty">Proposer + Renko book unavailable ({{ proposer_px_stats.error }}).</p>
    {% elif not proposer_px_rows %}
      <p class="empty">No sessions recorded in this date range.</p>
      <button type="button" id="proposer-px-backfill" class="btn-secondary">Backfill from 1 June</button>
      <span id="proposer-px-backfill-status" class="muted"></span>
    {% else %}
      <div class="cards">
        <div class="card"><div class="k">Net P&amp;L</div>
          <div class="v {{ 'green' if proposer_px_stats.net_total>0 else ('red' if proposer_px_stats.net_total<0 else '') }}">&#8377;{{ "{:,.0f}".format(proposer_px_stats.net_total) }}</div>
          <div class="sub-k">&#8377;{{ "{:,.0f}".format(proposer_px_stats.net_per_lot) }} per lot, after &#8377;{{ "{:,.0f}".format(proposer_px_stats.charges_total) }} charges</div></div>
        <div class="card"><div class="k">Green days</div>
          <div class="v">{{ proposer_px_stats.green_pct }}%</div>
          <div class="sub-k">{{ proposer_px_stats.green_days }} green / {{ proposer_px_stats.red_days }} red of {{ proposer_px_stats.traded_days }} traded</div></div>
        <div class="card"><div class="k">Trades</div>
          <div class="v">{{ proposer_px_stats.trades }}</div>
          <div class="sub-k">over {{ proposer_px_stats.days }} sessions</div></div>
        <div class="card"><div class="k">Worst day</div>
          <div class="v red">&#8377;{{ "{:,.0f}".format(proposer_px_stats.worst_day) }}</div>
          <div class="sub-k">worst trade &#8377;{{ "{:,.0f}".format(proposer_px_stats.worst_trade) }}</div></div>
        <div class="card"><div class="k">Max drawdown</div>
          <div class="v">&#8377;{{ "{:,.0f}".format(proposer_px_stats.max_dd) }}</div>
          <div class="sub-k">on cumulative net, day by day</div></div>
      </div>

      <div style="display:flex;gap:10px;align-items:center;margin:12px 0;flex-wrap:wrap">
        <button type="button" id="proposer-px-backfill" class="btn-secondary">Refresh backfill</button>
        <button type="button" id="proposer-px-rebuild" class="btn-secondary">Rebuild from 1 June</button>
        <span id="proposer-px-backfill-status" class="muted"></span>
        <span class="muted">Latest {{ proposer_px_stats.latest.trade_date }}: {{ proposer_px_stats.latest.status }}, through {{ (proposer_px_stats.latest.through_ts or '')[11:16] }}</span>
      </div>

      <h2 style="margin-top:24px">By month</h2>
      <table class="perf">
        <thead><tr><th>Month</th><th>Sessions</th><th>Green</th><th>Red</th><th>Trades</th><th>Net &#8377;</th><th>Worst day</th></tr></thead>
        <tbody>
        {% for m in proposer_px_stats.months %}
          <tr><td>{{ m.month }}</td><td>{{ m.days }}</td><td>{{ m.green }}</td><td>{{ m.red }}</td><td>{{ m.trades }}</td>
            <td class="{{ 'green' if m.net_rs>0 else ('red' if m.net_rs<0 else '') }}">&#8377;{{ "{:,.0f}".format(m.net_rs) }}</td>
            <td class="muted">&#8377;{{ "{:,.0f}".format(m.worst_day) }}</td></tr>
        {% endfor %}
        </tbody>
      </table>

      <h2 style="margin-top:24px">By exit</h2>
      <table class="perf">
        <thead><tr><th>Exit rule</th><th>Trades</th><th>Net &#8377;</th></tr></thead>
        <tbody>
        {% for b in proposer_px_stats.by_rule %}
          <tr><td>{{ b.rule }}</td><td>{{ b.n }}</td>
            <td class="{{ 'green' if b.net_rs>0 else ('red' if b.net_rs<0 else '') }}">&#8377;{{ "{:,.0f}".format(b.net_rs) }}</td></tr>
        {% endfor %}
        </tbody>
      </table>

      <h2 style="margin-top:24px">Trades</h2>
      {% if proposer_px_trades %}
      <table class="perf">
        <thead><tr><th>Date</th><th>#</th><th>Signal</th><th>Contract</th><th>Entry &rarr; Exit</th><th>Spot in / out</th>
          <th>Buy</th><th>Sell</th><th>Gross &#8377;</th><th>Charges</th><th>Net &#8377;</th><th>Exit</th></tr></thead>
        <tbody>
        {% for t in proposer_px_trades %}
          <tr>
            <td>{{ t.trade_date }}</td><td>{{ t.seq }}</td><td>{{ t.signal }}</td>
            <td>{{ t.tradingsymbol or (t.strike ~ ' ' ~ t.side) }}</td>
            <td>{{ t.entry_ts[11:19] }} &rarr; {{ t.exit_ts[11:19] if t.exit_ts else 'open' }}</td>
            <td class="muted">{{ "%.0f"|format(t.entry_spot or 0) }} / {{ "%.0f"|format(t.exit_spot) if t.exit_spot is not none else '&mdash;' }}</td>
            <td>{{ "%.2f"|format(t.entry_price or 0) }}</td>
            <td>{{ "%.2f"|format(t.exit_price) if t.exit_price is not none else '&mdash;' }}</td>
            <td class="{{ 'green' if (t.gross_rs or 0)>0 else ('red' if (t.gross_rs or 0)<0 else '') }}">&#8377;{{ "{:,.0f}".format(t.gross_rs or 0) }}</td>
            <td class="muted">&#8377;{{ "{:,.0f}".format(t.charges_rs or 0) }}</td>
            <td class="{{ 'green' if (t.net_rs or 0)>0 else ('red' if (t.net_rs or 0)<0 else '') }}">&#8377;{{ "{:,.0f}".format(t.net_rs or 0) }}</td>
            <td>{% if not t.exit_ts %}<span class="amber">open</span>{% else %}{{ t.exit_rule }}{% endif %}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      {% else %}
        <p class="empty">No trades in this date range.</p>
      {% endif %}

      <h2 style="margin-top:24px">Daily history</h2>
      <table class="perf">
        <thead><tr><th>Date</th><th>Status</th><th>Regime</th><th>Gap</th><th>Expiry</th><th>Trades</th>
          <th>Day target</th><th>Gross &#8377;</th><th>Charges</th><th>Net &#8377;</th><th>Book before</th></tr></thead>
        <tbody>
        {% for r in proposer_px_rows %}
          <tr>
            <td>{{ r.trade_date }}</td>
            <td class="{{ 'amber' if r.status in ['live', 'unavailable'] else 'muted' }}" title="{{ r.error or '' }}">{{ r.status }}</td>
            <td>{{ r.regime or '&mdash;' }}</td>
            <td class="muted">{{ "%+.2f"|format(r.gap_pct) ~ '%' if r.gap_pct is not none else '&mdash;' }}</td>
            <td>{{ r.expiry_code or '&mdash;' }}</td>
            <td>{{ r.n_trades }}{% if r.n_losses %} <span class="muted">({{ r.n_losses }} loss)</span>{% endif %}</td>
            <td>{{ '&check;' if r.day_banked else '&mdash;' }}</td>
            <td>&#8377;{{ "{:,.0f}".format(r.gross_rs or 0) }}</td>
            <td class="muted">&#8377;{{ "{:,.0f}".format(r.charges_rs or 0) }}</td>
            <td class="{{ 'green' if (r.net_rs or 0)>0 else ('red' if (r.net_rs or 0)<0 else '') }}">&#8377;{{ "{:,.0f}".format(r.net_rs or 0) }}</td>
            <td class="muted">&#8377;{{ "{:,.0f}".format(r.book_gross_before or 0) }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      <p class="note">Paper only; this book never calls a broker order API. It trades the live bot's rules and signals
        (strategy <code>proposer_dt25_px</code>), so it is the like-for-like paper twin of that live variant.</p>
    {% endif %}

"""

TEMPLATE_SCRIPT = """{% if active_live_tab == 'proposer_px' %}
<script>
  (function () {
    const status = document.getElementById('proposer-px-backfill-status');
    async function run(button, rebuild) {
      button.disabled = true;
      let completed = 0, first = rebuild;
      try {
        while (true) {
          status.textContent = 'Backfilling... ' + completed + ' sessions completed';
          const response = await fetch('/labs/api/proposer_px/backfill?limit=5' + (first ? '&rebuild=1' : ''),
                                       {method: 'POST'});
          first = false;
          const payload = await response.json();
          if (!response.ok || payload.error) {
            throw new Error(payload.error || ('HTTP ' + response.status));
          }
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
    const refresh = document.getElementById('proposer-px-backfill');
    const rebuild = document.getElementById('proposer-px-rebuild');
    if (refresh) refresh.addEventListener('click', () => run(refresh, false));
    if (rebuild) rebuild.addEventListener('click', () => {
      if (window.confirm('Clear this paper book and replay every session from 1 June?')) run(rebuild, true);
    });
  })();
</script>
{% endif %}
"""


def patch(path: Path, edits: list[tuple[str, str]]) -> int:
    text = path.read_text(encoding="utf-8")
    applied = 0
    for anchor, replacement in edits:
        if replacement in text:
            continue                       # already wired
        if text.count(anchor) != 1:
            raise SystemExit(f"{path.name}: anchor found {text.count(anchor)} times -> {anchor[:70]!r}")
        text = text.replace(anchor, replacement, 1)
        applied += 1
    path.write_text(text, encoding="utf-8", newline="")
    return applied


def main() -> int:
    tabs = '    "sensex_alpha": "Sensex_alpha",\n}\n'
    vars_line = "    btc_rows, btc_trades, btc_stats = [], [], {}\n"
    btc_comment = "        # BTCUSDT RSI/ROC short (paper, 24/7): its own ledger and tab.\n"
    kwargs_line = "        btc_stats=btc_stats,\n"
    theta_route = '@labs_bp.route("/api/theta_straddle/backfill", methods=["POST"])\n'
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, '    "sensex_alpha": "Sensex_alpha",\n    "proposer_px": "Sensex Proposer + Renko",\n}\n'),
        (vars_line, vars_line + "    proposer_px_rows, proposer_px_trades, proposer_px_stats = [], [], {}\n"),
        (btc_comment, ROUTES_QUERY + btc_comment),
        (kwargs_line, kwargs_line + "        proposer_px_rows=proposer_px_rows,\n"
                                    "        proposer_px_trades=proposer_px_trades,\n"
                                    "        proposer_px_stats=proposer_px_stats,\n"),
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

    imp = "    from labs.engine.theta_iron_fly_tracker import run_day as run_theta_iron_fly_day\n"
    log_key = '        "theta_iron_fly": None,\n'
    runner = '                ("theta_iron_fly", run_theta_iron_fly_day),\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.proposer_px_tracker import run_day as run_proposer_px_day\n"),
        (log_key, log_key + '        "proposer_px": None,\n'),
        (runner, runner + '                ("proposer_px", run_proposer_px_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
