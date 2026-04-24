"""
One-time recovery script to force-close all open paper positions.

Run on PythonAnywhere after deploying EOD close fixes when you want to clear
any leftover open positions immediately.
"""
import logging
from datetime import datetime
from pathlib import Path

import pytz

from auth.session_manager import get_kite
from labs.engine.data_loader import load_options_1min, load_spot_1min, latest_ltp
from labs.engine.paper_executor import close_position
from storage.db import get_conn, init_db

IST = pytz.timezone("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [eod-recovery] %(message)s",
)
log = logging.getLogger(__name__)


def run() -> None:
    init_db()
    kite = get_kite(force_refresh=True)
    now = datetime.now(IST)
    log.info(f"Starting one-time EOD recovery now={now.strftime('%Y-%m-%d %H:%M:%S')}")

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY trade_date, entry_time"
        ).fetchall()
        positions = [dict(r) for r in rows]
        log.info(f"Open positions found={len(positions)}")

        closed = 0
        for position in positions:
            try:
                spot_df = load_spot_1min(position["underlying"], position["trade_date"])
                options_df = load_options_1min(position["underlying"], position["trade_date"])
                current_spot = (
                    float(spot_df.iloc[-1]["close"])
                    if not spot_df.empty
                    else float(position["entry_spot"])
                )
                current_ltp = latest_ltp(options_df, position["symbol"])
                if current_ltp is None:
                    current_ltp = float(position["entry_ltp"])

                trade = close_position(position, current_ltp, current_spot, "eod", conn=conn)
                log.info(
                    f"Closed symbol={position['symbol']} qty={position['qty']} "
                    f"exit_price={current_ltp:.2f} pnl={trade['pnl_pts']:+.1f}pts "
                    f"gross_rs={trade['pnl_rs']:+.2f} net_rs={trade['net_pnl_rs']:+.2f}"
                )
                closed += 1
            except Exception as exc:
                log.error(
                    f"Failed to close symbol={position.get('symbol')} qty={position.get('qty')} err={exc}",
                    exc_info=True,
                )
                continue

        log.info(f"EOD recovery complete closed_positions={closed}")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
