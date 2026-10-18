"""One-shot wiring of the crypto-only twin of the crypto taker-imbalance paper book (crypto_xs_c) into the labs
dashboard. The ledger itself is rebuilt by labs/engine/crypto_xs_tracker.refresh together with the main book,
so the paper loop needs no change; the Overview card is in labs/services/book_overview_more.py.

The files it edits (labs/ui/routes.py, templates/live_strategy.html) can carry other sessions' work, so the
edits are small anchored insertions next to the "Crypto taker imbalance" wiring: every anchor is asserted, a
near-miss fails instead of writing into the wrong place, and re-running is a no-op.
`python scripts/patch_crypto_xs_c_ui.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

ROUTES_QUERY = '''        # Crypto taker imbalance, crypto-only twin (paper): same panels, its own ledger and tab.
        if active_live_tab == "crypto_xs_c":
            try:
                from labs.engine.crypto_xs_tracker import CRYPTO_ONLY, tab_data as crypto_xs_tab_data
                crypto_xs_c_rows, crypto_xs_c_book, crypto_xs_c_stats = crypto_xs_tab_data(
                    conn, date_clause, date_params, book=CRYPTO_ONLY)
            except Exception as exc:
                if "no such table" not in str(exc):
                    crypto_xs_c_stats = {"error": str(exc)}

'''

TEMPLATE_PANEL = """    {% elif active_live_tab == 'crypto_xs_c' %}
    {% include '_crypto_xs_c.html' %}
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
    tabs = '    "crypto_xs": "Crypto taker imbalance",\n'
    vars_line = "    crypto_xs_rows, crypto_xs_book, crypto_xs_stats = [], [], {}\n"
    comment = "        # Crypto cross-sectional taker imbalance (paper, Binance archive): its own ledger and tab.\n"
    kwargs_line = "        crypto_xs_stats=crypto_xs_stats,\n"
    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        (tabs, tabs + '    "crypto_xs_c": "Crypto taker · crypto only",\n'),
        (vars_line, vars_line + "    crypto_xs_c_rows, crypto_xs_c_book, crypto_xs_c_stats = [], [], {}\n"),
        (comment, ROUTES_QUERY + comment),
        (kwargs_line, kwargs_line + "        crypto_xs_c_rows=crypto_xs_c_rows,\n"
                                    "        crypto_xs_c_book=crypto_xs_c_book,\n"
                                    "        crypto_xs_c_stats=crypto_xs_c_stats,\n"),
    ])
    print(f"routes.py: {n} edits applied")
    panel = "    {% elif active_live_tab == 'crypto_xs' %}\n"
    n = patch(ROOT / "templates" / "live_strategy.html", [(panel, TEMPLATE_PANEL + panel)])
    print(f"live_strategy.html: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
