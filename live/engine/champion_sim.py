"""Self-contained, faithful port of the alphaIMB research champion engine
(`research/experiments/2026-05-26_v28_research_isolation/v79_v281_isolation.py::sim`).

This is the SAME exit/entry rule stack the validated research backtest runs:
Rule 1 / Rule 2 / Rule 3 (v7.10), v7.6 ALPHA_STALL, v7.7 PC400-DN PUT filter,
v7.7 PC400-DN CALL trail, v7.8 PC50 denom guard, v7.9 D2 wall-unwind override,
v7.11 drift-protective stop, PC400 spot trail, PC400 gap-UP CALL wall rejection.

Until 2026-06-19 the labs paper tracker only ran the per-bar AlphaSignalEngine
(Rules 1/2/3 + v7.8 + v7.11 + v22 trail). v7.6 / v7.7 / v7.9-D2 were referenced
in comments but never implemented in labs — which made /labs/live diverge from
research on PC400 gap-DN days (06-08, 06-11, …). This module closes that gap by
running the actual research loop, fed by labs' own (verified-equal) alpha plus
the OI maps and 1-min OHLC the rules need.

The alpha series itself is computed upstream (live.engine.alpha_hybrid) and is
already bit-identical to research; this module consumes it and applies the rules.

Inputs (all built by the caller, paper_strategy_tracker):
  adf      DataFrame[timestamp, alpha, d_pe_sum, d_ce_sum, denom, spot]
  ce_map   {timestamp -> {strike -> CE open-interest}}
  pe_map   {timestamp -> {strike -> PE open-interest}}
  ohlc     OHLC provider (get_spot, get_1min_bars) — 1-min high/low/close,
           sourced from alphaIMB nifty_1min_ohlc.csv where present and the
           shared-store 1-min spot (high=low=close=spot) otherwise — exactly
           mirroring the research module's fallback.
"""
from __future__ import annotations

import pandas as pd

# ── Strategy constants (verbatim from v79_v281_isolation.py) ─────────────────
VIX_TH = 17.0
TRAIL_ARM = 40
TRAIL_LOCK = 20
V211A_DN_PUT_TRAIL_ARM = 30
V211A_DN_PUT_TRAIL_RETRACE = 20
EOD_EXIT_MINUTE = 15 * 60 + 25

# v7.6 ALPHA_STALL (PC50 gap-UP CALL)
ALPHA_STALL_MIN_DURATION_MIN = 45
ALPHA_STALL_LOWER_ALPHA = 25
ALPHA_STALL_UPPER_ALPHA = 50
ALPHA_STALL_MAX_ALPHA_SEEN = 60

# v7.7 split filter (PC400 gap-DN PUT)
F5_SGAP_THRESHOLD = -150
VAC100_MIN_GAP = 100
VAC100_MAJOR_OI_THRESHOLD = 3_000_000
VAC100_BELOW_RANGE = 600

# v7.8 denom guard (PC50)
DENOM_GUARD_TIME_CUTOFF_MIN = 600  # 10:00 IST

# v7.9 D2 — wall-unwind override
D2_PCT_THRESHOLD = -8.0
D2_ALPHA_GATE = -30.0
D2_ALPHA_INVALIDATE = 0.0

# v7.10-partial — Rule 3
RULE3_LOWER_OPENING_ALPHA = -100.0
RULE3_UPPER_OPENING_ALPHA = -50.0
RULE3_INVALIDATE_ALPHA = 0.0

# v7.11 — drift protective
V711_DRIFT_ALPHA = -10.0
V711_CONFIRMATION_ALPHA = -70.0
V711_PROTECTIVE_STOP_BUFFER = 0.0

# v2.8 PC250 gap-UP spot TP/SL (dormant in the champion: flip flag is False)
V28_PC250_GAP_UP_TP_PTS = 60.0
V28_PC250_GAP_UP_SL_PTS = -30.0


# ── helpers (verbatim) ───────────────────────────────────────────────────────
def _bar_minutes_from_midnight(ts) -> int:
    s = str(ts)
    hm = s[11:16] if len(s) >= 16 else s
    try:
        h, m = map(int, hm.split(":"))
        return h * 60 + m
    except Exception:
        return 0


def exact_mark_cutoff(trade_date: str, now_ist) -> "pd.Timestamp | None":
    """Return the latest alpha mark eligible in a replay-to-now run.

    Alpha is a point-in-time OI snapshot at each five-minute mark, not a candle
    that must close five minutes later. Completed one-minute OHLC rows still
    govern the v2.12 spot-stop/recovery overlay independently.
    """
    if trade_date != now_ist.date().isoformat():
        return None
    return pd.Timestamp(now_ist).floor("min")


def _local_naive_timestamp(value) -> pd.Timestamp:
    """Normalize mixed archived/live timestamps for safe ordering."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return ts


def _after_minute_close(bar_start) -> pd.Timestamp:
    """Earliest causal execution mark after a labelled one-minute candle."""
    return pd.Timestamp(bar_start) + pd.Timedelta(minutes=1)


def _event_is_executable_before_eod(date_str: str, event_ts) -> bool:
    """Reject causal fills after the strategy's 15:25 square-off."""
    event = _local_naive_timestamp(event_ts)
    cutoff = pd.Timestamp(date_str) + pd.Timedelta(minutes=EOD_EXIT_MINUTE)
    return event <= cutoff


def find_wall_above(ce_at_ts, spot, max_dist=300):
    cands = [(s, oi) for s, oi in ce_at_ts.items()
             if s > spot and s <= spot + max_dist and oi > 0]
    if not cands:
        return None, 0
    cands.sort(key=lambda x: x[1], reverse=True)
    return cands[0]


def pc400_dn_put_should_skip(pe_at_ts, spot, range_lo, range_hi,
                             regime, sgap, weekday) -> bool:
    if weekday == "Thu":
        return regime == "WALL" and sgap <= F5_SGAP_THRESHOLD
    cands = [(s, oi) for s, oi in pe_at_ts.items()
             if range_lo <= s <= range_hi and oi > 0]
    if not cands:
        return False
    sorted_oi = sorted(cands, key=lambda x: -x[1])[:5]
    nearest_s, _ = min(sorted_oi, key=lambda x: abs(x[0] - spot))
    if abs(nearest_s - spot) <= 5:
        position = "AT"
    elif spot < nearest_s:
        position = "ABOVE"
    else:
        position = "BELOW"
    if position != "ABOVE":
        return False
    major_below = sorted(
        [s for s, oi in cands
         if s < spot
         and (spot - s) <= VAC100_BELOW_RANGE
         and oi >= VAC100_MAJOR_OI_THRESHOLD],
        reverse=True,
    )
    if not major_below:
        return True
    chain = [spot] + major_below
    gaps = [chain[i] - chain[i + 1] for i in range(len(chain) - 1)]
    return max(gaps) < VAC100_MIN_GAP


def d2_nearest_below_wall(pe_at_ts, spot, range_lo, range_hi):
    cands = [(s, oi) for s, oi in pe_at_ts.items()
             if s < spot
             and (spot - s) <= VAC100_BELOW_RANGE
             and oi >= VAC100_MAJOR_OI_THRESHOLD
             and range_lo <= s <= range_hi]
    if not cands:
        return None
    cands.sort(key=lambda x: spot - x[0])
    return int(cands[0][0])


class OHLC:
    """1-min spot accessor mirroring research's get_spot / get_1min_bars.

    `by_minute` maps "HH:MM" -> (open, high, low, close) for the trade_date.
    Built from alphaIMB's nifty_1min_ohlc.csv where available, else the
    shared-store 1-min spot column (open=high=low=close=spot) — the exact
    fallback the research module uses (v79_v281_isolation.py lines 85-104).
    """

    def __init__(self, by_minute: dict):
        self.by_minute = by_minute

    def day_open(self):
        o = self.by_minute.get("09:15")
        return float(o[0]) if o else None

    def get_spot(self, ts):
        for delta in (0, -1, 1, -2, 2):
            t = (pd.Timestamp(ts) + pd.Timedelta(minutes=delta)).strftime("%H:%M")
            if t in self.by_minute:
                return float(self.by_minute[t][3])  # close
        return None

    def get_open(self, ts):
        """Exact labelled one-minute open; never search adjacent candles."""
        candle = self.by_minute.get(pd.Timestamp(ts).strftime("%H:%M"))
        return float(candle[0]) if candle is not None else None

    def get_1min_bars(self, bar_ts):
        base = pd.Timestamp(bar_ts)
        out = []
        for m in range(5):
            t = (base + pd.Timedelta(minutes=m)).strftime("%H:%M")
            ohlc = self.by_minute.get(t)
            if ohlc is not None:
                out.append((float(ohlc[1]), float(ohlc[2])))  # high, low
        return out

    def get_1min_bar_closes(self, bar_ts):
        """Return (timestamp, high, low, close) for barrier-cross modelling."""
        base = pd.Timestamp(bar_ts)
        out = []
        for m in range(5):
            ts = base + pd.Timedelta(minutes=m)
            ohlc = self.by_minute.get(ts.strftime("%H:%M"))
            if ohlc is not None:
                out.append(
                    (ts, float(ohlc[1]), float(ohlc[2]), float(ohlc[3]))
                )
        return out


def simulate(adf, ce_map, pe_map, ohlc: OHLC, date_str, day_use_trail, sgap,
             tier, weekday, regime, range_lo, range_hi,
             enable_v76_alpha_stall=True,
             enable_v77_dn_put_filter=True,
             enable_v77_dn_call_trail=True,
             enable_v78_denom_guard=True,
             enable_v79_d2=True,
             enable_rule3_dn_put=True,
             enable_v711_drift_protective=True,
             enable_v211a_low_vix_dn_put_trail=False,
             enable_entry_spot_recovery=False,
             entry_spot_close_confirmed=False,
             # ── Alpha-CPR paper overlay (isolated; all default OFF) ──────────
             no_alpha_exits=False,
             cpr_levels=None,
             enable_cpr_sl=False,
             enable_cpr_tp=False,
             cpr_min_dist=0.0,
             close_eod=True, return_state=False,
             entries_until_ts=None):
    """Faithful port of v79_v281_isolation.sim() with the champion flag set.

    Returns (pnl_pts, trades) where each trade carries entry/exit timestamps,
    spots, alpha, rule tag and exit reason — enough for the paper tracker to
    price premiums and compute charges. pc250 gap-UP flip is dormant in the
    champion (flag False), so that branch is omitted here.

    `close_eod` (default True): square off any open position at the last bar
    (EOD semantics — backtest / EOD paper run). Set False for the live
    "replay-to-now" decider so the in-flight position is returned, not closed.
    `return_state` (default False): also return the open-position state dict
    (or None) as a third tuple element — the live decider's reconcile target.

    `entries_until_ts` (default None): if set, any bar with timestamp >
    entries_until_ts is a STOPS-ONLY partial bar — the in-progress 5-min bucket,
    evaluated for spot-based stops (entry-spot SL, trail, drift, PC250 spot TP/SL)
    and entry-spot recovery RE-ENTRY on its completed 1-min sub-bars, but NOT for
    new alpha-crossover entries or alpha-based exits (which stay on the 5-min
    grid). When None (every backtest / research / EOD caller) the partial-bar
    guards are inert, so completed-day output is byte-for-byte unchanged.

    `enable_v211a_low_vix_dn_put_trail` is an isolated Alpha v2.11A paper
    overlay. Its caller enables it only when the resolved opening VIX is present
    and below 17. It changes PC400 gap-DOWN PUT exits from Alpha-only to a
    30-point arm with a 20-point retrace; every other v2.11 rule is unchanged.

    `entry_spot_close_confirmed` modifies only the entry-spot overlay. A CALL
    stop is confirmed when the completed one-minute close is at/below its
    original entry anchor; a PUT stop is confirmed at/above the anchor. A
    candle that merely touches the anchor and closes back on the favourable
    side remains HOLD, avoiding v2.12's synthetic same-candle exit/re-entry.

    Alpha-CPR paper overlay (all flags default OFF — v2.11 / v2.12 / v2.13 and
    the live runner are byte-for-byte unchanged when they are not passed):
      `no_alpha_exits`  alpha becomes an ENTRY TRIGGER ONLY. Drops the tier
                        alpha SL/TP and ALPHA_STALL, so with the CPR flags the
                        only exits left are the CPR levels and the EOD close.
                        Also drops TRAIL / WALL_REJ / PC250 spot TP-SL / v7.11
                        drift, which are non-alpha exits from v2.11's stack.
      `cpr_levels`      sorted prev-day CPR + floor-pivot spot levels for the
                        day. At entry the nearest level below the entry spot
                        becomes the stop (mirrored for puts), nearest above the
                        target.
      `cpr_min_dist`    ignore levels closer than this many spot points and walk
                        out to the next one — a 13-point spot stop is ~8 premium
                        points on a 350 option, inside spread plus noise.
    Research reference: alphaIMB `research/experiments/2026-08-06_cpr_sl_lot_sizing`.
    """
    if adf is None or len(adf) < 2:
        return (0.0, [], None) if return_state else (0.0, [])
    is_up = sgap > 0
    gap_dir = "UP" if is_up else "DN"
    crossover_th = 25 if tier == "PC50" else 30
    sl_th_default = 25 if tier == "PC50" else 0
    pos_sl_override = None
    target = 100
    oa = float(adf.iloc[0]["alpha"])
    if abs(oa) > 200:
        oa = 0.0
    cb = oa > 50
    pb = oa < -50
    ce_ = 30 <= oa <= 50
    pe_ = -50 <= oa <= -30
    crr = False
    prr = False

    rule3_armed = (
        enable_rule3_dn_put
        and tier in ("PC50", "PC400")
        and gap_dir == "DN"
        and RULE3_LOWER_OPENING_ALPHA <= oa <= RULE3_UPPER_OPENING_ALPHA
    )
    rule3_used = False

    pos = None
    esp = 0.0
    # Alpha-CPR overlay — per-trade structural levels, resolved at entry.
    cpr_sl_level = None
    cpr_tp_level = None
    fill_entry_spot = 0.0
    pnl = 0.0
    peak_pnl = 0.0
    trail_armed = False
    pos_arm = None
    pos_lock = None
    pos_wall_active = False
    appr_ts = None
    appr_wall = None
    appr_oi = None

    d2_pending = False
    d2_nbw = None
    d2_prev_oi = None

    drift_min_alpha = None
    drift_confirmation = False
    drift_armed = False
    drift_stop = None

    trades = []
    entry_ts = None
    entry_alpha = None
    entry_rule = None
    pos_max_alpha = None
    T = crossover_th
    recovery_waiting = False
    recovery_pos = None
    recovery_level = None
    recovery_entry_rule = None
    recovery_sl_override = None
    recovery_origin_ts = None

    def recovery_alpha_exit(side, alpha, sl_override):
        """Whether Alpha invalidates a flat, pending same-side recovery."""
        if tier == "PC50":
            sl_th = sl_override if sl_override is not None else sl_th_default
            return (
                (alpha <= sl_th or alpha >= target)
                if side == "call"
                else (alpha >= -sl_th or alpha <= -target)
            )
        return (
            (alpha <= 0 or alpha >= 100)
            if side == "call"
            else (alpha >= 0 or alpha <= -100)
        )

    for i in range(1, len(adf)):
        p = adf.iloc[i - 1]
        c = adf.iloc[i]
        pa = float(p["alpha"])
        ca = float(c["alpha"])
        denom_alg = float(c["denom"])
        bar_minutes = _bar_minutes_from_midnight(c["timestamp"])
        # STOPS-ONLY partial bar: the in-progress 5-min bucket (ts beyond the
        # completed-bar cutoff). Spot stops + recovery re-entry fire on its 1-min
        # sub-bars; alpha entries/exits are suppressed (they stay on the 5-min
        # grid). Always False when entries_until_ts is None (all batch callers).
        partial_bar = (
            entries_until_ts is not None
            and _local_naive_timestamp(c["timestamp"])
            > _local_naive_timestamp(entries_until_ts)
        )
        if abs(ca) > 200:
            continue

        if cb:
            if not crr and ca < T:
                crr = True
            if crr and pa <= T and ca > T:
                cb = False
                crr = False
        if pb:
            if not prr and ca > -T:
                prr = True
            if prr and pa >= -T and ca < -T:
                pb = False
                prr = False

        if pos == "call" and pos_max_alpha is not None and ca > pos_max_alpha:
            pos_max_alpha = ca

        if (pos == "put" and tier == "PC400" and not is_up
                and enable_v711_drift_protective and drift_min_alpha is not None):
            if ca < drift_min_alpha:
                drift_min_alpha = ca
            if (not drift_confirmation
                    and drift_min_alpha <= V711_CONFIRMATION_ALPHA):
                drift_confirmation = True

        # Flat after an entry-spot stop: Alpha invalidation has priority.
        # Re-entry requires a one-minute touch plus a favourable-side close.
        if enable_entry_spot_recovery and recovery_waiting:
            if not partial_bar and recovery_alpha_exit(recovery_pos, ca, recovery_sl_override):
                trades.append(dict(
                    pnl=0.0, reason="RECOVERY_CANCEL_ALPHA", pos=recovery_pos,
                    entry_ts=recovery_origin_ts, exit_ts=c["timestamp"],
                    entry_alpha=None, exit_alpha=ca,
                    entry_spot=recovery_level, exit_spot=recovery_level,
                    entry_rule=recovery_entry_rule, tier=tier,
                ))
                recovery_waiting = False
                recovery_pos = None
                recovery_level = None
                recovery_entry_rule = None
                recovery_sl_override = None
                recovery_origin_ts = None
                continue

            confirmed_ts = None
            for bts, bh, bl, bc in ohlc.get_1min_bar_closes(c["timestamp"]):
                if recovery_pos == "call" and bl <= recovery_level < bc:
                    confirmed_ts = bts
                    break
                if recovery_pos == "put" and bc < recovery_level <= bh:
                    confirmed_ts = bts
                    break
            if confirmed_ts is not None:
                pos = recovery_pos
                esp = float(recovery_level)
                fill_entry_spot = esp
                entry_ts = confirmed_ts
                entry_alpha = ca
                entry_rule = recovery_entry_rule
                pos_sl_override = recovery_sl_override
                pos_max_alpha = ca
                recovery_waiting = False

                if (tier == "PC400" and pos == "put" and not is_up
                        and enable_v711_drift_protective):
                    drift_min_alpha = ca
                    drift_confirmation = ca <= V711_CONFIRMATION_ALPHA
                    drift_armed = False
                    drift_stop = None
                if tier == "PC400":
                    if is_up and pos == "put":
                        pos_arm, pos_lock, pos_wall_active = TRAIL_ARM, TRAIL_LOCK, False
                    elif pos == "call":
                        if is_up and not day_use_trail:
                            pos_arm, pos_lock, pos_wall_active = None, None, True
                        else:
                            pos_arm, pos_lock, pos_wall_active = TRAIL_ARM, TRAIL_LOCK, False
                    else:
                        if enable_v211a_low_vix_dn_put_trail:
                            pos_arm = V211A_DN_PUT_TRAIL_ARM
                            pos_lock = V211A_DN_PUT_TRAIL_RETRACE
                        else:
                            pos_arm = None
                            pos_lock = None
                        pos_wall_active = False
                else:
                    pos_arm, pos_lock, pos_wall_active = None, None, False
                peak_pnl = 0.0
                trail_armed = False
                recovery_pos = None
                recovery_level = None
                recovery_entry_rule = None
                recovery_sl_override = None
                recovery_origin_ts = None
            # Pending recovery suppresses fresh Alpha entries; a recovered
            # position begins normal exit evaluation on the next 5-minute bar.
            continue

        # ── EXIT ─────────────────────────────────────────────────────────────
        if pos is not None:
            curr_spot = ohlc.get_spot(c["timestamp"]) or float(c["spot"])
            exited = False
            exit_sp = None
            reason = ""

            # The entry-spot close1m overlay runs before every legacy spot/trail
            # stop. Canonical v2.12 books every touch. The close-confirmed live
            # variant ignores a touch whose completed candle closes back on the
            # favourable side, keeping the existing option position open.
            # Recovery remains anchored to the original signal level (`esp`).
            if enable_entry_spot_recovery:
                stop_hits = 0
                active_at_end = True
                for bts, bh, bl, bc in ohlc.get_1min_bar_closes(c["timestamp"]):
                    if active_at_end:
                        hit = (
                            (pos == "call" and bl <= esp)
                            or (pos == "put" and bh >= esp)
                        )
                        favourable_close = (
                            (pos == "call" and bc > esp)
                            or (pos == "put" and bc < esp)
                        )
                        if hit and entry_spot_close_confirmed and favourable_close:
                            continue
                        if hit:
                            stop_hits += 1
                            stop_exit_spot = float(bc)
                            stop_pnl = (
                                stop_exit_spot - esp
                                if pos == "call"
                                else esp - stop_exit_spot
                            )
                            pnl += stop_pnl
                            trades.append(dict(
                                pnl=round(stop_pnl, 2), reason="ENTRY_SPOT_SL", pos=pos,
                                entry_ts=entry_ts,
                                exit_ts=bts,
                                entry_alpha=entry_alpha, exit_alpha=ca,
                                entry_spot=esp,
                                signal_entry_spot=esp,
                                exit_spot=stop_exit_spot,
                                entry_rule=entry_rule, tier=tier,
                            ))
                            active_at_end = False
                            if favourable_close:
                                active_at_end = True
                                fill_entry_spot = esp
                                entry_ts = bts
                                entry_alpha = ca
                    else:
                        crossed = (
                            (pos == "call" and bl <= esp < bc)
                            or (pos == "put" and bc < esp <= bh)
                        )
                        if crossed:
                            active_at_end = True
                            fill_entry_spot = esp
                            entry_ts = bts
                            entry_alpha = ca

                if stop_hits:
                    peak_pnl = 0.0
                    trail_armed = False
                    appr_ts = None
                    pos_arm = None
                    pos_lock = None
                    pos_wall_active = False
                    pos_max_alpha = ca
                    drift_min_alpha = ca if pos == "put" else None
                    drift_confirmation = bool(
                        pos == "put" and ca <= V711_CONFIRMATION_ALPHA
                    )
                    drift_armed = False
                    drift_stop = None
                    if active_at_end:
                        continue

                    recovery_waiting = True
                    recovery_pos = pos
                    recovery_level = float(esp)
                    recovery_entry_rule = entry_rule
                    recovery_sl_override = pos_sl_override
                    recovery_origin_ts = entry_ts
                    pos = None
                    fill_entry_spot = 0.0
                    entry_ts = None
                    entry_alpha = None
                    pos_max_alpha = None
                    continue

            # ── Alpha-CPR structural stop / target ───────────────────────────
            # Hard intra-bar levels resolved at entry, evaluated on the 1-min
            # sub-bars BEFORE every other exit. TP wins ties within a sub-bar
            # (repo convention, cf. PC250 / v2.9.1 spot exits).
            if (not exited and (enable_cpr_sl or enable_cpr_tp)
                    and (cpr_sl_level is not None or cpr_tp_level is not None)):
                for (bh, bl) in ohlc.get_1min_bars(c["timestamp"]):
                    if pos == "call":
                        if (enable_cpr_tp and cpr_tp_level is not None
                                and bh >= cpr_tp_level):
                            exit_sp = cpr_tp_level; exited = True
                            reason = "CPR_TP"; break
                        if (enable_cpr_sl and cpr_sl_level is not None
                                and bl <= cpr_sl_level):
                            exit_sp = cpr_sl_level; exited = True
                            reason = "CPR_SL"; break
                    else:
                        if (enable_cpr_tp and cpr_tp_level is not None
                                and bl <= cpr_tp_level):
                            exit_sp = cpr_tp_level; exited = True
                            reason = "CPR_TP"; break
                        if (enable_cpr_sl and cpr_sl_level is not None
                                and bh >= cpr_sl_level):
                            exit_sp = cpr_sl_level; exited = True
                            reason = "CPR_SL"; break

            if (not exited and not no_alpha_exits
                    and enable_v711_drift_protective and pos == "put"
                    and tier == "PC400" and not is_up):
                if (not drift_armed and not drift_confirmation
                        and ca >= V711_DRIFT_ALPHA):
                    drift_armed = True
                    drift_stop = esp + V711_PROTECTIVE_STOP_BUFFER
                if drift_armed and drift_stop is not None:
                    for (bh, bl) in ohlc.get_1min_bars(c["timestamp"]):
                        if bh >= drift_stop:
                            exit_sp = drift_stop
                            exited = True
                            reason = "v711_drift_stop"
                            break

            # PC400 + non-PC50 trail
            if (pos_arm is not None and not exited and tier != "PC50"
                    and not no_alpha_exits):
                for (bh, bl) in ohlc.get_1min_bars(c["timestamp"]):
                    if pos == "call":
                        bp2 = bh - esp
                        if bp2 > peak_pnl:
                            peak_pnl = bp2
                        if peak_pnl >= pos_arm:
                            trail_armed = True
                        if trail_armed:
                            stop = esp + (peak_pnl - pos_lock)
                            if bl <= stop:
                                exit_sp = stop
                                exited = True
                                reason = "TRAIL"
                                break
                    else:
                        bp2 = esp - bl
                        if bp2 > peak_pnl:
                            peak_pnl = bp2
                        if peak_pnl >= pos_arm:
                            trail_armed = True
                        if trail_armed:
                            stop = esp - (peak_pnl - pos_lock)
                            if bh >= stop:
                                exit_sp = stop
                                exited = True
                                reason = "TRAIL"
                                break
            if not exited:
                if pos == "call":
                    cp = curr_spot - esp
                    if cp > peak_pnl:
                        peak_pnl = cp
                else:
                    cp = esp - curr_spot
                    if cp > peak_pnl:
                        peak_pnl = cp

            # Wall rejection — PC400 gap-UP CALL × WALL only
            if (not exited and not no_alpha_exits
                    and pos_wall_active and peak_pnl >= 40
                    and pos == "call"):
                ce_at = ce_map.get(c["timestamp"], {})
                wall_s, wall_oi = find_wall_above(ce_at, curr_spot)
                if wall_s is not None and 0 < wall_s - curr_spot <= 25:
                    if appr_ts is None:
                        appr_ts = c["timestamp"]
                        appr_wall = wall_s
                        appr_oi = wall_oi
                elif (appr_ts is not None and wall_s is not None
                      and curr_spot > appr_wall):
                    appr_ts = None
                if appr_ts is not None:
                    elapsed = (pd.Timestamp(c["timestamp"]) - pd.Timestamp(appr_ts)).total_seconds() / 60
                    if elapsed >= 5:
                        cw = ce_at.get(appr_wall, 0)
                        oi_red = (appr_oi - cw) / appr_oi if appr_oi > 0 else 0
                        if curr_spot <= appr_wall and oi_red < 0.05:
                            exit_sp = curr_spot
                            exited = True
                            reason = "WALL_REJ"
                        appr_ts = None

            # Tier-specific alpha exits. Alpha-based exits stay on the 5-min grid
            # (suppressed on a stops-only partial bar); spot exits (PC250 TP/SL)
            # below still fire intra-bar.
            if not exited and not no_alpha_exits:
                if tier == "PC50":
                    sl_th = pos_sl_override if pos_sl_override is not None else sl_th_default
                    if not partial_bar:
                        if pos == "call":
                            if ca <= sl_th:
                                exit_sp = curr_spot; exited = True; reason = "SL_ALPHA"
                            elif ca >= target:
                                exit_sp = curr_spot; exited = True; reason = "TGT_ALPHA"
                        else:
                            if ca >= -sl_th:
                                exit_sp = curr_spot; exited = True; reason = "SL_ALPHA"
                            elif ca <= -target:
                                exit_sp = curr_spot; exited = True; reason = "TGT_ALPHA"
                elif tier == "PC250":
                    for (bh, bl) in ohlc.get_1min_bars(c["timestamp"]):
                        if pos == "call":
                            if bh - esp >= 70:
                                exit_sp = esp + 70; exited = True; reason = "TP_SPOT"; break
                            if (not enable_entry_spot_recovery
                                    and esp - bl >= 30):
                                exit_sp = esp - 30; exited = True; reason = "SL_SPOT"; break
                        else:
                            if esp - bl >= 70:
                                exit_sp = esp - 70; exited = True; reason = "TP_SPOT"; break
                            if (not enable_entry_spot_recovery
                                    and bh - esp >= 30):
                                exit_sp = esp + 30; exited = True; reason = "SL_SPOT"; break
                    if not exited and not partial_bar:
                        if pos == "call":
                            if ca <= 0 or ca >= 100:
                                exit_sp = curr_spot; exited = True; reason = "ALPHA"
                        else:
                            if ca >= 0 or ca <= -100:
                                exit_sp = curr_spot; exited = True; reason = "ALPHA"
                elif not partial_bar:  # PC400
                    if pos == "call":
                        if ca <= 0:
                            exit_sp = curr_spot; exited = True; reason = "ALPHA_SL"
                        elif ca >= 100:
                            exit_sp = curr_spot; exited = True; reason = "ALPHA_TGT"
                    else:
                        if ca >= 0:
                            exit_sp = curr_spot; exited = True; reason = "ALPHA_SL"
                        elif ca <= -100:
                            exit_sp = curr_spot; exited = True; reason = "ALPHA_TGT"

            # v7.6 ALPHA_STALL: PC50 UP CALL only (alpha-based -> 5-min grid)
            if (enable_v76_alpha_stall and not exited and not partial_bar
                    and not no_alpha_exits and tier == "PC50"
                    and pos == "call" and is_up
                    and pos_max_alpha is not None and entry_ts is not None):
                trade_minutes = (pd.Timestamp(c["timestamp"]) - pd.Timestamp(entry_ts)).total_seconds() / 60
                if (trade_minutes >= ALPHA_STALL_MIN_DURATION_MIN
                        and ALPHA_STALL_LOWER_ALPHA <= ca <= ALPHA_STALL_UPPER_ALPHA
                        and pos_max_alpha < ALPHA_STALL_MAX_ALPHA_SEEN):
                    exit_sp = curr_spot; exited = True; reason = "ALPHA_STALL"

            if exited:
                t = (
                    (exit_sp - fill_entry_spot)
                    if pos == "call"
                    else (fill_entry_spot - exit_sp)
                )
                pnl += t
                trades.append(dict(
                    pnl=round(t, 2), reason=reason, pos=pos,
                    entry_ts=entry_ts, exit_ts=c["timestamp"],
                    entry_alpha=entry_alpha, exit_alpha=ca,
                    entry_spot=fill_entry_spot,
                    signal_entry_spot=esp,
                    exit_spot=exit_sp,
                    entry_rule=entry_rule, tier=tier,
                    cpr_sl=cpr_sl_level, cpr_tp=cpr_tp_level,
                ))
                cpr_sl_level = None
                cpr_tp_level = None
                pos = None
                fill_entry_spot = 0.0
                peak_pnl = 0.0
                trail_armed = False
                appr_ts = None
                pos_arm = None
                pos_lock = None
                pos_wall_active = False
                pos_sl_override = None
                entry_ts = None
                entry_alpha = None
                entry_rule = None
                pos_max_alpha = None
                drift_min_alpha = None
                drift_confirmation = False
                drift_armed = False
                drift_stop = None
                continue

        # ── ENTRY ────────────────────────────────────────────────────────────
        # Alpha-crossover entries (Rule 1/2/3, D2) are 5-min-grid only — never on
        # a stops-only partial bar. (Entry-spot recovery RE-ENTRY is handled above
        # in the recovery block and DOES fire intra-bar.)
        if pos is None and not partial_bar:
            sp = ohlc.get_spot(c["timestamp"]) or float(c["spot"])
            entered = False
            new_pos = None
            rule_tag = None

            # Rule 3 — first, bypasses v7.7C / v7.8
            if (rule3_armed and not rule3_used and ca < pa and ca < 0):
                esp = sp; new_pos = "put"; entered = True; rule_tag = "RULE3"
                rule3_used = True; rule3_armed = False
                d2_pending = False; d2_nbw = None; d2_prev_oi = None
            elif rule3_armed and ca >= RULE3_INVALIDATE_ALPHA:
                rule3_armed = False

            # D2 trigger (v7.9) — before regular entries
            if not entered and enable_v79_d2 and d2_pending and d2_nbw is not None:
                pe_at = pe_map.get(c["timestamp"], {})
                cur_oi = pe_at.get(d2_nbw)
                if cur_oi is not None and cur_oi > 0 and d2_prev_oi is not None and d2_prev_oi > 0:
                    if ca > D2_ALPHA_INVALIDATE:
                        d2_pending = False; d2_nbw = None; d2_prev_oi = None
                    else:
                        pct = (cur_oi - d2_prev_oi) / d2_prev_oi * 100.0
                        if pct <= D2_PCT_THRESHOLD and ca <= D2_ALPHA_GATE:
                            esp = sp; new_pos = "put"; entered = True; rule_tag = "D2"
                            d2_pending = False; d2_nbw = None; d2_prev_oi = None
                        else:
                            d2_prev_oi = float(cur_oi)

            # Standard Rule 1 / Rule 2
            if not entered:
                if not cb:
                    if ce_:
                        if ca > pa:
                            esp = sp; new_pos = "call"; ce_ = False
                            entered = True; rule_tag = "RULE2"
                    elif pa <= T and ca > T:
                        esp = sp; new_pos = "call"; entered = True; rule_tag = "RULE1"
                if not entered and not pb:
                    if pe_:
                        if ca < pa:
                            esp = sp; new_pos = "put"; pe_ = False
                            entered = True; rule_tag = "RULE2"
                    elif pa >= -T and ca < -T:
                        esp = sp; new_pos = "put"; entered = True; rule_tag = "RULE1"

            # v7.8 PC50 early-session denom guard
            if (enable_v78_denom_guard and entered and tier == "PC50"
                    and rule_tag != "RULE3"):
                if denom_alg < 0 and bar_minutes <= DENOM_GUARD_TIME_CUTOFF_MIN:
                    entered = False

            # v7.7 C PC400 DN PUT filter (+ arms D2)
            if (enable_v77_dn_put_filter and entered and tier == "PC400"
                    and new_pos == "put" and not is_up
                    and rule_tag not in ("RULE3", "D2")):
                pe_at = pe_map.get(c["timestamp"], {})
                if pc400_dn_put_should_skip(pe_at, sp, range_lo, range_hi,
                                            regime, sgap, weekday):
                    entered = False
                    if rule_tag == "RULE2":
                        pe_ = False
                    if enable_v79_d2:
                        nbw = d2_nearest_below_wall(pe_at, sp, range_lo, range_hi)
                        if nbw is not None:
                            cur_oi = pe_at.get(nbw, 0)
                            if cur_oi > 0:
                                d2_pending = True
                                d2_nbw = nbw
                                d2_prev_oi = float(cur_oi)

            if entered:
                pos = new_pos
                fill_entry_spot = float(esp)
                entry_ts = c["timestamp"]
                entry_alpha = ca
                entry_rule = rule_tag
                # Alpha-CPR overlay — resolve structural levels off the entry
                # spot. CALL: stop = nearest level below, target = nearest
                # above; PUT mirrors. None when the entry spot sits outside the
                # level stack on that side (that exit simply cannot fire).
                cpr_sl_level = cpr_tp_level = None
                if cpr_levels:
                    below = [v for v in cpr_levels if v < esp - cpr_min_dist]
                    above = [v for v in cpr_levels if v > esp + cpr_min_dist]
                    if pos == "call":
                        cpr_sl_level = max(below) if below else None
                        cpr_tp_level = min(above) if above else None
                    else:
                        cpr_sl_level = min(above) if above else None
                        cpr_tp_level = max(below) if below else None
                pos_max_alpha = ca
                if tier == "PC50":
                    if rule_tag in ("RULE2", "RULE3"):
                        pos_sl_override = 0
                    else:
                        pos_sl_override = None

                if (tier == "PC400" and pos == "put" and not is_up
                        and enable_v711_drift_protective):
                    drift_min_alpha = ca
                    drift_confirmation = ca <= V711_CONFIRMATION_ALPHA
                    drift_armed = False
                    drift_stop = None

                # trail / wall config per cell
                if tier == "PC400":
                    if is_up and pos == "put":
                        pos_arm = TRAIL_ARM; pos_lock = TRAIL_LOCK; pos_wall_active = False
                    elif pos == "call":
                        if is_up:
                            if day_use_trail:
                                pos_arm = TRAIL_ARM; pos_lock = TRAIL_LOCK; pos_wall_active = False
                            else:
                                pos_arm = None; pos_lock = None; pos_wall_active = True
                        else:  # gap-DN CALL
                            if enable_v77_dn_call_trail:
                                pos_arm = TRAIL_ARM; pos_lock = TRAIL_LOCK; pos_wall_active = False
                            elif day_use_trail:
                                pos_arm = TRAIL_ARM; pos_lock = TRAIL_LOCK; pos_wall_active = False
                            else:
                                pos_arm = None; pos_lock = None; pos_wall_active = True
                    else:  # PC400 PUT × gap-DN
                        if enable_v211a_low_vix_dn_put_trail:
                            pos_arm = V211A_DN_PUT_TRAIL_ARM
                            pos_lock = V211A_DN_PUT_TRAIL_RETRACE
                        else:
                            pos_arm = None
                            pos_lock = None
                        pos_wall_active = False
                else:
                    pos_arm = None; pos_lock = None; pos_wall_active = False
                peak_pnl = 0.0
                trail_armed = False

                if rule_tag != "D2":
                    d2_pending = False; d2_nbw = None; d2_prev_oi = None

    # EOD close (backtest/EOD) OR return the in-flight position (replay-to-now)
    open_state = None
    if pos and not close_eod:
        open_state = dict(
            side="CALL" if pos == "call" else "PUT", entry_ts=entry_ts,
            entry_spot=fill_entry_spot, signal_entry_spot=esp,
            entry_alpha=entry_alpha,
            entry_rule=entry_rule, tier=tier, gap_direction=gap_dir)
    elif pos:
        lsp = ohlc.get_spot(adf.iloc[-1]["timestamp"]) or esp
        t = (
            (lsp - fill_entry_spot)
            if pos == "call"
            else (fill_entry_spot - lsp)
        )
        pnl += t
        trades.append(dict(
            pnl=round(t, 2), reason="EOD", pos=pos,
            entry_ts=entry_ts, exit_ts=adf.iloc[-1]["timestamp"],
            entry_alpha=entry_alpha, exit_alpha=None,
            entry_spot=fill_entry_spot, signal_entry_spot=esp, exit_spot=lsp,
            entry_rule=entry_rule, tier=tier,
            cpr_sl=cpr_sl_level, cpr_tp=cpr_tp_level,
        ))
    if return_state:
        return round(pnl, 2), trades, open_state
    return round(pnl, 2), trades
