"""One-shot edits for Alpha v2.14 E (v2.14 C that opens no position from 14:00) in the files that
carry other sessions' uncommitted work.

  live/engine/champion_sim.py     the `no_entry_from_minute` gate (default None = unchanged)
  labs/ui/routes.py               paper tab alpha_v214e
  templates/live_strategy.html    paper tab alpha_v214e
  pa_paper_tracker_loop.py        paper book in the loop
  labs/services/book_overview.py  paper book on the overview

Every edit is a small anchored replacement: the anchor must be found exactly once, a near-miss
fails instead of writing into the wrong place, and re-running is a no-op.
`python scripts/patch_alpha_v214e.py [root]` patches the files under `root` (default: this repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]


def patch(path: Path, edits: list[tuple[str, str]]) -> int:
    """Each edit is (old, new). Skipped when `new` is already in the file."""
    text = path.read_text(encoding="utf-8")
    applied = 0
    for old, new in edits:
        if new in text:
            continue                       # already applied
        if text.count(old) != 1:
            raise SystemExit(f"{path.name}: anchor found {text.count(old)} times -> {old[:70]!r}")
        text = text.replace(old, new, 1)
        applied += 1
    path.write_text(text, encoding="utf-8", newline="")
    return applied


SIM_CONSTANT = (
    "V214C_RENKO_REVERSAL = 2\n",
    "V214C_RENKO_REVERSAL = 2\n"
    "# Alpha v2.14 E: v2.14 C that opens no position from 14:00 (no_entry_from_minute).\n"
    "# Shared by the paper book and the live runner.\n"
    "V214E_NO_ENTRY_FROM_MINUTE = 14 * 60\n",
)
SIM_SIGNATURE = (
    "             entry_spot_renko_reversal=2,\n             entry_spot_recovery_trace=None,\n",
    "             entry_spot_renko_reversal=2,\n             no_entry_from_minute=None,\n"
    "             entry_spot_recovery_trace=None,\n",
)
SIM_DOC = (
    "    of every overlay re-entry, so pricing can move each one to the next snapshot.\n",
    "    of every overlay re-entry, so pricing can move each one to the next snapshot.\n"
    "\n"
    "    `no_entry_from_minute` (minutes from midnight; Alpha v2.14 E passes\n"
    "    V214E_NO_ENTRY_FROM_MINUTE = 14:00) stops every NEW position from that time:\n"
    "    an Alpha entry on a five-minute mark at or after it, and an overlay re-entry\n"
    "    whose one-minute candle closes at or after it (the order would be sent then).\n"
    "    A position already open keeps every one of its exits. None = no cut-off.\n",
)
SIM_HELPER = (
    "    i = 0\n    revisit = False\n    while True:\n",
    "    def reentry_closed(bar_start):\n"
    "        \"\"\"An overlay re-entry is ordered when its one-minute candle closes.\"\"\"\n"
    "        return (no_entry_from_minute is not None\n"
    "                and _bar_minutes_from_midnight(bar_start) + 1 >= no_entry_from_minute)\n"
    "\n"
    "    i = 0\n    revisit = False\n    while True:\n",
)
SIM_BAR = (
    "        bar_minutes = _bar_minutes_from_midnight(c[\"timestamp\"])\n",
    "        bar_minutes = _bar_minutes_from_midnight(c[\"timestamp\"])\n"
    "        entries_closed = (no_entry_from_minute is not None\n"
    "                          and bar_minutes >= no_entry_from_minute)\n",
)
SIM_FLAT_REENTRY = (
    "            confirmed_ts = None\n"
    "            for bts, bh, bl, bc in ohlc.get_1min_bar_closes(c[\"timestamp\"]):\n",
    "            confirmed_ts = None\n"
    "            for bts, bh, bl, bc in ohlc.get_1min_bar_closes(c[\"timestamp\"]):\n"
    "                if reentry_closed(bts):\n"
    "                    break\n",
)
SIM_TOUCH_REENTRY = (
    "                            active_at_end = False\n"
    "                            if favourable_close:\n",
    "                            active_at_end = False\n"
    "                            if favourable_close and not reentry_closed(bts):\n",
)
SIM_BAR_REENTRY = (
    "                            crossed = (\"u\" in _ev) if pos == \"call\" else (\"d\" in _ev)\n"
    "                        if crossed:\n",
    "                            crossed = (\"u\" in _ev) if pos == \"call\" else (\"d\" in _ev)\n"
    "                        if crossed and not reentry_closed(bts):\n",
)
SIM_ENTRY = (
    "        if pos is None and not partial_bar and not overlay_only:\n",
    "        if pos is None and not partial_bar and not overlay_only and not entries_closed:\n",
)

TEMPLATE_SUB = (
    "    {% elif active_live_tab == 'alpha_v214d' %}\n",
    "    {% elif active_live_tab == 'alpha_v214e' %}\n"
    "    <p class=\"sub\">v2.14 C (Renko 30) that opens no position from 14:00: no Alpha entry on the 14:00 mark or later,\n"
    "      no Renko re-entry once the clock reaches 14:00; a position already open keeps all its exits &middot;\n"
    "      paper twin of the live preset &ldquo;Alpha v2.14 E&rdquo;</p>\n"
    "    {% elif active_live_tab == 'alpha_v214d' %}\n",
)


def main() -> int:
    n = patch(ROOT / "live" / "engine" / "champion_sim.py", [
        SIM_CONSTANT, SIM_SIGNATURE, SIM_DOC, SIM_HELPER, SIM_BAR, SIM_FLAT_REENTRY,
        SIM_TOUCH_REENTRY, SIM_BAR_REENTRY, SIM_ENTRY,
    ])
    print(f"champion_sim.py: {n} edits applied")

    n = patch(ROOT / "labs" / "ui" / "routes.py", [
        ('    "alpha_v214d": "Alpha v2.14 D",\n',
         '    "alpha_v214d": "Alpha v2.14 D",\n    "alpha_v214e": "Alpha v2.14 E",\n'),
        ('        "alpha_v214d": "v2.14 D",\n',
         '        "alpha_v214d": "v2.14 D",\n        "alpha_v214e": "v2.14 E",\n'),
        ('"alpha_v214", "alpha_v214c", "alpha_v214d", "alpha_cpr"}:',
         '"alpha_v214", "alpha_v214c", "alpha_v214d", "alpha_v214e", "alpha_cpr"}:'),
    ])
    print(f"routes.py: {n} edits applied")

    n = patch(ROOT / "templates" / "live_strategy.html", [
        ("'alpha_v214c', 'alpha_v214d', 'alpha_cpr'] %}", "'alpha_v214c', 'alpha_v214d', 'alpha_v214e', 'alpha_cpr'] %}"),
        TEMPLATE_SUB,
    ])
    print(f"live_strategy.html: {n} edits applied")

    imp = "    from labs.engine.alpha_v214d_tracker import run_day as run_v214d_day\n"
    n = patch(ROOT / "pa_paper_tracker_loop.py", [
        (imp, imp + "    from labs.engine.alpha_v214e_tracker import run_day as run_v214e_day\n"),
        ('        "alpha_v214d": None,\n', '        "alpha_v214d": None,\n        "alpha_v214e": None,\n'),
        ('                ("alpha_v214d", run_v214d_day),\n',
         '                ("alpha_v214d", run_v214d_day),\n                ("alpha_v214e", run_v214e_day),\n'),
    ])
    print(f"pa_paper_tracker_loop.py: {n} edits applied")

    card = '    "alpha_v214d": {"label": "Alpha v2.14 D", "instrument": "NIFTY options", "size": "1 lot"},\n'
    build = '            conn, "alpha_v214d", "alpha_v214d_daily", "alpha_v214d_trades", today),\n'
    n = patch(ROOT / "labs" / "services" / "book_overview.py", [
        (card, card + '    "alpha_v214e": {"label": "Alpha v2.14 E", "instrument": "NIFTY options", "size": "1 lot"},\n'),
        (build, build + '        "alpha_v214e": lambda: _option_buy_book(\n'
                        '            conn, "alpha_v214e", "alpha_v214e_daily", "alpha_v214e_trades", today),\n'),
    ])
    print(f"book_overview.py: {n} edits applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
