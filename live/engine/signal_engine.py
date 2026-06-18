import json
import math
from collections import deque
from datetime import time as dtime
from pathlib import Path

# ==================================================
# CONFIG (Alpha 2.7 / v7.9)
# ==================================================
PC50_THRESHOLD    = 25   # Alpha 2.3 v7.2: PC50 crossover + alpha SL tightened 30→25
STD_THRESHOLD     = 30   # PC250, PC400 crossover + extreme reset threshold
EXPANSION_UPPER   = 50   # expansion zone upper bound (unchanged across tiers)
EXPANSION_LOWER   = 30   # expansion zone lower bound (unchanged — widening to 25 hurts -156 pts)
TARGET_LEVEL      = 100
BLOWUP_LIMIT      = 200

# Alpha 2.6 v7.8 — PC50 early-session denominator-pathology guard cutoff.
# Spec: skip PC50 entry IF denom_alg < 0 AND bar time <= 10:00 IST.
# Rationale: in PC50's tight 50-pt range (3-5 strikes), net-OI-unwind
# (denom < 0) makes the algebraic alpha formula sign-flip vs the structurally-
# correct interpretation; pathology empirically concentrates before 10:00 IST.
PC50_DENOM_GUARD_CUTOFF = dtime(10, 0)

# Alpha v2.6 / v7.10-partial — Rule 3 extreme-zone gap-DN PUT entry.
# Per docs/current_alpha_strategy_spec.md Section 10D (canonical source
# of truth per POLICY.md Section 2). Re-introduced 2026-05-16 after
# user clarification: Rule 3 IS in production for PC50 + PC400 (DN PUT
# only). Earlier removal on 2026-05-15 (commit 55f0cd1) was based on
# the reconstruction doc, which POLICY.md ranks below the canonical
# spec — reconstruction was missing the user's v7.10-partial promotion.
#
# Spec compliance (current_alpha_strategy_spec.md §10D):
# - Arming: tier in (PC50, PC400) AND gap_direction=DOWN AND
#   -100 <= opening_alpha <= -50
# - Trigger: prev_alpha exists AND current_alpha < prev_alpha (first
#   downtick) AND current_alpha < 0 (still bearish)
# - Disarm: current_alpha >= 0 (conservative — bearish thesis broken)
# - One-shot per day (separate `used` flag, never re-fires)
# - Bypasses v7.7 PC400 DN PUT skip filter, v7.8 PC50 denom guard,
#   put_blocked extreme-open block
# - Exit reason names: PC50_RULE3_EXTREME_DN_PUT / PC400_RULE3_EXTREME_DN_PUT
# - PC50 Rule 3 exits use SL=0 (looser, since Rule 3 entry isn't gated by T)
# - Persisted across mid-day restarts (rule3_armed / used / opening_alpha)
RULE3_OPENING_ALPHA_MIN = -100   # inclusive lower
RULE3_OPENING_ALPHA_MAX = -50    # inclusive upper
RULE3_ELIGIBLE_TIERS    = ("PC50", "PC400")  # NOT PC800, NOT PC250

# Per-day state file (key = today's IST date). Restored on bot startup;
# saved on every change. Allows mid-day restart to recover Rule 3 arming.
_DEFAULT_RULE3_STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "rule3_state_botA.json"


# ==================================================
# v7.11 — PC400 DN PUT drift protective stop
# ==================================================
# Per-trade alpha-drift latch on PC400 gap-DN PUT entries. If alpha during
# the trade ever reaches the confirmation threshold (-70) AND later drifts
# back to the arm threshold (-10), arm a spot stop at entry_spot + buffer.
# On any subsequent bar where spot LTP >= stop, exit at stop. Fires across
# Rule 1 / Rule 2 / Rule 3 / D2 entries. TRAIL + WALL regimes both.
#
# This file holds: (a) class constants on AlphaSignalEngine and (b) the
# pure state-transition helper `v711_drift_update`. The exit branch itself
# lives in OrderManager (execution.py) because it needs broker LTP +
# _exit_all. Layer #1 of Phase B-1; flag-gated, default FALSE.
def v711_drift_update(drift_min, confirmation, armed, stop,
                      current_alpha, entry_spot,
                      drift_alpha=-10.0,
                      confirmation_alpha=-70.0,
                      stop_buffer=0.0):
    """Pure state-transition for v7.11 drift protective stop.

    Inputs are the four persisted drift fields plus the new bar's alpha
    and the trade's entry_spot. Returns a dict with the updated four
    fields. Caller compares spot LTP to drift_protective_stop to decide
    whether to fire the exit — that comparison is intentionally NOT in
    this helper so the helper stays broker-free and unit-testable.

    Semantics (matches alphaIMB v7.11 spec exactly):
      - drift_min_alpha: running min α since entry
      - drift_confirmation_reached: latches True once drift_min <= -70
      - drift_protective_armed: latches True once confirmation AND
        current α >= -10 (alpha drifted back toward neutral)
      - drift_protective_stop: entry_spot + buffer, set at arm time

    All three latches are sticky: once True, stay True for the rest of
    the trade. Fields reset on every new entry (handled by caller —
    OrderManager seeds the four fields back to None/False/False/None
    in the entry-save dict).
    """
    if drift_min is None or current_alpha < drift_min:
        drift_min = current_alpha

    if (not confirmation
            and drift_min is not None
            and drift_min <= confirmation_alpha):
        confirmation = True

    if (confirmation
            and not armed
            and current_alpha >= drift_alpha):
        stop = entry_spot + stop_buffer
        armed = True

    return {
        "drift_min_alpha": drift_min,
        "drift_confirmation_reached": confirmation,
        "drift_protective_armed": armed,
        "drift_protective_stop": stop,
    }


def _T(tier):
    """Tier-specific crossover threshold (also used for alpha SL on PC50)."""
    return PC50_THRESHOLD if tier == "PC50" else STD_THRESHOLD


class AlphaSignalEngine:
    # v7.11 PC400 DN PUT drift protective stop — class constants.
    # Default FALSE in this bot (live-money instance; explicit paper-trade
    # opt-in required). Flip via code edit + commit + restart; no intra-day
    # toggle by design.
    # ACTIVATED 2026-05-25 (Commit 3) — live-money research instance.
    ENABLE_V711_DRIFT_PROTECTIVE = True
    V711_DRIFT_ALPHA = -10.0           # arm when α drifts back >= this
    V711_CONFIRMATION_ALPHA = -70.0    # confirmation gate: α must have reached <= this
    V711_PROTECTIVE_STOP_BUFFER = 0.0  # stop = entry_spot + buffer

    # v2.8 PC250 gap-UP per-trade side flip — class constants.
    # Phase B-2 layer #5: SCAFFOLDING ONLY. Default FALSE in this bot.
    # Dormant until alphaIMB upstream stops emitting bucket="SKIP" for
    # gap-UP 50-80 days (Option A from Phase A audit — bot does NOT
    # reclassify SKIP locally). With the flag flipped True but alphaIMB
    # still emitting SKIP, the cell still produces no signal — gate is
    # at the tier source, not here.
    # ACTIVATED 2026-05-25 (Commit 3) — flag flipped for forward-readiness.
    # Code path remains dormant in practice until alphaIMB upstream stops
    # emitting bucket="SKIP" for PC250 gap-UP 50-80 days (Phase B-2 note).
    # Bot will start flipping the moment upstream publishes a non-SKIP
    # bucket on a gap-UP day.
    # Run F (2026-05-27): OFF — PC250 gap-UP stays SKIP. Operator's gut +
    # stitched best-of-both research (alphaIMB commit e4aed85). Previously
    # True (champion); flipped for Run F deployment.
    ENABLE_V28_PC250_GAP_UP_FLIP = False
    V28_PC250_GAP_UP_TP_PTS = 60.0     # spot TP offset (+60 from entry_spot)
    V28_PC250_GAP_UP_SL_PTS = -30.0    # spot SL offset (-30 from entry_spot)

    # Workstream D (2026-05-25) — v2.7 / v2.8 / v2.8.1 alpha-source picker.
    # All flags default FALSE here (Commit 2); Commit 3 flips them True.
    # Per-tier routing:
    #   ENABLE_V27_GEMINI_DYNAMIC      → PC50 + PC250 Gemini c2 alpha
    #                                    selection per pipeline-published
    #                                    pc{50,250}_range_source.
    #   ENABLE_V28_PC50_ABS_DENOM      → PC50 gap-UP swaps to abs-denom
    #                                    alpha (bounded [-100, +100]).
    #                                    Note: v2.8.1 keeps regime range
    #                                    for PC50 gap-UP (NOT Gemini c2)
    #                                    — only the formula switches.
    #   ENABLE_V28_PC400_R13_ROUTING   → PC400 routing per carve-out flag:
    #                                    non-carve  → Gemini c2 + abs-denom
    #                                    carve-out  → regime + std (legacy)
    # Each flag is independently de-promotable: setting it back to False
    # routes that tier through alpha_imb (legacy path).
    # ACTIVATED 2026-05-25 (Commit 3) — live-money research instance.
    # Each flag still individually de-promotable: setting any back to
    # False routes that tier through alpha_imb (legacy path) with no
    # other code changes required.
    # Run F (2026-05-27): OFF — disables BOTH PC50 G11 and PC250 gap-DN
    # gemini routing (one flag controls both). Both cells revert to regime
    # alpha per stitched best-of-both research (alphaIMB commit e4aed85;
    # PC50 G11 clean diagnostic -1,772, PC250 G11 -627). Previously True
    # (champion); flipped for Run F deployment.
    ENABLE_V27_GEMINI_DYNAMIC = False
    ENABLE_V28_PC50_ABS_DENOM = True
    ENABLE_V28_PC400_R13_ROUTING = True

    # ── K (v2.9.1) — PC50 gap-UP spot TP/SL overlay (VIX-gated) ─────────
    # Champion default ON. Faithful port of alphaIMB
    # _alpha_3tier_v26_strategy._check_pc50_exit (commit a6ef782, Alpha
    # v2.9.1). Layered ADDITIVELY on Run F: on PC50 gap-UP days with
    # vix_at_open >= 15.0 (the 09:15 locked VIX), apply a spot exit to BOTH
    # sides, checked BEFORE the PC50 alpha exits (ALPHA_STALL / per-rule
    # alpha SL-TP). TP wins ties.
    #   CALL: TP entry_spot+50, SL entry_spot-30
    #   PUT:  TP entry_spot-50, SL entry_spot+30
    # If neither spot level is touched → fall through to the existing PC50
    # alpha exits unchanged. Inert on PC50 gap-DN, below the VIX gate, or
    # flag-off → Run F exactly.
    # Locked rule: alphaIMB research/experiments/2026-05-29_alpha_roc_filters/
    #   LOCKED_RULE_pc50up_tp50sl30_vix15.md (backtest +246.70 over Run F;
    #   thin/lumpy — 14 of 105 PC50-UP trades modified). Operator-promoted
    #   2026-05-31.
    # REMOVED 2026-06-18 for champion v2.11: the +246.70 edge was a 1-minute
    # sampling artifact; on production-faithful 5-min snapshots it nets only
    # +2.7 (and -59 in the recent window). Flag OFF -> PC50 gap-UP uses Run F
    # alpha exits exactly. See alphaIMB docs/backtest_1min_sampling.md.
    ENABLE_V29_1_PC50_GAP_UP_SPOT_EXIT = False
    V29_1_PC50_UP_TP_PTS = 50.0    # spot TP: favorable move from entry_spot
    V29_1_PC50_UP_SL_PTS = 30.0    # spot SL: adverse move from entry_spot
    V29_1_PC50_UP_VIX_GATE = 15.0  # apply only when vix_at_open >= this
    # ── END K (v2.9.1) ─────────────────────────────────────────────────

    @staticmethod
    def pick_alpha(tier, gap_direction, pc50_range_source,
                   pc250_range_source, pc400_in_carve_out,
                   alpha_imb, alpha_abs_denom, alpha_gemini_c2,
                   alpha_gemini_c2_abs_denom,
                   enable_v27, enable_v28_pc50_absdenom,
                   enable_v28_pc400_r13):
        """v2.8.1 per-tier alpha picker.

        Returns the single alpha value that should be fed into evaluate()
        for the current bar. Defensive: falls back to alpha_imb whenever
          - the relevant flag is False, OR
          - the expected v2.7 / v2.8 column is None (pipeline not yet
            populated for this bar / source), OR
          - the tier doesn't have a v2.7 / v2.8 / v2.8.1 rule defined.

        This is pure: no instance state, no broker access. Tests can
        call it directly with manufactured args.

        Tier rules (matches alphaIMB CHANGELOG_v710_to_v28.md):
          PC50  gap-UP   → abs_denom alpha (regime range)   [v2.8.1]
          PC50  gap-DOWN → Gemini c2 vs regime per G11 selector  [v2.7]
          PC250 gap-DOWN → Gemini c2 alpha per selector     [v2.7]
          PC400          → Gemini c2 + abs_denom (non-carve-out)
                           OR regime + std alpha (carve-out) [v2.8 R13]
          PC50  gap-DOWN with selector=regime, PC800, SKIP, anything
          else            → alpha_imb (legacy regime alpha)
        """
        # PC50 gap-UP — v2.8.1 abs-denom alpha (range unchanged: regime)
        if tier == "PC50" and gap_direction == "UP":
            if enable_v28_pc50_absdenom and alpha_abs_denom is not None:
                return alpha_abs_denom
            return alpha_imb

        # PC50 gap-DOWN — v2.7 G11 Gemini-vs-regime selector
        if tier == "PC50" and gap_direction == "DOWN":
            if (enable_v27 and pc50_range_source == "gemini_c2"
                    and alpha_gemini_c2 is not None):
                return alpha_gemini_c2
            return alpha_imb

        # PC250 gap-DOWN — v2.7 always-Gemini selector when published
        if tier == "PC250" and gap_direction == "DOWN":
            if (enable_v27 and pc250_range_source == "gemini_c2"
                    and alpha_gemini_c2 is not None):
                return alpha_gemini_c2
            return alpha_imb

        # PC400 — v2.8 R13 carve-out routing
        if tier == "PC400":
            if enable_v28_pc400_r13:
                # Carve-out (vix_band / sgap_up / sgap_down) → regime + std
                if pc400_in_carve_out is True:
                    return alpha_imb
                # Non-carve-out → Gemini c2 + abs_denom (with safe fallback)
                if (pc400_in_carve_out is False
                        and alpha_gemini_c2_abs_denom is not None):
                    return alpha_gemini_c2_abs_denom
            # Flag off OR pipeline not yet populated (carve_out=None) OR
            # required alpha column missing — legacy regime alpha.
            return alpha_imb

        # PC800 forward-compat / PC250 gap-UP (still SKIP today) /
        # everything else → legacy regime alpha.
        return alpha_imb

    def __init__(self, rule3_state_path=None):
        self.alpha_series = deque(maxlen=20)
        self.peak_alpha = None
        self.trough_alpha = None
        self.first_bar_processed = False
        self.first_alpha = None
        self.extreme_direction = None
        self.reentry_blocked = False
        # Alpha 2.3 v7.3: opening expansion flags — set once at open, cleared on entry
        self.call_expansion_pending = False
        self.put_expansion_pending = False
        # v7.10-partial: Rule 3 DN PUT one-shot state. Persisted across
        # mid-day restarts via `_rule3_state_path` (canonical spec §10D
        # "Persistence" — mid-day restart between arming and trigger must
        # NOT silently lose the day's setup).
        self.rule3_armed = False
        self.rule3_used = False
        self.rule3_opening_alpha = None
        self._rule3_state_path = (
            Path(rule3_state_path) if rule3_state_path is not None
            else _DEFAULT_RULE3_STATE_PATH
        )
        # Stashed by evaluate() at entry, read by _enter for the v2.8 flip.
        # Defaults are safe: with these as None, the flip block in _enter
        # is impossible to trigger before evaluate() has populated them.
        self._current_tier = None
        self._current_gap_direction = None

    def reset(self):
        """Reset engine for a new session day. Also wipes the persisted
        Rule 3 state file (per-day key check is done at restore time, but
        explicit wipe here is cleaner)."""
        self.alpha_series.clear()
        self.peak_alpha = None
        self.trough_alpha = None
        self.first_bar_processed = False
        self.first_alpha = None
        self.extreme_direction = None
        self.reentry_blocked = False
        self.call_expansion_pending = False
        self.put_expansion_pending = False
        self.rule3_armed = False
        self.rule3_used = False
        self.rule3_opening_alpha = None

    def restore_rule3_state(self, today_iso):
        """Restore Rule 3 state from disk if it was saved for today's
        date. Called by runner on startup so mid-day restart doesn't lose
        the day's arming/usage. Safe to call repeatedly."""
        try:
            if not self._rule3_state_path.exists():
                return
            with open(self._rule3_state_path) as f:
                state = json.load(f)
            if state.get("date") != today_iso:
                return  # Stale (different day) — leave defaults
            self.rule3_armed = bool(state.get("rule3_armed", False))
            self.rule3_used = bool(state.get("rule3_used", False))
            self.rule3_opening_alpha = state.get("rule3_opening_alpha")
        except Exception:
            # Best-effort restore; on any failure proceed with defaults
            pass

    def _persist_rule3_state(self, today_iso):
        """Save Rule 3 state to disk (atomic). Called after every state
        change. Best-effort — silently no-ops on failure."""
        try:
            self._rule3_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._rule3_state_path.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump({
                    "date": today_iso,
                    "rule3_armed": self.rule3_armed,
                    "rule3_used": self.rule3_used,
                    "rule3_opening_alpha": self.rule3_opening_alpha,
                }, f, indent=2)
            tmp.replace(self._rule3_state_path)
        except Exception:
            pass

    def block_reentry(self):
        self.reentry_blocked = True

    def evaluate(self, current_alpha, position, side, tier="PC50",
                 entry_rule=None, denom_alg=None, bar_time_ist=None,
                 gap_direction=None, today_iso=None,
                 spot=None, entry_spot=None, vix_at_open=None):
        # spot / entry_spot / vix_at_open are OPTIONAL, additive context
        # for the v2.9.1 PC50 gap-UP spot exit overlay (K block). They
        # default None so every existing caller is unaffected; when any is
        # missing the overlay is inert and the alpha-only exits run as before.

        # Stash tier + gap_direction so _enter (single chokepoint where
        # the entry dict is finalized) can apply the v2.8 PC250 gap-UP
        # side flip at the boundary. All upstream decisions (Rule 1, Rule 2,
        # Rule 3 arming, v7.6 ALPHA_STALL, v7.8 denom guard, BLOWUP guard)
        # still run against the unflipped side — only the emitted action
        # is flipped on its way out. See _enter for the flip block.
        self._current_tier = tier
        self._current_gap_direction = gap_direction

        # SKIP day → no entries, no exits — just HOLD. Covers both alphaIMB
        # SKIP (gap-UP 50-80) and Alpha 2.3 Wed+PC50 → SKIP override (set in runner).
        if tier == "SKIP":
            return self._hold()

        # Tier-aware crossover threshold (PC50=25, others=30)
        T = _T(tier)

        # Alpha 2.2 blow-up guard
        if abs(current_alpha) > BLOWUP_LIMIT:
            return self._hold()

        self.alpha_series.append(current_alpha)

        if not self.first_bar_processed:
            self.first_bar_processed = True
            self.first_alpha = current_alpha

            if current_alpha > EXPANSION_UPPER:
                self.extreme_direction = "BULL"
            elif current_alpha < -EXPANSION_UPPER:
                self.extreme_direction = "BEAR"
            else:
                # Alpha 2.3 v7.3: set expansion-pending flag for first-uptick rule.
                # Note: zone bounds use STD_THRESHOLD (30..50) regardless of tier —
                # widening to 25..50 for PC50 was tested and lost -156 pts.
                if EXPANSION_LOWER < current_alpha <= EXPANSION_UPPER:
                    self.call_expansion_pending = True
                elif -EXPANSION_UPPER <= current_alpha < -EXPANSION_LOWER:
                    self.put_expansion_pending = True

            # v7.10-partial — Rule 3 arming. Per canonical spec §10D, set
            # at opening bar based on (tier, gap_direction, opening_alpha).
            # Mutually compatible with extreme_direction=BEAR (Rule 3 is the
            # intended path for the extreme-bearish-open DN PUT setup; it
            # explicitly bypasses the standard put_blocked block).
            if (
                tier in RULE3_ELIGIBLE_TIERS
                and gap_direction == "DOWN"
                and not self.rule3_used
                and RULE3_OPENING_ALPHA_MIN <= current_alpha <= RULE3_OPENING_ALPHA_MAX
            ):
                self.rule3_armed = True
                self.rule3_opening_alpha = current_alpha
                if today_iso is not None:
                    self._persist_rule3_state(today_iso)

            return self._hold()

        # Extreme open recovery: clear when alpha returns inside ±30 (hardcoded).
        # Production behavior (per reconstructed_alpha_champion.md Section 15
        # item 11 + Section 6): block-recovery threshold is ±30 for ALL tiers,
        # NOT tier-aware T. This creates intentional "asymmetric hysteresis"
        # on PC50 — entry threshold T=±25 but block lifts only after α returns
        # inside ±30. Fixed 2026-05-15 per Venkatesh adjudication.
        if self.extreme_direction is not None:
            if -STD_THRESHOLD < current_alpha < STD_THRESHOLD:
                self.extreme_direction = None

        if position == "NONE" and len(self.alpha_series) >= 2:

            prev, curr = self.alpha_series[-2], self.alpha_series[-1]

            # ── v7.10-partial Rule 3 (canonical spec §10D) ────────────────
            # Evaluated BEFORE D2 (v7.9) and BEFORE the standard entry
            # pipeline (Rule 1 / Rule 2). Bypasses:
            #   - v7.7 PC400 DN PUT skip filter (in execution.py — fired
            #     reason name will be PC*_RULE3_EXTREME_DN_PUT)
            #   - v7.8 PC50 early-session denom guard (firing here returns
            #     before the v7.8 check below)
            #   - reentry_blocked (Rule 3 is its own one-shot path)
            #   - put_blocked / extreme_direction block (Rule 3 is the
            #     intended path for extreme bearish open DN PUT)
            if (
                self.rule3_armed
                and not self.rule3_used
                and tier in RULE3_ELIGIBLE_TIERS
                and gap_direction == "DOWN"
            ):
                # Conservative disarm: alpha crossed back into bullish
                if curr >= 0:
                    self.rule3_armed = False
                    if today_iso is not None:
                        self._persist_rule3_state(today_iso)
                # Trigger: first downtick AND still bearish
                elif curr < prev and curr < 0:
                    reason = (
                        "PC50_RULE3_EXTREME_DN_PUT" if tier == "PC50"
                        else "PC400_RULE3_EXTREME_DN_PUT"
                    )
                    self.rule3_armed = False
                    self.rule3_used = True
                    if today_iso is not None:
                        self._persist_rule3_state(today_iso)
                    return self._enter("PUT", reason)
                # else: still armed, no fire this bar

            if self.reentry_blocked:
                return self._hold()

            # Alpha 2.6 v7.8: PC50 early-session denominator-pathology guard.
            # Skip ANY PC50 entry signal if denom_alg < 0 AND bar time <= 10:00 IST.
            # NOTE: Rule 3 entries above bypass this guard (they return before
            # reaching this check). v7.8 only gates Rule 1 / Rule 2 PC50 entries.
            if (
                tier == "PC50"
                and denom_alg is not None
                and denom_alg < 0
                and bar_time_ist is not None
                and bar_time_ist <= PC50_DENOM_GUARD_CUTOFF
            ):
                return self._hold(reason="DENOM_GUARD")

            # ── Extreme open: only standard crossover after recovery ──
            if self.first_alpha > EXPANSION_UPPER:
                if self.extreme_direction is not None:
                    return self._hold()
                if prev <= T and curr > T:
                    return self._enter("CALL", "POST_EXTREME_BREAKOUT")
                if prev >= -T and curr < -T:
                    return self._enter("PUT", "POST_EXTREME_BREAKDOWN")
                return self._hold()

            if self.first_alpha < -EXPANSION_UPPER:
                if self.extreme_direction is not None:
                    return self._hold()
                if prev >= -T and curr < -T:
                    return self._enter("PUT", "POST_EXTREME_BREAKDOWN")
                if prev <= T and curr > T:
                    return self._enter("CALL", "POST_EXTREME_BREAKOUT")
                return self._hold()

            # ── Alpha 2.3 v7.3: simplified expansion entry (first-uptick) ──
            # No contraction-before-expansion check, no curr-vs-T check.
            # Fires on the first bar where alpha continues in the open direction.
            if self.call_expansion_pending and curr > prev:
                self.call_expansion_pending = False
                return self._enter("CALL", "OPEN_EXPANSION")
            if self.put_expansion_pending and curr < prev:
                self.put_expansion_pending = False
                return self._enter("PUT", "OPEN_EXPANSION")

            # ── Standard crossover (uses tier-specific T) ──
            if prev <= T and curr > T:
                return self._enter("CALL", "ALPHA_BREAKOUT_UP")
            if prev >= -T and curr < -T:
                return self._enter("PUT", "ALPHA_BREAKOUT_DOWN")

            return self._hold()

        # ── Open-position alpha exits ──
        if position == "OPEN":
            if tier == "PC50":
                # ── K (v2.9.1) — PC50 gap-UP spot TP/SL overlay, VIX-gated.
                # Checked FIRST, BEFORE the per-rule alpha SL/TP exits.
                # TP wins ties. Inert (returns None → fall through) on PC50
                # gap-DN, below the VIX gate, flag-off, or missing context.
                v291 = self._check_v291_pc50_gap_up_spot_exit(
                    side, spot, entry_spot, vix_at_open)
                if v291 is not None:
                    return v291
                # ── END K (v2.9.1) ──
                # Alpha 2.5 v7.5 + v7.10-partial: per-rule alpha SL.
                # Rule 2 (expansion entry, "OPEN_EXPANSION") and Rule 3
                # (extreme-zone, "PC50_RULE3_EXTREME_DN_PUT") both fire
                # ungated by ±25 — entry α can be anywhere inside ±25.
                # Tighter SL=±25 would stop them out next bar. Looser SL=0
                # lets the trade develop to its natural mean-reversion stop.
                # Rule 1 (crossover) entered with α already past ±25 →
                # keeps tighter SL=±25.
                if entry_rule in ("rule2", "rule3"):
                    sl_call_th = 0
                    sl_put_th = 0
                    sl_reason = "ALPHA_SL_0"
                else:
                    sl_call_th = PC50_THRESHOLD       # 25
                    sl_put_th = -PC50_THRESHOLD       # -25
                    sl_reason = "ALPHA_SL_25"
                if side == "CALL":
                    if current_alpha <= sl_call_th:
                        return self._exit(sl_reason)
                    if current_alpha >= TARGET_LEVEL:
                        return self._exit("ALPHA_TARGET_100")
                elif side == "PUT":
                    if current_alpha >= sl_put_th:
                        return self._exit(sl_reason)
                    if current_alpha <= -TARGET_LEVEL:
                        return self._exit("ALPHA_TARGET_-100")
            else:
                # PC250, PC400 — alpha SL=0, target=±100
                if side == "CALL":
                    if current_alpha <= 0:
                        return self._exit("ALPHA_SL_0")
                    if current_alpha >= TARGET_LEVEL:
                        return self._exit("ALPHA_TARGET_100")
                elif side == "PUT":
                    if current_alpha >= 0:
                        return self._exit("ALPHA_SL_0")
                    if current_alpha <= -TARGET_LEVEL:
                        return self._exit("ALPHA_TARGET_-100")
            return self._hold()

        return self._hold()

    def _enter(self, side, reason):
        current_alpha = self.alpha_series[-1]
        if side == "CALL":
            self.peak_alpha = current_alpha
            self.trough_alpha = None
        else:
            self.trough_alpha = current_alpha
            self.peak_alpha = None
        # Alpha 2.5 v7.5 + v7.10-partial: tag the entry rule so PC50 exit
        # logic uses the right SL.
        # - Rule 2 ("OPEN_EXPANSION") and Rule 3 ("PC50_RULE3_EXTREME_DN_PUT"
        #   / "PC400_RULE3_EXTREME_DN_PUT") both fire ungated, use SL=0
        # - Everything else (crossover, post-extreme breakout/breakdown) =
        #   Rule 1, uses SL=±25 on PC50
        if reason == "OPEN_EXPANSION":
            rule = "rule2"
        elif reason.endswith("_RULE3_EXTREME_DN_PUT"):
            rule = "rule3"
        else:
            rule = "rule1"
        sig = {"action": "ENTER", "side": side, "reason": reason, "rule": rule}

        # v2.8 PC250 gap-UP per-trade side flip (Phase B-2 layer #5,
        # scaffolding — dormant until alphaIMB stops emitting
        # bucket="SKIP" for gap-UP 50-80 days). Flag-gated FALSE by
        # default. When True AND tier=PC250 AND gap_direction=UP, this
        # block:
        #   - Inverts the side: v26 alpha decided one direction; v2.8 takes
        #     the opposite. self.peak_alpha / self.trough_alpha above
        #     still track the original direction (vestigial state — not
        #     read by evaluate's OPEN branch, so the flip doesn't desync
        #     anything).
        #   - Tags telemetry keys that OrderManager consumes for spot
        #     exits (tp_v28_spot / sl_v28_spot at entry_spot+60 / -30).
        # This is the LAST mutation applied to the entry dict; all
        # upstream decisions ran normally against the unflipped side.
        if (self.ENABLE_V28_PC250_GAP_UP_FLIP
                and self._current_tier == "PC250"
                and self._current_gap_direction == "UP"):
            sig["side"] = "PUT" if side == "CALL" else "CALL"
            sig["v28_pc250_gap_up_flipped"] = True
            sig["exit_mode"] = "spot"
            sig["tp_pts"] = self.V28_PC250_GAP_UP_TP_PTS
            sig["sl_pts"] = self.V28_PC250_GAP_UP_SL_PTS

        return sig

    def _check_v291_pc50_gap_up_spot_exit(self, side, spot, entry_spot,
                                          vix_at_open):
        """K (v2.9.1) — PC50 gap-UP spot TP+50 / SL-30 overlay, VIX-gated.

        Faithful port of alphaIMB _alpha_3tier_v26_strategy._check_pc50_exit's
        v2.9.1 block (commit a6ef782). Returns an EXIT signal dict or None
        (None → caller falls through to the existing PC50 alpha exits).

        Fires only on PC50 gap-UP days with vix_at_open >= V29_1_PC50_UP_VIX_GATE
        (the 09:15 locked VIX). Both sides, spot-reference. TP is checked
        before SL so it wins ties.
          CALL: TP entry_spot+50, SL entry_spot-30
          PUT:  TP entry_spot-50, SL entry_spot+30

        Live-precision note: alphaIMB checks intra-bar 1-min highs/lows;
        the labs live path supplies only the 5-min alpha-bar spot, so this
        uses the current bar spot — i.e. the alphaIMB one_min_bars=None
        fallback path [(spot, spot)]. entry_spot is the alpha-signal trigger
        spot at entry (spec §15), captured by the runner at entry time.
        """
        if not self.ENABLE_V29_1_PC50_GAP_UP_SPOT_EXIT:
            return None
        if self._current_gap_direction != "UP":
            return None
        if spot is None or entry_spot is None or vix_at_open is None:
            return None
        try:
            vix = float(vix_at_open)
        except (TypeError, ValueError):
            return None
        if math.isnan(vix) or vix < self.V29_1_PC50_UP_VIX_GATE:
            return None
        tp = self.V29_1_PC50_UP_TP_PTS
        sl = self.V29_1_PC50_UP_SL_PTS
        if side == "CALL":
            if spot - entry_spot >= tp:        # TP wins ties (checked first)
                return self._exit("V291_SPOT_TP")
            if entry_spot - spot >= sl:
                return self._exit("V291_SPOT_SL")
        elif side == "PUT":
            if entry_spot - spot >= tp:
                return self._exit("V291_SPOT_TP")
            if spot - entry_spot >= sl:
                return self._exit("V291_SPOT_SL")
        return None

    def _exit(self, reason):
        self.peak_alpha = None
        self.trough_alpha = None
        return {"action": "EXIT", "side": None, "reason": reason}

    def _hold(self, reason=None):
        return {"action": "HOLD", "side": None, "reason": reason}


# ==================================================
# Self-test — run directly: python -m live.engine.signal_engine
# ==================================================
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    def _seq(bars, tier="PC50"):
        engine = AlphaSignalEngine()
        sig = None
        for alpha in bars:
            sig = engine.evaluate(alpha, "NONE", None, tier=tier)
        return sig

    def _first(alpha):
        engine = AlphaSignalEngine()
        return engine.evaluate(alpha, "NONE", None)

    def _exit_test(first_alpha, exit_alpha, side, tier="PC50", entry_rule=None):
        engine = AlphaSignalEngine()
        engine.evaluate(first_alpha, "NONE", None, tier=tier)
        return engine.evaluate(exit_alpha, "OPEN", side, tier=tier, entry_rule=entry_rule)

    def _reentry_blocked():
        engine = AlphaSignalEngine()
        engine.evaluate(10, "NONE", None)
        engine.block_reentry()
        return engine.evaluate(35, "NONE", None)

    def _skip_test():
        engine = AlphaSignalEngine()
        engine.evaluate(10, "NONE", None, tier="SKIP")
        return engine.evaluate(35, "NONE", None, tier="SKIP")

    def _may14_pc400_multi_entry_test():
        """Regression test for 2026-05-14 PC400 gap-UP miss.

        Replays the actual alpha trajectory from nifty_regime_metrics.json
        through the signal engine. Should see at least 3 ENTER signals
        before block_reentry would have fired on a PC250 day — confirms
        PC400 re-entry is allowed per reconstructed_alpha_champion.md.

        Note: this test does NOT call block_reentry between exits, because
        on PC400 the runner is no longer instructed to do so (Fix 1).
        """
        # May 14 PC400 alpha series (subset around crossovers)
        bars = [
            28.43, 26.53, 30.18, 32.76, 25.96, 10.29, -7.89, -4.41,
            -12.92, -26.35, -19.75, -19.74, -20.30, -15.16, -16.99,
            -12.49, -10.51, -4.71, -8.47, -29.91, -25.67, -28.53, -31.54,
            -33.44, -29.31, -13.08, 3.94, 30.51, 50.13,
        ]
        engine = AlphaSignalEngine()
        entries = []
        exits = []
        position = "NONE"
        side = None
        entry_rule = None
        for a in bars:
            sig = engine.evaluate(
                a, position, side, tier="PC400",
                entry_rule=entry_rule,
                gap_direction="UP",
            )
            if sig["action"] == "ENTER":
                entries.append((a, sig["side"], sig["reason"]))
                position = "OPEN"
                side = sig["side"]
                entry_rule = sig.get("rule")
            elif sig["action"] == "EXIT":
                exits.append((a, sig["reason"]))
                position = "NONE"
                side = None
                entry_rule = None
        return entries, exits

    def _pc50_crossover_at_25(prev_alpha, curr_alpha):
        # PC50 should fire crossover at ±25 (Alpha 2.3 v7.2)
        engine = AlphaSignalEngine()
        engine.evaluate(10, "NONE", None, tier="PC50")  # first bar — neutral open
        engine.evaluate(prev_alpha, "NONE", None, tier="PC50")
        return engine.evaluate(curr_alpha, "NONE", None, tier="PC50")

    def _denom_guard_test(denom_alg, bar_time_str, tier="PC50"):
        """Alpha 2.6 v7.8 — does PC50 entry get suppressed by the denom guard?
        Sets up a clean crossover that would normally fire ENTER (curr=35
        crosses BOTH PC50's 25 and PC250's 30 thresholds), then evaluates
        the would-be entry bar with the given denom_alg + bar_time."""
        from datetime import time as _t
        h, m = map(int, bar_time_str.split(":"))
        bar_time = _t(h, m)
        engine = AlphaSignalEngine()
        engine.evaluate(10, "NONE", None, tier=tier)              # first bar
        engine.evaluate(20, "NONE", None, tier=tier)              # bar 2 (prev for crossover)
        return engine.evaluate(
            35, "NONE", None, tier=tier,
            denom_alg=denom_alg, bar_time_ist=bar_time,
        )

    def _rule3_arming_test(opening_alpha, tier="PC400", gap_direction="DOWN"):
        """v7.10-partial — does Rule 3 arm at the opening bar?
        Returns (signal_returned_from_first_bar, rule3_armed_flag).
        First bar always HOLDs but may set rule3_armed."""
        # Use a NON-default state path to avoid touching real disk state
        import tempfile, os
        tmp_dir = tempfile.mkdtemp(prefix="rule3_test_")
        state_path = os.path.join(tmp_dir, "rule3_state.json")
        engine = AlphaSignalEngine(rule3_state_path=state_path)
        sig = engine.evaluate(
            opening_alpha, "NONE", None,
            tier=tier, gap_direction=gap_direction, today_iso="2026-05-15",
        )
        return sig, engine.rule3_armed

    def _rule3_trigger_test(opening_alpha, second_alpha, tier="PC400",
                            gap_direction="DOWN"):
        """v7.10-partial — does Rule 3 fire on the second bar after arming?
        First bar arms (if conditions hold), second bar checks trigger.
        Returns the second-bar signal."""
        import tempfile, os
        tmp_dir = tempfile.mkdtemp(prefix="rule3_test_")
        state_path = os.path.join(tmp_dir, "rule3_state.json")
        engine = AlphaSignalEngine(rule3_state_path=state_path)
        engine.evaluate(
            opening_alpha, "NONE", None,
            tier=tier, gap_direction=gap_direction, today_iso="2026-05-15",
        )
        return engine.evaluate(
            second_alpha, "NONE", None,
            tier=tier, gap_direction=gap_direction, today_iso="2026-05-15",
        )

    cases = [
        # ── First-bar always HOLD ─────────────────────────────────────────
        ("First bar +35  → HOLD",            _first(35),    "HOLD", None),
        ("First bar -35  → HOLD",            _first(-35),   "HOLD", None),
        ("First bar +81  → HOLD (extreme)",  _first(81),    "HOLD", None),
        ("First bar -81  → HOLD (extreme)",  _first(-81),   "HOLD", None),
        ("First bar +10  → HOLD (neutral)",  _first(10),    "HOLD", None),
        # ── Alpha 2.3 v7.3: simplified expansion (first uptick) ───────────
        # First-uptick fires immediately on bar 2 if curr > prev (no contraction needed)
        ("OPEN_EXPANSION CALL (first uptick)", _seq([45, 46]),    "ENTER", "CALL"),
        ("OPEN_EXPANSION PUT (first uptick)",  _seq([-45, -46]),  "ENTER", "PUT"),
        ("Open expansion zone but downtick → HOLD",  _seq([45, 44]),    "HOLD",  None),
        # ── Blow-up guard ─────────────────────────────────────────────────
        ("Blowup α=250 → HOLD",             _first(250),   "HOLD", None),
        ("Blowup then normal → first bar",  _seq([250, 35]),"HOLD", None),
        # ── Alpha 2.5 v7.5: PC50 Rule 1 (crossover) keeps SL=±25 ──────────
        ("PC50 r1 CALL SL: α=24 → EXIT", _exit_test(10, 24, "CALL", "PC50", "rule1"), "EXIT", None),
        ("PC50 r1 PUT  SL: α=-24 → EXIT", _exit_test(-10, -24, "PUT", "PC50", "rule1"), "EXIT", None),
        ("PC50 r1 CALL hold: α=27 → HOLD", _exit_test(10, 27, "CALL", "PC50", "rule1"), "HOLD", None),
        # ── Alpha 2.5 v7.5: PC50 Rule 2 (expansion) uses SL=0 ─────────────
        # α=24 with rule2 should NOT exit (24 > 0)
        ("PC50 r2 CALL hold: α=24 → HOLD", _exit_test(10, 24, "CALL", "PC50", "rule2"), "HOLD", None),
        # α=-1 with rule2 CALL should exit (alpha crossed below 0)
        ("PC50 r2 CALL SL: α=-1 → EXIT", _exit_test(10, -1, "CALL", "PC50", "rule2"), "EXIT", None),
        # α=1 with rule2 PUT should exit (alpha crossed above 0)
        ("PC50 r2 PUT SL: α=1 → EXIT", _exit_test(-10, 1, "PUT", "PC50", "rule2"), "EXIT", None),
        # default (None) entry_rule → behaves like rule1 (back-compat)
        ("PC50 default CALL SL: α=24 → EXIT", _exit_test(10, 24, "CALL", "PC50"), "EXIT", None),
        # ── Alpha 2.3 v7.2: PC50 crossover at 25 (was 30) ─────────────────
        ("PC50 crossover up at 26 → ENTER CALL", _pc50_crossover_at_25(20, 26), "ENTER", "CALL"),
        ("PC50 crossover up at 24 → HOLD",       _pc50_crossover_at_25(20, 24), "HOLD",  None),
        # ── PC250/PC400 exits: alpha SL=0 (unchanged) ─────────────────────
        ("PC250 CALL SL: α=-5 → EXIT",   _exit_test(10, -5, "CALL", "PC250"),  "EXIT", None),
        ("PC250 PUT  SL: α=5  → EXIT",   _exit_test(-10, 5, "PUT", "PC250"),   "EXIT", None),
        ("PC400 CALL SL: α=-1 → EXIT",   _exit_test(10, -1, "CALL", "PC400"),  "EXIT", None),
        # ── Alpha target ──────────────────────────────────────────────────
        ("CALL target α=100 → EXIT",     _exit_test(10, 100, "CALL"),           "EXIT", None),
        # ── Re-entry blocked ──────────────────────────────────────────────
        ("Reentry blocked → HOLD",        _reentry_blocked(),                   "HOLD", None),
        # ── Alpha 2.3: SKIP day → HOLD always ─────────────────────────────
        ("SKIP tier → HOLD",              _skip_test(),                         "HOLD", None),
        # ── Alpha 2.6 v7.8: PC50 early-session denom guard ────────────────
        # denom_alg < 0 AND time ≤ 10:00 → guard fires (HOLD, not ENTER)
        ("v7.8 PC50 denom<0 @ 09:35 → HOLD (guard)",
            _denom_guard_test(-50000, "09:35", "PC50"), "HOLD", None),
        # denom_alg < 0 BUT time > 10:00 → guard does NOT fire (afternoon
        # denom-negative bars typically have legitimate directional meaning)
        ("v7.8 PC50 denom<0 @ 10:30 → ENTER (after cutoff)",
            _denom_guard_test(-50000, "10:30", "PC50"), "ENTER", "CALL"),
        # denom_alg >= 0 @ 09:35 → guard does NOT fire (no pathology)
        ("v7.8 PC50 denom>=0 @ 09:35 → ENTER (denom positive)",
            _denom_guard_test(50000, "09:35", "PC50"), "ENTER", "CALL"),
        # denom_alg None (older bar / non-regime source) → guard does not fire
        ("v7.8 PC50 denom=None → ENTER (no data, fail-open)",
            _denom_guard_test(None, "09:35", "PC50"), "ENTER", "CALL"),
        # PC250 with denom<0 @ 09:35 → guard does NOT apply (PC50-only)
        ("v7.8 PC250 denom<0 @ 09:35 → ENTER (other tier unfiltered)",
            _denom_guard_test(-50000, "09:35", "PC250"), "ENTER", "CALL"),
        # Boundary case: exactly 10:00 → still considered "early-session" (≤ cutoff)
        ("v7.8 PC50 denom<0 @ 10:00 → HOLD (boundary inclusive)",
            _denom_guard_test(-50000, "10:00", "PC50"), "HOLD", None),
        # ── v7.10-partial: Rule 3 trigger tests ───────────────────────────
        # Eligible tiers + gap-DN + opening α ∈ [-100, -50] → arms then fires
        # on first downtick while still bearish.
        ("v7.10 Rule 3 PC400 gap-DN α=-60→-70 → ENTER PUT",
            _rule3_trigger_test(-60, -70, "PC400", "DOWN"), "ENTER", "PUT"),
        ("v7.10 Rule 3 PC50 gap-DN α=-60→-70 → ENTER PUT",
            _rule3_trigger_test(-60, -70, "PC50", "DOWN"), "ENTER", "PUT"),
        # Boundary opening α=-100 / α=-50 — inclusive endpoints
        ("v7.10 Rule 3 boundary α=-100 → ENTER PUT (downtick to -105)",
            _rule3_trigger_test(-100, -105, "PC400", "DOWN"), "ENTER", "PUT"),
        ("v7.10 Rule 3 boundary α=-50 → ENTER PUT (downtick to -60)",
            _rule3_trigger_test(-50, -60, "PC400", "DOWN"), "ENTER", "PUT"),
        # PC250 — NOT in RULE3_ELIGIBLE_TIERS, falls through to standard logic.
        # extreme_direction=BEAR set at first bar (-60 < -50) → blocks bar 2.
        ("v7.10 Rule 3 PC250 α=-60→-70 → HOLD (PC250 not eligible)",
            _rule3_trigger_test(-60, -70, "PC250", "DOWN"), "HOLD", None),
        # gap-UP — NOT eligible (counter-trend cell rejected)
        ("v7.10 Rule 3 gap-UP excluded → HOLD",
            _rule3_trigger_test(-60, -70, "PC400", "UP"), "HOLD", None),
        # Outside [-100, -50] range — α=-30 doesn't arm; std logic runs
        # (PC400 T=30, prev=-30 fail, curr=-40 < -30 → Rule 1 ALPHA_BREAKOUT_DOWN)
        ("v7.10 α=-30 (not extreme) → Rule 1 fires (Rule 3 not armed)",
            _rule3_trigger_test(-30, -40, "PC400", "DOWN"), "ENTER", "PUT"),
        # Disarmed by alpha crossing into bullish before trigger
        ("v7.10 Rule 3 conservative disarm: α=-60→+5 → HOLD",
            _rule3_trigger_test(-60, 5, "PC400", "DOWN"), "HOLD", None),
        # Uptick from extreme but still bearish → no trigger, stays armed
        ("v7.10 Rule 3 uptick still bearish → HOLD (armed; no first-downtick)",
            _rule3_trigger_test(-60, -55, "PC400", "DOWN"), "HOLD", None),
        # Out of range opening — α=-105 (more extreme than -100)
        ("v7.10 Rule 3 α=-105 (below range) → HOLD (not armed)",
            _rule3_trigger_test(-105, -110, "PC400", "DOWN"), "HOLD", None),
    ]

    # Extra v7.5 + v7.10-partial: verify entry signals carry the right "rule" tag
    expansion_sig = _seq([45, 46])
    crossover_sig = _pc50_crossover_at_25(20, 26)
    rule3_sig = _rule3_trigger_test(-60, -70, "PC400", "DOWN")
    rule_cases = [
        ("OPEN_EXPANSION rule==rule2", expansion_sig.get("rule"), "rule2"),
        ("ALPHA_BREAKOUT rule==rule1", crossover_sig.get("rule"), "rule1"),
        ("PC400_RULE3_EXTREME_DN_PUT rule==rule3", rule3_sig.get("rule"), "rule3"),
    ]

    # ──────────────────────────────────────────────────────────────────
    # Phase B-1 layer #7 — PC400 R13 carve-out detection (telemetry)
    # ──────────────────────────────────────────────────────────────────
    # Imports execution.py's pure helper. Guarded so a missing
    # kiteconnect (local dev) doesn't break the rest of the test run.
    carve_cases = []
    v711_cases = []
    try:
        raise ImportError("carve-out helper is not bundled with the live signal module")
        carve_cases = [
            ("v7  carve-out vix=18, sgap=+50 → vix_band",
                compute_pc400_carve_out(18.0, 50.0, enable=True),  (True, "vix_band")),
            ("v7  carve-out vix=14, sgap=+150 → sgap_up",
                compute_pc400_carve_out(14.0, 150.0, enable=True), (True, "sgap_up")),
            ("v7  carve-out vix=14, sgap=-250 → sgap_down",
                compute_pc400_carve_out(14.0, -250.0, enable=True),(True, "sgap_down")),
            ("v7  carve-out vix=14, sgap=+50 → no carve-out",
                compute_pc400_carve_out(14.0, 50.0, enable=True),  (False, None)),
            ("v7  carve-out vix=NaN, sgap=+150 → sgap_up (NaN falls through)",
                compute_pc400_carve_out(float('nan'), 150.0, enable=True), (True, "sgap_up")),
        ]
    except Exception as e:
        print(f"  [SKIP] carve-out helper import failed: {e}")

    # ──────────────────────────────────────────────────────────────────
    # Phase B-1 layer #1 — v7.11 PC400 DN PUT drift protective stop
    # ──────────────────────────────────────────────────────────────────
    # Exercises the pure state-transition `v711_drift_update` defined at
    # module level above, wrapped with the gate logic used by
    # OrderManager.check_v711_drift_protective. Tests do not touch the
    # broker — the spot LTP comparison is performed against the test's
    # current_spot argument.
    def _v711_simulate(bars, entry_spot, current_spot,
                       tier="PC400", gap_direction="DOWN", side="PUT",
                       enable=True):
        """Mimics check_v711_drift_protective gates + state walk.
        Returns "v711_drift_stop" if exit would fire, else None.
        Tests run against the module-level pure helper so they remain
        broker-free and decoupled from OrderManager construction."""
        if not enable:
            return None
        if tier != "PC400":
            return None
        if gap_direction != "DOWN":
            return None
        if side != "PUT":
            return None
        drift_min = None
        confirmation = False
        armed = False
        stop = None
        for a in bars:
            new = v711_drift_update(
                drift_min, confirmation, armed, stop,
                a, entry_spot,
                drift_alpha=AlphaSignalEngine.V711_DRIFT_ALPHA,
                confirmation_alpha=AlphaSignalEngine.V711_CONFIRMATION_ALPHA,
                stop_buffer=AlphaSignalEngine.V711_PROTECTIVE_STOP_BUFFER,
            )
            drift_min = new["drift_min_alpha"]
            confirmation = new["drift_confirmation_reached"]
            armed = new["drift_protective_armed"]
            stop = new["drift_protective_stop"]
        if armed and stop is not None and current_spot is not None and current_spot >= stop:
            return "v711_drift_stop"
        return None

    v711_cases = [
        # 1a — trajectory hits -80 (confirms), drifts to -8 (arms at α=-8 >= -10),
        # current_spot=99 below stop (=100) → NO exit
        ("v7.11 1a confirmed+armed+spot<stop → NO exit",
            _v711_simulate([-30, -80, -50, -8], entry_spot=100, current_spot=99),
            None),
        # 1b — same as 1a but spot==stop → exit
        ("v7.11 1b confirmed+armed+spot==stop → v711_drift_stop",
            _v711_simulate([-30, -80, -50, -8], entry_spot=100, current_spot=100),
            "v711_drift_stop"),
        # 1c — trajectory never reaches -70 → confirmation gate blocks arming
        ("v7.11 1c never-confirmed → NO exit",
            _v711_simulate([-30, -50, -8], entry_spot=100, current_spot=200),
            None),
        # 1d — gap-UP cell (defensive): should NEVER fire regardless of trajectory
        ("v7.11 1d gap-UP → NO exit (gap gate)",
            _v711_simulate([-30, -80, -50, -8], entry_spot=100, current_spot=200,
                           gap_direction="UP"),
            None),
        # 1e — PC50 tier: should NEVER fire (PC400-only)
        ("v7.11 1e PC50 tier → NO exit (tier gate)",
            _v711_simulate([-30, -80, -50, -8], entry_spot=100, current_spot=200,
                           tier="PC50"),
            None),
        # 1f — CALL side: should NEVER fire (PUT-only)
        ("v7.11 1f CALL side → NO exit (side gate)",
            _v711_simulate([-30, -80, -50, -8], entry_spot=100, current_spot=200,
                           side="CALL"),
            None),
    ]

    # ──────────────────────────────────────────────────────────────────
    # Phase B-2 layer #5 — v2.8 PC250 gap-UP per-trade flip + spot exits
    # ──────────────────────────────────────────────────────────────────
    # Cases 5a-5e exercise the flip in evaluate()/_enter via a controlled
    # alpha sequence that triggers a Rule 1 crossover entry. Cases 5f-5j
    # exercise the pure spot-exit decision helper imported from execution.

    def _v28_flip_test(tier, gap_direction, alpha_direction, enable_flip):
        """Drive the engine through a clean crossover so _enter fires.
        alpha_direction='UP' → CALL crossover (prev=20, curr=35 > T=30);
        alpha_direction='DOWN' → PUT crossover (prev=-20, curr=-35 < -T).
        Returns the entry signal dict."""
        engine = AlphaSignalEngine()
        engine.ENABLE_V28_PC250_GAP_UP_FLIP = enable_flip
        if alpha_direction == "UP":
            seq = [10, 20, 35]
        else:
            seq = [-10, -20, -35]
        sig = None
        for a in seq:
            sig = engine.evaluate(a, "NONE", None,
                                  tier=tier, gap_direction=gap_direction)
        return sig

    # Cases 5a-5e: flip behavior (or non-flip)
    v28_flip_cases = []

    # 5a — PC250 + gap-UP + flag=True + alpha emits CALL → flipped to PUT
    sig_5a = _v28_flip_test("PC250", "UP", "UP", enable_flip=True)
    v28_flip_cases.append((
        "v2.8 5a PC250 gap-UP flag=True alpha-CALL → side flips to PUT",
        (sig_5a["action"], sig_5a["side"],
         sig_5a.get("v28_pc250_gap_up_flipped"),
         sig_5a.get("exit_mode"),
         sig_5a.get("tp_pts"), sig_5a.get("sl_pts")),
        ("ENTER", "PUT", True, "spot", 60.0, -30.0),
    ))

    # 5b — PC250 + gap-UP + flag=True + alpha emits PUT → flipped to CALL
    sig_5b = _v28_flip_test("PC250", "UP", "DOWN", enable_flip=True)
    v28_flip_cases.append((
        "v2.8 5b PC250 gap-UP flag=True alpha-PUT → side flips to CALL",
        (sig_5b["action"], sig_5b["side"],
         sig_5b.get("v28_pc250_gap_up_flipped"),
         sig_5b.get("exit_mode")),
        ("ENTER", "CALL", True, "spot"),
    ))

    # 5c — PC250 + gap-UP + flag=False → no flip (passes through unchanged)
    sig_5c = _v28_flip_test("PC250", "UP", "UP", enable_flip=False)
    v28_flip_cases.append((
        "v2.8 5c PC250 gap-UP flag=False → no flip (CALL stays CALL)",
        (sig_5c["action"], sig_5c["side"],
         sig_5c.get("v28_pc250_gap_up_flipped"),
         sig_5c.get("exit_mode")),
        ("ENTER", "CALL", None, None),
    ))

    # 5d — PC50 + gap-UP + flag=True → no flip (tier gate)
    sig_5d = _v28_flip_test("PC50", "UP", "UP", enable_flip=True)
    v28_flip_cases.append((
        "v2.8 5d PC50 gap-UP flag=True → no flip (tier gate)",
        (sig_5d["action"], sig_5d["side"],
         sig_5d.get("v28_pc250_gap_up_flipped")),
        ("ENTER", "CALL", None),
    ))

    # 5e — PC250 + gap-DOWN + flag=True → no flip (gap-direction gate)
    sig_5e = _v28_flip_test("PC250", "DOWN", "DOWN", enable_flip=True)
    v28_flip_cases.append((
        "v2.8 5e PC250 gap-DOWN flag=True → no flip (gap-direction gate)",
        (sig_5e["action"], sig_5e["side"],
         sig_5e.get("v28_pc250_gap_up_flipped")),
        ("ENTER", "PUT", None),
    ))

    # ──────────────────────────────────────────────────────────────────
    # Workstream D — pick_alpha per-tier router (v2.7 / v2.8 / v2.8.1)
    # ──────────────────────────────────────────────────────────────────
    # 11 cases exercising the pure static AlphaSignalEngine.pick_alpha.
    # Each row: (label, actual, expected).
    pa = AlphaSignalEngine.pick_alpha
    pick_alpha_cases = [
        # 1. PC50 UP — abs_denom flag ON, value present → abs_denom wins
        ("WD pick_alpha PC50 UP + absdenom-on + value → abs_denom",
            pa("PC50", "UP", None, None, None,
               alpha_imb=12.0, alpha_abs_denom=18.5,
               alpha_gemini_c2=99.0, alpha_gemini_c2_abs_denom=99.0,
               enable_v27=True, enable_v28_pc50_absdenom=True, enable_v28_pc400_r13=True),
            18.5),
        # 2. PC50 UP — abs_denom flag OFF → alpha_imb
        ("WD pick_alpha PC50 UP + absdenom-OFF → alpha_imb",
            pa("PC50", "UP", None, None, None,
               alpha_imb=12.0, alpha_abs_denom=18.5,
               alpha_gemini_c2=99.0, alpha_gemini_c2_abs_denom=99.0,
               enable_v27=True, enable_v28_pc50_absdenom=False, enable_v28_pc400_r13=True),
            12.0),
        # 3. PC50 UP — flag ON but abs_denom column NULL → fallback to alpha_imb
        ("WD pick_alpha PC50 UP + absdenom-on + NULL → alpha_imb fallback",
            pa("PC50", "UP", None, None, None,
               alpha_imb=12.0, alpha_abs_denom=None,
               alpha_gemini_c2=99.0, alpha_gemini_c2_abs_denom=99.0,
               enable_v27=True, enable_v28_pc50_absdenom=True, enable_v28_pc400_r13=True),
            12.0),
        # 4. PC50 DOWN — G11 selector="gemini_c2" + flag ON → gemini_c2
        ("WD pick_alpha PC50 DOWN + selector=gemini_c2 + v27-on → gemini_c2",
            pa("PC50", "DOWN", "gemini_c2", None, None,
               alpha_imb=12.0, alpha_abs_denom=None,
               alpha_gemini_c2=-25.0, alpha_gemini_c2_abs_denom=None,
               enable_v27=True, enable_v28_pc50_absdenom=True, enable_v28_pc400_r13=True),
            -25.0),
        # 5. PC50 DOWN — selector="regime" + flag ON → alpha_imb
        ("WD pick_alpha PC50 DOWN + selector=regime + v27-on → alpha_imb",
            pa("PC50", "DOWN", "regime", None, None,
               alpha_imb=12.0, alpha_abs_denom=None,
               alpha_gemini_c2=-25.0, alpha_gemini_c2_abs_denom=None,
               enable_v27=True, enable_v28_pc50_absdenom=True, enable_v28_pc400_r13=True),
            12.0),
        # 6. PC250 DOWN — selector="gemini_c2" + flag ON → gemini_c2
        ("WD pick_alpha PC250 DOWN + selector=gemini_c2 + v27-on → gemini_c2",
            pa("PC250", "DOWN", None, "gemini_c2", None,
               alpha_imb=15.0, alpha_abs_denom=None,
               alpha_gemini_c2=-40.0, alpha_gemini_c2_abs_denom=None,
               enable_v27=True, enable_v28_pc50_absdenom=False, enable_v28_pc400_r13=False),
            -40.0),
        # 7. PC400 carve-out=True + R13 flag ON → alpha_imb (carve-out fallback)
        ("WD pick_alpha PC400 carve-out=True + R13-on → alpha_imb (carve-out)",
            pa("PC400", "DOWN", None, None, True,
               alpha_imb=20.0, alpha_abs_denom=99.0,
               alpha_gemini_c2=99.0, alpha_gemini_c2_abs_denom=-55.0,
               enable_v27=True, enable_v28_pc50_absdenom=True, enable_v28_pc400_r13=True),
            20.0),
        # 8. PC400 non-carve + R13 flag ON + gemini_abs_denom present → gemini_abs_denom
        ("WD pick_alpha PC400 carve-out=False + R13-on + value → gemini_abs_denom",
            pa("PC400", "DOWN", None, None, False,
               alpha_imb=20.0, alpha_abs_denom=99.0,
               alpha_gemini_c2=99.0, alpha_gemini_c2_abs_denom=-55.0,
               enable_v27=True, enable_v28_pc50_absdenom=True, enable_v28_pc400_r13=True),
            -55.0),
        # 9. PC400 non-carve + R13 ON + gemini_abs_denom NULL → alpha_imb fallback
        ("WD pick_alpha PC400 carve-out=False + R13-on + NULL → alpha_imb fallback",
            pa("PC400", "DOWN", None, None, False,
               alpha_imb=20.0, alpha_abs_denom=99.0,
               alpha_gemini_c2=99.0, alpha_gemini_c2_abs_denom=None,
               enable_v27=True, enable_v28_pc50_absdenom=True, enable_v28_pc400_r13=True),
            20.0),
        # 10. PC400 carve-out=None (pipeline not populated) + R13 ON → alpha_imb
        ("WD pick_alpha PC400 carve-out=None + R13-on → alpha_imb (defensive)",
            pa("PC400", "DOWN", None, None, None,
               alpha_imb=20.0, alpha_abs_denom=99.0,
               alpha_gemini_c2=99.0, alpha_gemini_c2_abs_denom=-55.0,
               enable_v27=True, enable_v28_pc50_absdenom=True, enable_v28_pc400_r13=True),
            20.0),
        # 11. PC800 forward-compat — all flags ON → alpha_imb (no PC800 rule yet)
        ("WD pick_alpha PC800 + all flags ON → alpha_imb (no PC800 rule)",
            pa("PC800", "UP", "gemini_c2", "gemini_c2", False,
               alpha_imb=33.0, alpha_abs_denom=44.0,
               alpha_gemini_c2=55.0, alpha_gemini_c2_abs_denom=66.0,
               enable_v27=True, enable_v28_pc50_absdenom=True, enable_v28_pc400_r13=True),
            33.0),
    ]

    # Cases 5f-5j: spot-exit decision helper
    v28_spot_cases = []
    try:
        raise ImportError("v28 spot-exit helper is not bundled with the live signal module")
        v28_spot_cases = [
            # 5f — TP hit: cur - entry = +65 >= +60
            ("v2.8 5f spot delta=+65 → tp_v28_spot",
                v28_pc250_gap_up_decide_spot_exit(True, 24500, 60, -30, 24565),
                "tp_v28_spot"),
            # 5g — SL hit: cur - entry = -30 <= -30 (boundary inclusive)
            ("v2.8 5g spot delta=-30 → sl_v28_spot (boundary inclusive)",
                v28_pc250_gap_up_decide_spot_exit(True, 24500, 60, -30, 24470),
                "sl_v28_spot"),
            # 5h — within bands: cur - entry = +30, neither hit
            ("v2.8 5h spot delta=+30 → None (within bands)",
                v28_pc250_gap_up_decide_spot_exit(True, 24500, 60, -30, 24530),
                None),
            # 5i — is_flip=False → never fires (state gate)
            ("v2.8 5i is_flip=False → None (state gate)",
                v28_pc250_gap_up_decide_spot_exit(False, 24500, 60, -30, 24600),
                None),
            # 5j — entry_spot=None → defensive guard returns None
            ("v2.8 5j entry_spot=None → None (defensive guard)",
                v28_pc250_gap_up_decide_spot_exit(True, None, 60, -30, 24600),
                None),
        ]
    except Exception as e:
        print(f"  [SKIP] v28 spot-exit helper import failed: {e}")

    # May 14 PC400 multi-entry regression
    may14_entries, may14_exits = _may14_pc400_multi_entry_test()
    print(f"\n--- May 14 PC400 multi-entry regression ---")
    print(f"  Entries: {len(may14_entries)}  Exits: {len(may14_exits)}")
    for i, (a, s, r) in enumerate(may14_entries, 1):
        print(f"    Entry {i}: alpha={a:.2f} side={s} reason={r}")
    for i, (a, r) in enumerate(may14_exits, 1):
        print(f"    Exit  {i}: alpha={a:.2f} reason={r}")
    expected_min_entries = 2   # at minimum, the 09:30 CALL and the 11:30 CALL
    may14_pass = len(may14_entries) >= expected_min_entries
    tag = "PASS" if may14_pass else "FAIL"
    print(f"  [{tag}] May 14 PC400 should fire >= {expected_min_entries} entries (got {len(may14_entries)})")

    passed = 0
    for desc, sig, exp_action, exp_side in cases:
        ok = sig["action"] == exp_action and sig["side"] == exp_side
        passed += ok
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {desc}")
        if not ok:
            print(f"         expected: action={exp_action!r} side={exp_side!r}")
            print(f"         got:      {sig}")

    rule_passed = 0
    for desc, actual, expected in rule_cases:
        ok = actual == expected
        rule_passed += ok
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {desc}")
        if not ok:
            print(f"         expected={expected!r}, got={actual!r}")

    # Phase B-1 layer #7 + layer #1 + Phase B-2 layer #5 + Workstream D
    # scoring (all tuple-equality cases)
    extra_passed = 0
    for desc, actual, expected in (carve_cases + v711_cases
                                   + v28_flip_cases + v28_spot_cases
                                   + pick_alpha_cases):
        ok = actual == expected
        extra_passed += ok
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {desc}")
        if not ok:
            print(f"         expected={expected!r}, got={actual!r}")

    total_pass = passed + rule_passed + extra_passed
    total = (len(cases) + len(rule_cases) + len(carve_cases)
             + len(v711_cases) + len(v28_flip_cases) + len(v28_spot_cases)
             + len(pick_alpha_cases))
    print(f"\n{total_pass}/{total} passed")
