"""One process for the MCX real-time DRY runners: the CRUDEOILM combination and the gold CCI short.

PHASE 0: there is no broker code path here or in either runner. Each runner is stepped in turn
and isolated from the other: an error in one is logged and the other still runs. They share one
always-on PythonAnywhere task (pa_crudem_runner.py) because the account has no free task slot.
"""
from __future__ import annotations

import logging
import time

from live import crudem_runner as cr
from live import gold_runner as gr
from storage.live_db import get_live_conn, init_live_db

log = logging.getLogger("live.mcx_dry_loop")


def run(max_cycles: int | None = None, clock=cr.now_ist, runners: list | None = None) -> None:
    init_live_db()
    cr.ensure_schema()
    gr.ensure_schema()
    runners = runners or [("crudem", cr.Runner()), ("gold", gr.Runner())]
    log.info("mcx_dry_loop boot | phase 0 dry run | runners=%s | crude start %s | gold start %s",
             [name for name, _ in runners], cr.START, gr.START)
    cycles = 0
    while True:
        now = clock()
        for name, runner in runners:
            try:
                conn = get_live_conn()
                try:
                    runner.step(now, conn)
                finally:
                    conn.close()
            except Exception as e:
                log.error("%s cycle error: %s: %s", name, type(e).__name__, str(e)[:200])
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        in_session = now.weekday() < 5 and cr.SESSION_OPEN <= now.time() < cr.SESSION_END
        time.sleep(cr.POLL_S if in_session else 30.0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
