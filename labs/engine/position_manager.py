"""
Checks open positions against hard SL/target (1-min) and indicator exits (5-min).
Returns an exit_reason string if the position should be closed, else None.

Exit reason priority:
  1. ltp_sl        — option LTP dropped below entry_ltp - ltp_sl_pts
  2. ltp_target    — option LTP rose above entry_ltp + ltp_target_pts
  3. spot_sl       — spot moved against position by spot_sl_pts
  4. spot_target   — spot moved in favour by spot_target_pts
  5. indicator_exit — strategy exit rule fired on completed 5-min bar
  6. eod           — position still open at EOD_CUTOFF (handled by strategy_runner)
"""
from typing import Any

import pandas as pd


def check_exit(
    position: dict,
    current_ltp: float | None,
    current_spot: float,
    df_5min: pd.DataFrame,
    params: dict[str, Any],
    strategy,
    check_indicator: bool = False,
) -> str | None:
    """
    position    : dict with keys from positions table
    current_ltp : latest option LTP (None if unavailable)
    current_spot: latest index spot price
    df_5min     : completed 5-min bars with indicators
    params      : bot_params dict
    strategy    : Strategy instance
    check_indicator: True only when called on 5-min bar close
    """
    entry_ltp  = float(position["entry_ltp"])
    entry_spot = float(position["entry_spot"])
    side       = position["side"]   # CE or PE

    # 1. Hard LTP SL
    ltp_sl_pts = params.get("ltp_sl_pts")
    if ltp_sl_pts and current_ltp is not None:
        if current_ltp <= entry_ltp - ltp_sl_pts:
            return "ltp_sl"

    # 2. Hard LTP target
    ltp_target_pts = params.get("ltp_target_pts")
    if ltp_target_pts and current_ltp is not None:
        if current_ltp >= entry_ltp + ltp_target_pts:
            return "ltp_target"

    # 3. Hard spot SL
    spot_sl_pts = params.get("spot_sl_pts")
    if spot_sl_pts:
        spot_move = (current_spot - entry_spot) * (1 if side == "CE" else -1)
        if spot_move <= -spot_sl_pts:
            return "spot_sl"

    # 4. Hard spot target
    spot_target_pts = params.get("spot_target_pts")
    if spot_target_pts:
        spot_move = (current_spot - entry_spot) * (1 if side == "CE" else -1)
        if spot_move >= spot_target_pts:
            return "spot_target"

    # 5. Indicator exit (only on 5-min bar close)
    if check_indicator and not df_5min.empty:
        reason = strategy.exit_signal(df_5min, params, side)
        if reason:
            return "indicator_exit"

    return None
