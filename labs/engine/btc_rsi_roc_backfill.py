"""Bounded backfill for the paper-only BTCUSDT RSI/ROC book.

Every call pulls at most ``limit`` pages of 1,000 one-minute klines (oldest missing first,
from the fixed replay anchor); once the store reaches the last closed minute it replays and
rewrites the ledger from BOOK_START. The tab's button calls this until ``remaining`` is 0.

    python3 -m labs.engine.btc_rsi_roc_backfill        # run to completion
"""
from __future__ import annotations

import math
from datetime import datetime

from labs.engine.btc_rsi_roc_tracker import IST, run_live

DEFAULT_START = "2026-06-01"          # informational: the book's first entry date


def run_backfill(start_date: str | None = None, end_date: str | None = None,
                 limit: int = 20) -> dict:
    """``start_date``/``end_date`` are accepted for the shared backfill route and ignored:
    the replay window is fixed so the ledger stays deterministic."""
    try:
        res = run_live(datetime.now(IST), max_pages=max(1, int(limit)))
    except Exception as exc:                                        # noqa: BLE001
        return {"done": [], "remaining": 0, "errors": {"btc": f"{type(exc).__name__}: {exc}"}}
    done = [f"page {k + 1}" for k in range(int(res.get("pages", 0) or 0))]
    if res.get("status") == "backfilling":
        # a rough count, for the progress text: one page is 1,000 minutes
        remaining = max(1, math.ceil(int(limit) / 2))
        return {"done": done or ["page"], "remaining": remaining, "errors": {}}
    return {"done": done or ["replay"], "remaining": 0, "errors": {}, "result": res}


if __name__ == "__main__":
    import json
    while True:
        out = run_backfill(limit=50)
        print(json.dumps({k: v for k, v in out.items() if k != "done"}, default=str), flush=True)
        if out["errors"] or not out["remaining"]:
            break
