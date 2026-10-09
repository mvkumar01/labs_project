"""One-shot wiring of the crypto cross-sectional paper book into the labs dashboard and the paper loop.

The three files it edits (labs/ui/routes.py, templates/live_strategy.html,
pa_paper_tracker_loop.py) carry other sessions' uncommitted work, so the edits are small anchored
insertions next to the Infosys / TCS pair wiring (dashboard) and the BTC 24/7 block (loop): every
anchor is asserted, a near-miss fails instead of writing into the wrong place, and re-running is
a no-op.
`python scripts/patch_crypto_xs_ui.py [root]` patches the files under `root` (default: this repo).
Run scripts/patch_infy_tcs_pair_ui.py first (its lines are the anchors).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # Crypto cross-sectional taker imbalance (paper, Binance archive): its own ledger and tab.
        if active_live_tab == "crypto_xs":
            try:
                from labs.engine.crypto_xs_tracker import tab_data as crypto_xs_tab_data
                crypto_xs_rows, crypto_xs_book, crypto_xs_stats = crypto_xs_tab_data(
                    conn, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    crypto_xs_stats = {"error": str(exc)}

'''

TEMPLATE_PANEL = """    {% elif active_live_tab == 'crypto_xs' %}
    {% include '_crypto_xs.html' %}

"""

LOOP_BLOCK = '''        # The crypto cross-sectional book reads Binance's public archive, published about a day
        # late. The call only starts a background check when one is due, so it never holds the loop.
        try:
            res = run_crypto_xs_live(now)
            if res != last_log["crypto_xs"]:
                print(f"[paper-loop:crypto_xs] {now.strftime('%H:%M')} {res}", flush=True)
                last_log["crypto_xs"] = res
        except Exception as exc:
            print(f"[paper-loop:crypto_xs] error: {type(exc).__name__}: {exc}", flush=True)
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
    tabs = '    "infy_tcs_pair": "INFY / TCS pair",\n'
    vars_line = "    infy_tcs_rows, infy_tcs_trades, infy_tcs_stats = [], [], {}\n"
    pair_comment = "        # Infosys / TCS divergence pair (paper, daily bars): its own ledger and tab.\n"
    kwargs_line = "        infy_tcs_stats=infy_tcs_stats,\n"
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "crypto_xs": "Crypto taker imbalance",\n'),
        (vars_line, vars_line + "    crypto_xs_rows, crypto_xs_book, crypto_xs_stats = [], [], {}\n"),
        (pair_comment, ROUTES_QUERY + pair_comment),
        (kwargs_line, kwargs_line + "        crypto_xs_rows=crypto_xs_rows,\n"
                                    "        crypto_xs_book=crypto_xs_book,\n"
                                    "        crypto_xs_stats=crypto_xs_stats,\n"),
    ])
    print(f"routes.py: {n} edits applied")

    panel_anchor = "    {% elif active_live_tab == 'infy_tcs_pair' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [
        (panel_anchor, TEMPLATE_PANEL + panel_anchor),
    ])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.btc_rsi_roc_tracker import run_live as run_btc_rsi_roc_live\n"
    log_key = '        "btc_rsi_roc": None,\n'
    btc_error = '            print(f"[paper-loop:btc_rsi_roc] error: {type(exc).__name__}: {exc}", flush=True)\n'
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.crypto_xs_tracker import run_live as run_crypto_xs_live\n"),
        (log_key, log_key + '        "crypto_xs": None,\n'),
        (btc_error, btc_error + LOOP_BLOCK),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
