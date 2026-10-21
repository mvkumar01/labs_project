"""SENSEX Proposer 2.5% day-target engine. Pure, broker-free, clock injected.

Port of Pramanaa `ProposerV1dDayTargetEngine` and its parents (services/labs/engines/
proposer_v1d_target.py, proposer_signals.py, and the fresh-print gate of labs_orchestrator.py, at
commit 62ef7d4) - the brain behind "Proposer-v1d Spot 100/40 conf>0.45 DayTarget 2.5%".

ENTRY (flat, only on a FRESH 10-min print, consumed when attempted):
  - the daily regime drives while fresh and decisive, once per session (the regime licence);
  - afterwards, on a neutral regime, or once a strong 5-class print contradicts the regime (the
    one-way stale latch), the 5-class drives - and only above the confidence gate (0.45);
  - a 300 s cooldown follows every exit except a signal flip;
  - no entries once the day is banked, or (variants with max_losses_per_day) once that many
    trades have closed at a loss today;
  - variants with with_day_pts (v3): only with the day - SENSEX's last completed 1-min close must be
    at least that many points beyond the 09:15 close in the trade's direction. Not in Pramanaa's
    engine - added 2026-10-08: the entries that are not with the day carried the big losers
    (alphaIMB research, REBUILD.md sec. 18).
EXIT (open), first match wins:
  loss_floor  - premium down to the floor (-30% in the live variant, -20% in v3; the source default
                is -15%)
  spot_target - SENSEX moved spot_target_pts in favour (decisive width only for the
                regime-driven entry, neutral width otherwise), less a 1-pt tolerance
  signal_flip - a gate-passing opposite print (exit, no cooldown so the reverse can follow)
  daily_target- day realized + this trade's unrealized >= the configured share of this trade's
                entry capital (2.5% normally; 3.5% in proposer_dt35_v3)
  spot_floor  - variants with spot_floor_pts only (proposer_dt35_v5): SENSEX that many points against
                the entry spot. Checked after every other exit, as in the Sensex V5 paper book; those
                variants switch the premium floor off.
  bar_exit    - variants with a bar_exit spec only: the completed 1-min SENSEX bars since the
                entry turned against the position (proposer_bar_exit: Renko bricks or a run of
                adverse closes). Not in Pramanaa's engine - added 2026-10-06 because the flip
                above fires a median 35-46 min into a losing trade.
  (15:25 flat is the caller's job.)
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import math
from typing import Iterable, Optional, Sequence

from live.engine import proposer_bar_exit as bar_exit

DECISIVE = ("bullish", "bearish", "risk_off")
STRATEGY_VERSION = "proposer_dt25"
# The same engine with the price-action exit and the one-loss-per-day stop switched on.
STRATEGY_VERSION_PX = "proposer_dt25_px"
PX_BAR_EXIT = "renko-50"
# v3: the base rules (no bar exit, no loss cap) taking only entries that go with the day, with the
# premium floor at -20%: none of v3's winning trades in the 1 Jun - 8 Oct 2026 replay dipped below
# -20% before winning, while most of its sizeable losers passed it on the way to -30%
# (alphaIMB research, REBUILD.md sec. 20).
STRATEGY_VERSION_V3 = "proposer_dt25_v3"
V3_WITH_DAY_PTS = 75.0
V3_LOSS_FLOOR_PCT = -0.20
# v3 with the only changed parameter being the per-trade day target (3.5% vs 2.5%).
STRATEGY_VERSION_V3_DT35 = "proposer_dt35_v3"
V3_DT35_DAY_TARGET_PCT = 0.035
# Every live preset waits 270 s after an exit, not Pramanaa's 300 s (2026-10-16; tested on v3 and
# v3 3.5%, applied to all at the operator's request): prints come every 300 s and a
# spot-target exit lands a few seconds after one, so the next print is due ~4m50s after the exit -
# inside a 300 s cooldown, and thrown away (16 Oct 09:32:25 vs a cooldown to 09:32:37). 270 s still
# burns the print that arrived while the trade was open. Labs tracker, fresh book from 1 Jul, v3 3.5%
# with the Sentinel strike: +10.64L vs +9.53L, worst day -2.56L vs -3.27L; v3 2.5%: +9.44L vs
# +7.57L (alphaIMB REBUILD.md sec. 29).
REENTRY_COOLDOWN_S = 270.0
# Sensex V5: proposer_dt35_v3 (= the Sensex V4 paper book) with the -20% premium floor replaced by a loss
# floor in SENSEX points - out once SENSEX is 150 points against the entry spot. Paper book proposer_v5,
# 1 Jul - 16 Oct: +10.75L vs V4's +8.15L, 150 picked in-sample from four values (alphaIMB REBUILD.md
# sec. 33, P150). The live preset "Sensex V4 / V5" picks between the two floors (Vipin, 2026-10-19).
STRATEGY_VERSION_V5 = "proposer_dt35_v5"
STRATEGY_VERSION_NIFTY_SCALPER_V1 = "proposer_nifty_scalper_v1"
V5_SPOT_FLOOR_PTS = 150.0
NO_PREMIUM_FLOOR_PCT = -0.999


@dataclass(frozen=True)
class ProposerParams:
    conf_gate: float = 0.45
    spot_target_decisive_pts: float = 100.0
    spot_target_neutral_pts: float = 40.0
    spot_target_tolerance_pts: float = 1.0
    loss_floor_pct: float = -0.30     # live variant: holds through -18% dips (16 & 24 Sep ledger)
    reentry_cooldown_secs: float = 300.0
    daily_target_pct: float = 0.025
    recovery_min_book_net: float = 0.0
    predictor_max_age_secs: float = 600.0
    confirm_bank_on_realized: bool = True
    confirm_bank_tolerance: float = 0.10
    # Pramanaa's orchestrator consumes a print when it checks for an entry, even if the entry is
    # then blocked by a still-working exit; with sliced exits the reverse after a flip is lost (16 Sep
    # 09:42 in the ledger). False reproduces that: the print that caused the flip is consumed.
    reverse_after_flip: bool = False
    # Price-action exit on completed 1-min bars (proposer_bar_exit spec, "" = off) and a cap on
    # losing trades per day (0 = off). Both off reproduces Pramanaa's engine.
    bar_exit: str = ""
    max_losses_per_day: int = 0
    # Entry filter (0 = off): points SENSEX must be beyond the 09:15 close in the trade's direction.
    with_day_pts: float = 0.0
    # Loss floor in SENSEX points against the entry spot (0 = off).
    spot_floor_pts: float = 0.0


# The live preset "Sensex V4 / V5" lets each connection set its own day target (Vipin, 2026-10-19): stored
# per connection as a percent ("2.5"), applied to these versions only, within these bounds.
DAY_TARGET_VERSIONS = frozenset({STRATEGY_VERSION_V3_DT35, STRATEGY_VERSION_V5,
                                 STRATEGY_VERSION_NIFTY_SCALPER_V1})
DAY_TARGET_MIN_PCT, DAY_TARGET_MAX_PCT = 0.5, 10.0


def parse_day_target(value) -> Optional[float]:
    """A day target typed as a percent ("2.5", "2.5%") -> 0.025; None when blank, not a number or out of
    bounds."""
    text = str(value or "").strip().rstrip("%").strip()
    if not text:
        return None
    try:
        pct = float(text)
    except ValueError:
        return None
    if not (DAY_TARGET_MIN_PCT <= pct <= DAY_TARGET_MAX_PCT):
        return None
    return round(pct, 4) / 100.0


def params_for(strategy_version: Optional[str], day_target_pct: Optional[float] = None) -> ProposerParams:
    """The parameter set a strategy version trades with. `day_target_pct` (a fraction) replaces the day
    target of the versions in DAY_TARGET_VERSIONS; other versions ignore it."""
    params = _preset_params(strategy_version)
    if day_target_pct and str(strategy_version or "") in DAY_TARGET_VERSIONS:
        params = replace(params, daily_target_pct=float(day_target_pct))
    return params


def _preset_params(strategy_version: Optional[str]) -> ProposerParams:
    if str(strategy_version or "") == STRATEGY_VERSION_NIFTY_SCALPER_V1:
        # Approved 2026-10-21 after the re-entry-loop replay (plan section 8).
        return replace(_preset_params(STRATEGY_VERSION_V5), daily_target_pct=0.015,
                       confirm_bank_tolerance=0.25)
    if str(strategy_version or "") == STRATEGY_VERSION_PX:
        return ProposerParams(bar_exit=PX_BAR_EXIT, max_losses_per_day=1, reentry_cooldown_secs=REENTRY_COOLDOWN_S)
    if str(strategy_version or "") == STRATEGY_VERSION_V3:
        return ProposerParams(with_day_pts=V3_WITH_DAY_PTS, loss_floor_pct=V3_LOSS_FLOOR_PCT,
                              reentry_cooldown_secs=REENTRY_COOLDOWN_S)
    if str(strategy_version or "") == STRATEGY_VERSION_V3_DT35:
        return ProposerParams(
            with_day_pts=V3_WITH_DAY_PTS,
            loss_floor_pct=V3_LOSS_FLOOR_PCT,
            daily_target_pct=V3_DT35_DAY_TARGET_PCT,
            reentry_cooldown_secs=REENTRY_COOLDOWN_S,
        )
    if str(strategy_version or "") == STRATEGY_VERSION_V5:
        return ProposerParams(
            with_day_pts=V3_WITH_DAY_PTS,
            loss_floor_pct=NO_PREMIUM_FLOOR_PCT,
            spot_floor_pts=V5_SPOT_FLOOR_PTS,
            daily_target_pct=V3_DT35_DAY_TARGET_PCT,
            reentry_cooldown_secs=REENTRY_COOLDOWN_S,
        )
    return ProposerParams(reentry_cooldown_secs=REENTRY_COOLDOWN_S)


# Every version params_for knows; a runner must not trade any other Proposer version (it would fall back
# to the base preset's rules).
KNOWN_VERSIONS = frozenset({STRATEGY_VERSION, STRATEGY_VERSION_PX, STRATEGY_VERSION_V3, STRATEGY_VERSION_V3_DT35,
                            STRATEGY_VERSION_V5, STRATEGY_VERSION_NIFTY_SCALPER_V1})


def scaled_for_nifty(params: ProposerParams, ratio: float) -> ProposerParams:
    """NIFTY exits scaled by the two 09:15 opens; the SENSEX entry filter stays unscaled."""
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("NIFTY/SENSEX opening ratio must be finite and positive")
    return replace(params,
                   spot_target_decisive_pts=params.spot_target_decisive_pts * ratio * 1.5,
                   spot_target_neutral_pts=params.spot_target_neutral_pts * ratio * 1.5,
                   spot_target_tolerance_pts=params.spot_target_tolerance_pts * ratio,
                   spot_floor_pts=params.spot_floor_pts * ratio)


@dataclass(frozen=True)
class Signal:
    action: str                     # ENTER | EXIT | HOLD
    side: Optional[str] = None      # CALL | PUT
    reason: Optional[str] = None


@dataclass
class Position:
    side: Optional[str] = None      # CE | PE when open
    entry_price: float = 0.0
    entry_spot: float = 0.0
    qty: int = 0

    @property
    def open(self) -> bool:
        return self.side is not None


def entry_side_5class(x5: Optional[str]) -> Optional[str]:
    x5 = (x5 or "").lower()
    if x5 in ("mild_bull", "strong_bull"):
        return "CALL"
    if x5 in ("mild_bear", "strong_bear"):
        return "PUT"
    return None


def is_strong_reversal(regime: Optional[str], x5: Optional[str]) -> bool:
    regime, x5 = (regime or "").lower(), (x5 or "").lower()
    return (regime == "bullish" and x5 == "strong_bear") or \
           (regime in ("bearish", "risk_off") and x5 == "strong_bull")


def entry_side_v1d(regime: Optional[str], x5: Optional[str], stale: bool) -> Optional[str]:
    regime = (regime or "").lower()
    if not stale:
        if regime == "bullish":
            return "CALL"
        if regime in ("bearish", "risk_off"):
            return "PUT"
    return entry_side_5class(x5)


@dataclass
class ProposerEngine:
    params: ProposerParams = field(default_factory=ProposerParams)

    def __post_init__(self):
        self.reset_session()

    # ------------------------------------------------------------ session ---
    def reset_session(self) -> None:
        self.regime_stale = False
        self.regime_entry_used = False
        self.entry_decisive = False
        self.skip_cd_arm = False
        self.cooldown_until: Optional[datetime] = None
        self.prev_open = False
        self.last_acted_asof: Optional[str] = None
        self.day_done = False
        self.last_entry_capital = 0.0
        self.bank_target_rs = 0.0
        self.day_real_at_latch: Optional[float] = None
        self.day_real = 0.0
        self.book_net = 0.0
        self.day_losses = 0

    def restore(self, *, regime: Optional[str] = None, x5_today: Iterable[str] = (),
                regime_entry_used: bool = False, day_target_banked: bool = False,
                last_entry_capital: float = 0.0, last_acted_asof: Optional[str] = None) -> None:
        """Restart-safe state from today's immutable history (prints and trades)."""
        if regime in DECISIVE and any(is_strong_reversal(regime, t) for t in x5_today):
            self.regime_stale = True
        self.regime_entry_used = self.regime_entry_used or bool(regime_entry_used)
        self.day_done = self.day_done or bool(day_target_banked)
        if last_entry_capital > 0 and self.last_entry_capital <= 0:
            self.last_entry_capital = float(last_entry_capital)
        if last_acted_asof:
            self.last_acted_asof = last_acted_asof

    def set_book(self, *, day_realized: float, book_net: float, day_losses: int = 0) -> None:
        self.day_real = float(day_realized or 0.0)
        self.book_net = float(book_net or 0.0)
        self.day_losses = int(day_losses or 0)

    # ------------------------------------------------------------ helpers ---
    def _gated_side(self, p: dict) -> Optional[str]:
        stale = self.regime_stale or self.regime_entry_used
        cp = entry_side_v1d(p.get("regime"), p.get("x5"), stale)
        if not cp:
            return None
        by_5class = stale or (p.get("regime") or "").lower() not in DECISIVE
        if self.params.conf_gate > 0 and by_5class and float(p.get("x5_conf") or 0) <= self.params.conf_gate:
            return None
        return cp

    def _fresh(self, p: dict, now: datetime) -> bool:
        asof = p.get("x5_asof")
        if not asof:
            return False
        age = (now - datetime.fromisoformat(asof)).total_seconds()
        return 0 <= age <= self.params.predictor_max_age_secs

    def mark_entry(self, p: dict) -> None:
        """Call once the entry has filled: consumes the regime licence, fixes the target width."""
        regime = (p.get("regime") or "").lower()
        driven = regime in DECISIVE and not self.regime_stale and not self.regime_entry_used
        if driven:
            self.regime_entry_used = True
        self.entry_decisive = driven

    def banked(self) -> bool:
        if self.day_done:
            return True
        return (self.last_entry_capital > 0
                and self.day_real >= self.params.daily_target_pct * self.last_entry_capital
                and self.book_net >= self.params.recovery_min_book_net)

    def _confirm_bank(self) -> None:
        if not (self.params.confirm_bank_on_realized and self.day_done and self.day_real_at_latch is not None):
            return
        if self.day_real == self.day_real_at_latch:
            return                                  # the exit's P&L is not booked yet
        if self.day_real >= self.bank_target_rs * (1.0 - self.params.confirm_bank_tolerance):
            self.day_real_at_latch = None
            return
        self.day_done = False                       # the fill landed well short: keep trading
        self.day_real_at_latch = None
        self.bank_target_rs = 0.0

    # ----------------------------------------------------------- evaluate ---
    def evaluate(self, now: datetime, p: dict, pos: Position, *, option_ltp: Optional[float],
                 spot: Optional[float], closes: Optional[Sequence[float]] = None,
                 entry_idx: Optional[int] = None, day_move: Optional[float] = None) -> Signal:
        """`closes` / `entry_idx` feed the bar_exit: the session's completed 1-min SENSEX closes and
        the index of the bar the open position was entered in. `day_move` feeds the with-the-day
        entry filter: the last completed 1-min close minus the 09:15 close (None = not known)."""
        if not self.regime_stale and is_strong_reversal(p.get("regime"), p.get("x5")):
            self.regime_stale = True
        if self.prev_open and not pos.open:         # an exit just completed
            if not self.skip_cd_arm and self.params.reentry_cooldown_secs > 0:
                self.cooldown_until = now + timedelta(seconds=self.params.reentry_cooldown_secs)
            self.skip_cd_arm = False
        self.prev_open = pos.open

        if pos.open:
            return self._evaluate_open(p, pos, option_ltp, spot, closes, entry_idx)

        self._confirm_bank()
        asof = p.get("x5_asof")
        fresh_print = bool(asof) and asof != self.last_acted_asof
        if not fresh_print:
            return Signal("HOLD")
        self.last_acted_asof = asof                 # consumed, even if blocked below
        if self.cooldown_until is not None and now < self.cooldown_until:
            return Signal("HOLD", reason="cooldown")
        if not self._fresh(p, now):
            return Signal("HOLD", reason="predictor_stale")
        cp = self._gated_side(p)
        if not cp:
            return Signal("HOLD")
        if self.banked():
            return Signal("HOLD", reason="day_target_banked")
        if self.params.max_losses_per_day and self.day_losses >= self.params.max_losses_per_day:
            return Signal("HOLD", reason="day_loss_limit")
        if self.params.with_day_pts > 0:
            if day_move is None:                    # never enter on an unknown day direction
                return Signal("HOLD", reason="day_move_unknown")
            if (day_move if cp == "CALL" else -day_move) < self.params.with_day_pts:
                return Signal("HOLD", reason="against_the_day")
        stale = self.regime_stale or self.regime_entry_used
        tag = f"5class_{p.get('x5')}" if stale or (p.get("regime") or "").lower() not in DECISIVE \
            else (p.get("regime") or "").lower()
        return Signal("ENTER", cp, f"proposer_{tag}")

    def _evaluate_open(self, p: dict, pos: Position, ltp: Optional[float], spot: Optional[float],
                       closes: Optional[Sequence[float]] = None, entry_idx: Optional[int] = None) -> Signal:
        cp = "CALL" if pos.side == "CE" else "PUT"
        if pos.entry_price and pos.qty:
            self.last_entry_capital = float(pos.entry_price) * pos.qty
        if ltp and pos.entry_price and (ltp - pos.entry_price) / pos.entry_price <= self.params.loss_floor_pct:
            self.skip_cd_arm = False
            return Signal("EXIT", cp, "loss_floor")
        if spot is not None and pos.entry_spot:
            move = (spot - pos.entry_spot) if pos.side == "CE" else (pos.entry_spot - spot)
            target = self.params.spot_target_decisive_pts if self.entry_decisive else self.params.spot_target_neutral_pts
            if move >= target - self.params.spot_target_tolerance_pts:
                self.skip_cd_arm = False
                return Signal("EXIT", cp, "spot_target")
        new_side = self._gated_side(p)
        if new_side and new_side != cp:
            self.skip_cd_arm = True
            if not self.params.reverse_after_flip:
                self.last_acted_asof = p.get("x5_asof")
            return Signal("EXIT", cp, "signal_flip")
        if ltp and self.last_entry_capital > 0:
            target_rs = self.params.daily_target_pct * self.last_entry_capital
            unreal = (float(ltp) - float(pos.entry_price)) * pos.qty
            if (self.day_real + unreal >= target_rs
                    and self.book_net + unreal >= self.params.recovery_min_book_net):
                self.day_done = True
                self.skip_cd_arm = False
                if self.params.confirm_bank_on_realized:
                    self.bank_target_rs = target_rs
                    self.day_real_at_latch = self.day_real
                return Signal("EXIT", cp, "daily_target")
        if (self.params.bar_exit and closes is not None and entry_idx is not None
                and bar_exit.fires(self.params.bar_exit, closes, entry_idx, pos.side)):
            self.skip_cd_arm = False
            return Signal("EXIT", cp, "bar_exit")
        if self.params.spot_floor_pts > 0 and spot is not None and pos.entry_spot:
            against = (pos.entry_spot - spot) if pos.side == "CE" else (spot - pos.entry_spot)
            if against >= self.params.spot_floor_pts:
                self.skip_cd_arm = False
                return Signal("EXIT", cp, "spot_floor")
        return Signal("HOLD")


def itm_strike(spot: float, side: str, itm_pts: int = 200) -> tuple[int, str]:
    """ATM (nearest 100) +/- itm_pts in the money: CALL -> CE below, PUT -> PE above."""
    atm = round(spot / 100) * 100
    return (atm - itm_pts, "CE") if side == "CALL" else (atm + itm_pts, "PE")
