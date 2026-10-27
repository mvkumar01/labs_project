"""One-shot wiring of the crypto trend (crypto_xs_trend) and combined (crypto_xs_combo) paper books into the
labs dashboard, the paper loop, the Overview cards and the strategy report.

The files it edits (labs/ui/routes.py, templates/live_strategy.html, pa_paper_tracker_loop.py,
labs/services/book_overview_more.py, labs/services/strategy_report.py) can carry other sessions' work, so the
edits are small anchored insertions next to the crypto taker-imbalance wiring: every anchor is asserted, a
near-miss fails instead of writing into the wrong place, and re-running is a no-op.
`python scripts/patch_crypto_trend_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # Crypto trend (BTC/ETH) and taker + trend combined (paper): one template.
        if active_live_tab in ("crypto_xs_trend", "crypto_xs_combo"):
            try:
                from labs.engine.crypto_trend_tracker import tab_data as ct_tab_data
                ct_rows, ct_stats = ct_tab_data(conn, active_live_tab, date_clause, date_params)
            except Exception as exc:
                if "no such table" not in str(exc):
                    ct_stats = {"error": str(exc)}

'''

TEMPLATE_PANEL = """    {% elif active_live_tab in ['crypto_xs_trend', 'crypto_xs_combo'] %}
    {% include '_crypto_trend.html' %}
"""

LOOP_STEP = '''        try:
            res = run_crypto_trend_live(now)
            if res != last_log["crypto_trend"]:
                print(f"[paper-loop:crypto_trend] {now.strftime('%H:%M')} {res}", flush=True)
                last_log["crypto_trend"] = res
        except Exception as exc:
            print(f"[paper-loop:crypto_trend] error: {type(exc).__name__}: {exc}", flush=True)
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
    tabs = '    "crypto_xs_c": "Crypto taker · crypto only",\n'
    vars_line = "    crypto_xs_c_rows, crypto_xs_c_book, crypto_xs_c_stats = [], [], {}\n"
    comment = "        # Crypto taker imbalance, crypto-only twin (paper): same panels, its own ledger and tab.\n"
    kwargs_line = "        crypto_xs_c_stats=crypto_xs_c_stats,\n"
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "crypto_xs_trend": "Crypto trend · BTC/ETH",\n    "crypto_xs_combo": "Crypto taker + trend",\n'),
        (vars_line, vars_line + "    ct_rows, ct_stats = [], {}\n"),
        (comment, ROUTES_QUERY + comment),
        (kwargs_line, kwargs_line + "        ct_rows=ct_rows,\n        ct_stats=ct_stats,\n"),
    ])
    print(f"routes.py: {n} edits applied")

    panel = "    {% elif active_live_tab == 'crypto_xs_c' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [(panel, TEMPLATE_PANEL + panel)])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.crypto_xs_tracker import run_live as run_crypto_xs_live\n"
    log_key = '        "crypto_xs": None,\n'
    after = '''            print(f"[paper-loop:crypto_xs] error: {type(exc).__name__}: {exc}", flush=True)\n'''
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.crypto_trend_tracker import run_live as run_crypto_trend_live\n"),
        (log_key, log_key + '        "crypto_trend": None,\n'),
        (after, after + LOOP_STEP),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")

    meta = '    "crypto_xs": {"label": "Crypto taker imbalance", "instrument": "Binance USDT perpetuals", "size": "$100,000 simulated equity"},\n'
    builder = '        "crypto_xs_c": lambda: _crypto(conn, today, "crypto_xs_c"),\n'
    n = patch(ROOT / "labs" / "services" / "book_overview_more.py", [
        (meta, meta + '    "crypto_xs_trend": {"label": "Crypto trend · BTC/ETH", "instrument": "Binance BTC/ETH perpetuals", '
                      '"size": "$100,000 simulated equity"},\n'
                      '    "crypto_xs_combo": {"label": "Crypto taker + trend", "instrument": "Binance USDT perpetuals", '
                      '"size": "$100,000 simulated equity"},\n'),
        (builder, builder + '        "crypto_xs_trend": lambda: _crypto(conn, today, "crypto_xs_trend"),\n'
                            '        "crypto_xs_combo": lambda: _crypto(conn, today, "crypto_xs_combo"),\n'),
    ])
    print(f"book_overview_more.py: {n} edits applied")

    source = "    'crypto_xs_c': Source('crypto_xs_c_daily', 'Portfolio', date_column='decision_date', currency='USD', daily_only=True),\n"
    n = patch(ROOT / "labs" / "services" / "strategy_report.py", [
        (source, source + "    'crypto_xs_trend': Source('crypto_xs_trend_daily', 'Portfolio', date_column='decision_date', "
                          "currency='USD', daily_only=True),\n"
                          "    'crypto_xs_combo': Source('crypto_xs_combo_daily', 'Portfolio', date_column='decision_date', "
                          "currency='USD', daily_only=True),\n"),
    ])
    print(f"strategy_report.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
