"""Alpha v2.13: v2.11 risk authority with the v2.12 spot overlay.

The v2.11 champion is replayed first as a shadow lifecycle. Entry-spot stops
and same-side recoveries may create extra economic segments only while that
lifecycle is active. A v2.11 exit always closes the active overlay segment and
cancels any pending recovery.
"""
from __future__ import annotations

import pandas as pd

from live.engine import champion_sim


def _local_naive(value) -> pd.Timestamp:
    return champion_sim._local_naive_timestamp(value)


def _next_open_fill(ohlc, bar_start, next_open_fallback):
    execution_ts = pd.Timestamp(bar_start) + pd.Timedelta(minutes=1)
    price = ohlc.get_open(execution_ts)
    if price is None and next_open_fallback is not None:
        fallback_ts, fallback_spot = next_open_fallback
        if _local_naive(fallback_ts).floor("min") == _local_naive(
            execution_ts
        ).floor("min"):
            try:
                price = float(fallback_spot)
            except (TypeError, ValueError):
                price = None
    return execution_ts, price


def _minute_bars(ohlc, date_str: str, *, start, stop, max_bar_ts):
    start = _local_naive(start)
    stop = _local_naive(stop) if stop is not None else None
    max_bar_ts = _local_naive(max_bar_ts) if max_bar_ts is not None else None
    rows = []
    for minute, values in ohlc.by_minute.items():
        if values is None or len(values) != 4:
            continue
        bar_ts = pd.Timestamp(f"{date_str} {minute}")
        if bar_ts < start:
            continue
        if stop is not None and bar_ts >= stop:
            continue
        if max_bar_ts is not None and bar_ts > max_bar_ts:
            continue
        rows.append(
            (
                bar_ts,
                float(values[1]),
                float(values[2]),
                float(values[3]),
            )
        )
    return sorted(rows, key=lambda row: row[0])


def _closed_segment(lifecycle: dict, *, entry_ts, exit_ts, entry_spot,
                    exit_spot, reason) -> dict:
    pos = lifecycle["pos"]
    pnl = (
        float(exit_spot) - float(entry_spot)
        if pos == "call"
        else float(entry_spot) - float(exit_spot)
    )
    return {
        "pnl": round(pnl, 2),
        "reason": str(reason or "v211_exit"),
        "pos": pos,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_alpha": lifecycle.get("entry_alpha"),
        "exit_alpha": lifecycle.get("exit_alpha"),
        "entry_spot": float(entry_spot),
        "signal_entry_spot": float(lifecycle["signal_entry_spot"]),
        "exit_spot": float(exit_spot),
        "entry_rule": lifecycle.get("entry_rule"),
        "tier": lifecycle.get("tier"),
    }


def _overlay_lifecycle(
    lifecycle: dict,
    ohlc,
    date_str: str,
    *,
    max_bar_ts=None,
    next_open_fallback=None,
) -> tuple[list[dict], dict | None]:
    """Apply the overlay inside one authoritative v2.11 holding window."""
    pos = lifecycle["pos"]
    barrier = float(lifecycle["signal_entry_spot"])
    fill_entry_spot = float(lifecycle["entry_spot"])
    segment_entry_ts = lifecycle["entry_ts"]
    lifecycle_exit_ts = lifecycle.get("exit_ts")
    active = True
    segments = []

    first_overlay_bar = pd.Timestamp(lifecycle["entry_ts"]) + pd.Timedelta(
        minutes=5
    )
    bars = _minute_bars(
        ohlc,
        date_str,
        start=first_overlay_bar,
        stop=lifecycle_exit_ts,
        max_bar_ts=max_bar_ts,
    )
    for bar_ts, high, low, close in bars:
        execution_ts, executable_spot = _next_open_fill(
            ohlc, bar_ts, next_open_fallback
        )
        if not champion_sim._event_is_executable_before_eod(
            date_str, execution_ts
        ):
            continue

        if active:
            hit = (pos == "call" and low <= barrier) or (
                pos == "put" and high >= barrier
            )
            if not hit:
                continue
            if executable_spot is None:
                break
            segments.append(
                _closed_segment(
                    lifecycle,
                    entry_ts=segment_entry_ts,
                    exit_ts=execution_ts,
                    entry_spot=fill_entry_spot,
                    exit_spot=close,
                    reason="ENTRY_SPOT_SL",
                )
            )
            active = False
            favourable = (pos == "call" and close > barrier) or (
                pos == "put" and close < barrier
            )
            if favourable and (
                lifecycle_exit_ts is None
                or _local_naive(execution_ts) < _local_naive(lifecycle_exit_ts)
            ):
                active = True
                fill_entry_spot = float(close)
                segment_entry_ts = execution_ts
            continue

        crossed = (pos == "call" and low <= barrier < close) or (
            pos == "put" and close < barrier <= high
        )
        if not crossed:
            continue
        if executable_spot is None:
            break
        if lifecycle_exit_ts is not None and _local_naive(
            execution_ts
        ) >= _local_naive(lifecycle_exit_ts):
            continue
        active = True
        fill_entry_spot = float(executable_spot)
        segment_entry_ts = execution_ts

    if lifecycle_exit_ts is not None:
        if active:
            segments.append(
                _closed_segment(
                    lifecycle,
                    entry_ts=segment_entry_ts,
                    exit_ts=lifecycle_exit_ts,
                    entry_spot=fill_entry_spot,
                    exit_spot=lifecycle["exit_spot"],
                    reason=lifecycle.get("reason"),
                )
            )
        return segments, None

    if not active:
        return segments, None
    return segments, {
        "side": "CALL" if pos == "call" else "PUT",
        "entry_ts": segment_entry_ts,
        "entry_spot": float(fill_entry_spot),
        "signal_entry_spot": barrier,
        "entry_alpha": lifecycle.get("entry_alpha"),
        "entry_rule": lifecycle.get("entry_rule"),
        "tier": lifecycle.get("tier"),
        "gap_direction": lifecycle.get("gap_direction"),
    }


def apply_overlay(
    base_trades: list[dict],
    base_open_state: dict | None,
    ohlc,
    date_str: str,
    *,
    max_bar_ts=None,
    next_open_fallback=None,
) -> tuple[list[dict], dict | None]:
    """Transform v2.11 lifecycle windows into v2.13 economic segments."""
    lifecycles = []
    for trade in base_trades:
        lifecycle = dict(trade)
        lifecycle["signal_entry_spot"] = float(
            trade.get("signal_entry_spot", trade["entry_spot"])
        )
        lifecycles.append(lifecycle)
    if base_open_state is not None:
        lifecycles.append(
            {
                "pos": str(base_open_state["side"]).lower(),
                "entry_ts": base_open_state["entry_ts"],
                "entry_spot": float(base_open_state["entry_spot"]),
                "signal_entry_spot": float(
                    base_open_state.get(
                        "signal_entry_spot", base_open_state["entry_spot"]
                    )
                ),
                "entry_alpha": base_open_state.get("entry_alpha"),
                "entry_rule": base_open_state.get("entry_rule"),
                "tier": base_open_state.get("tier"),
                "gap_direction": base_open_state.get("gap_direction"),
                "exit_ts": None,
            }
        )

    segments = []
    open_state = None
    for lifecycle in lifecycles:
        lifecycle_segments, lifecycle_open = _overlay_lifecycle(
            lifecycle,
            ohlc,
            date_str,
            max_bar_ts=max_bar_ts,
            next_open_fallback=next_open_fallback,
        )
        segments.extend(lifecycle_segments)
        if lifecycle_open is not None:
            open_state = lifecycle_open
    return segments, open_state


def simulate(
    adf,
    ce_map,
    pe_map,
    ohlc,
    date_str,
    day_use_trail,
    sgap,
    tier,
    weekday,
    regime,
    range_lo,
    range_hi,
    *,
    close_eod=True,
    return_state=False,
    entries_until_ts=None,
    next_open_fallback=None,
    **base_flags,
):
    """Replay v2.11, then apply the additive overlay to its lifecycles."""
    base_flags.pop("enable_entry_spot_recovery", None)
    _, base_trades, base_open_state = champion_sim.simulate(
        adf,
        ce_map,
        pe_map,
        ohlc,
        date_str,
        day_use_trail,
        sgap,
        tier,
        weekday,
        regime,
        range_lo,
        range_hi,
        enable_entry_spot_recovery=False,
        close_eod=close_eod,
        return_state=True,
        entries_until_ts=entries_until_ts,
        **base_flags,
    )
    max_bar_ts = None
    if adf is not None and not adf.empty:
        max_bar_ts = pd.Timestamp(adf.iloc[-1]["timestamp"]) + pd.Timedelta(
            minutes=4
        )
    segments, open_state = apply_overlay(
        base_trades,
        base_open_state,
        ohlc,
        date_str,
        max_bar_ts=max_bar_ts,
        next_open_fallback=next_open_fallback,
    )
    pnl = round(sum(float(segment["pnl"]) for segment in segments), 2)
    if return_state:
        return pnl, segments, open_state
    return pnl, segments
