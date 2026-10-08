"""CRUDEOILM "consistent" six-member combination: signals, exits and the one-position merge.

Pure (numpy / pandas only, no I/O, no broker). A faithful port of the parts of Strategy Tester v2
that define saved combination c8 of run ``results_v2/crudeoilm/crudem_20261005`` - spec in
``Strategy Tester/research/PAPER_SPEC_CRUDEM_CONSISTENT_COMBO.md``. The engine's code is the
definition; this file follows it function by function:

  bars          strategy_tester/data/bars.py        full 09:00-23:29 grid per session, gaps = NaN
  timeframes    strategy_tester/data/resample.py    session-anchored bins; a higher-timeframe value
                                                    is used only once its bar has finished
  indicators    strategy_tester/indicators/core.py  TradingView conventions (EMA / RMA seeded with
                                                    the SMA of the first n values, population std);
                                                    computed on valid bars only
  operators     strategy_tester/signals/ops.py      NaN -> False; a crossing is a one-bar pulse on
                                                    its own timeframe, held on the 1-minute clock
                                                    until the next bar of that timeframe closes
  fires         strategy_tester_v2/kernels.py       rising edge of AND(trigger states), every gate
                                                    on at that bar, 30-bar cooldown per member
  exits         strategy_tester/outcomes/simulate.py entry at the next bar's open; open beyond the
                                                    stop or target exits at the open; stop wins
                                                    when both are touched; flat at the session close
  merge         strategy_tester_v2/ui/saved.py      rule first_in, applied member by member

It is a FIT to 1 Jun - 6 Oct 2026, not a tested edge; nothing here may be tuned from paper or live
results. tests/test_crudem_combo.py checks this port against the engine's 136 reference trades.

It lives under live/engine so the real-money stack can use it (live/ never imports labs.engine);
the paper book (labs/engine/crudem_combo_tracker.py) imports it from here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd

SESSION_OPEN_MIN = 9 * 60            # 09:00, first bar (bars are stamped with their open time)
SESSION_CLOSE_MIN = 23 * 60 + 29     # 23:29, last bar
BARS_PER_DAY = SESSION_CLOSE_MIN - SESSION_OPEN_MIN + 1
MIN_VALID_FRAC = 0.5                 # a session with fewer valid bars takes no entries
COOLDOWN_BARS = 30
MAX_HOLD_BARS = 870
ATR_TF, ATR_N = 5, 14
LOT_QTY = 10                         # one CRUDEOILM lot = 10 barrels
TICK_SIZE = 1.0


# ------------------------------------------------------------------ members ---
@dataclass(frozen=True)
class Member:
    cid: int
    side: int                        # +1 long, -1 short
    triggers: tuple[str, ...]
    gates: tuple[str, ...]
    stop_kind: str                   # "pct" (of the entry price) | "atr" (x ATR(14) on 5m at the signal)
    stop_value: float
    target_r: float

    @property
    def exit_desc(self) -> str:
        stop = f"{self.stop_value:g}%" if self.stop_kind == "pct" else f"{self.stop_value:g} x ATR(14) 5m"
        return f"stop {stop}, target {self.target_r:g}R"


# building blocks: name -> (indicator, timeframe minutes, params, operator, lhs, rhs)
SPECS: dict[str, tuple] = {
    "rsi2@5m<30":          ("rsi", 5, {"n": 2}, "lt", "rsi", 30.0),
    "roc5@5m x<0":         ("roc", 5, {"n": 5}, "cross_below", "roc", 0.0),
    "roc5@15m x<0":        ("roc", 15, {"n": 5}, "cross_below", "roc", 0.0),
    "macd(5,35,5)@1h>0":   ("macd", 60, {"fast": 5, "slow": 35, "signal": 5}, "gt", "hist", 0.0),
    "macd(8,21,5)@1h>0":   ("macd", 60, {"fast": 8, "slow": 21, "signal": 5}, "gt", "hist", 0.0),
    "bb(10,2)@30m x<up":   ("bbands", 30, {"n": 10, "k": 2.0}, "cross_below", "close", "upper"),
    "stoch(5,3)@5m k<10":  ("stoch", 5, {"k": 5}, "lt", "k", 10.0),
    "wr9@5m<-80":          ("williams_r", 5, {"n": 9}, "lt", "wr", -80.0),
    "stoch(14,3)@5m k<20": ("stoch", 5, {"k": 14}, "lt", "k", 20.0),
    "kc(20,2)@15m x<up":   ("keltner", 15, {"n": 20, "mult": 2.0}, "cross_below", "close", "upper"),
    "ema9@5m close x>":    ("ema", 5, {"n": 9}, "cross_above", "close", "ma"),
    "wr9@15m>-20":         ("williams_r", 15, {"n": 9}, "gt", "wr", -20.0),
    "adx14@15m DI->DI+":   ("adx", 15, {"n": 14}, "lt", "plus_di", "minus_di"),
    "rsi14@5m x>50":       ("rsi", 5, {"n": 14}, "cross_above", "rsi", 50.0),
    "rsi3@15m<20":         ("rsi", 15, {"n": 3}, "lt", "rsi", 20.0),
}

# in priority order (the order the combination was built in)
MEMBERS: tuple[Member, ...] = (
    Member(593002, +1, ("rsi2@5m<30", "roc5@5m x<0", "roc5@15m x<0"), ("macd(5,35,5)@1h>0",), "pct", 1.0, 3.0),
    Member(506248, +1, ("bb(10,2)@30m x<up", "stoch(5,3)@5m k<10", "wr9@5m<-80"), (), "pct", 1.0, 3.0),
    Member(623555, -1, ("stoch(14,3)@5m k<20", "kc(20,2)@15m x<up"), (), "pct", 1.0, 0.5),
    Member(511479, -1, ("ema9@5m close x>", "wr9@15m>-20"), ("adx14@15m DI->DI+",), "pct", 0.5, 1.0),
    Member(2803, -1, ("rsi14@5m x>50", "rsi3@15m<20"), (), "atr", 2.0, 1.0),
    Member(595026, +1, ("rsi2@5m<30", "roc5@5m x<0", "roc5@15m x<0"), ("macd(8,21,5)@1h>0",), "pct", 1.0, 3.0),
)
MEMBER_BY_CID = {m.cid: m for m in MEMBERS}


# -------------------------------------------------------------------- bars ---
def build_bars(frame: pd.DataFrame, cutoff: datetime | None = None) -> dict:
    """The full gap-filled 1-minute grid for every weekday session present in ``frame``.

    ``frame``: ts (naive IST, bar open), open, high, low, close. ``cutoff`` is the first minute not
    yet complete (live): bars from it on are dropped, the session in progress keeps its full grid
    with the future minutes as gaps, exactly as the engine would see a file that ends mid-session.
    """
    ts = pd.to_datetime(frame["ts"]).dt.floor("min")
    minute = (ts.dt.hour * 60 + ts.dt.minute).to_numpy()
    keep = (minute >= SESSION_OPEN_MIN) & (minute <= SESSION_CLOSE_MIN) & (ts.dt.weekday < 5).to_numpy()
    if cutoff is not None:
        keep &= (ts < pd.Timestamp(cutoff).floor("min")).to_numpy()
    f = frame.loc[keep].assign(ts=ts[keep]).sort_values("ts", kind="stable").drop_duplicates("ts", keep="last")
    days = np.array(sorted(f["ts"].dt.date.unique()), dtype=object)
    n_days = days.size
    n = n_days * BARS_PER_DAY
    arr = {c: np.full(n, np.nan) for c in ("open", "high", "low", "close")}
    valid = np.zeros(n, dtype=bool)
    if n_days:
        index = {d: k for k, d in enumerate(days)}
        pos = (f["ts"].dt.date.map(index).to_numpy(np.int64) * BARS_PER_DAY
               + (f["ts"].dt.hour * 60 + f["ts"].dt.minute).to_numpy() - SESSION_OPEN_MIN)
        for c in arr:
            arr[c][pos] = pd.to_numeric(f[c], errors="coerce").to_numpy(np.float64)
        valid[pos] = True
        valid &= np.isfinite(arr["open"]) & np.isfinite(arr["high"]) & np.isfinite(arr["low"]) & np.isfinite(arr["close"])
        for c in arr:
            arr[c][~valid] = np.nan
    day_id = np.repeat(np.arange(n_days, dtype=np.int64), BARS_PER_DAY)
    bar_in_day = np.tile(np.arange(BARS_PER_DAY, dtype=np.int64), n_days)
    day0 = np.array([np.datetime64(d, "m") for d in days], dtype="datetime64[m]") if n_days \
        else np.array([], dtype="datetime64[m]")
    ts_min = day0[day_id] + (SESSION_OPEN_MIN + bar_in_day).astype("timedelta64[m]") if n \
        else np.array([], dtype="datetime64[m]")
    counts = np.bincount(day_id[valid], minlength=n_days) if n else np.zeros(n_days, np.int64)
    day_len = np.full(n_days, BARS_PER_DAY, dtype=np.int64)
    live_day = None
    n_known = n                                  # bars before the cutoff (the rest of a live day is unknown)
    if cutoff is not None and n_days and days[-1] == pd.Timestamp(cutoff).date():
        elapsed = int(max(0, min(BARS_PER_DAY, cutoff.hour * 60 + cutoff.minute - SESSION_OPEN_MIN)))
        if elapsed < BARS_PER_DAY:
            # the session is not over: judge its completeness on the minutes elapsed so far
            day_len[-1], live_day = max(elapsed, 1), n_days - 1
            n_known = (n_days - 1) * BARS_PER_DAY + elapsed
    day_ok = counts >= MIN_VALID_FRAC * day_len
    return {**arr, "valid": valid, "day_id": day_id, "bar_in_day": bar_in_day, "ts": ts_min, "days": days,
            "day_ok": day_ok, "valid_counts": counts, "n": n, "n_known": n_known, "live_day": live_day}


def resample(g: dict, tf: int) -> dict:
    """Session-anchored ``tf``-minute bars; the last bin of a day is cut at the session close."""
    n = g["n"]
    bins = -(-BARS_PER_DAY // tf)
    key = g["day_id"] * bins + g["bar_in_day"] // tf
    change = np.ones(n, dtype=bool)
    change[1:] = key[1:] != key[:-1]
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], n)
    m = starts.size
    o, h, l, c = (np.full(m, np.nan) for _ in range(4))
    ok = np.zeros(m, dtype=bool)
    valid = g["valid"]
    if m:
        bin_of = np.cumsum(change) - 1
        v = np.flatnonzero(valid)
        vb = bin_of[v]
        first = np.r_[True, vb[1:] != vb[:-1]]
        last = np.r_[vb[1:] != vb[:-1], True]
        ok[vb[first]] = True
        o[vb[first]] = g["open"][v[first]]
        c[vb[last]] = g["close"][v[last]]
        hi = pd.Series(g["high"][v]).groupby(vb).max()
        lo = pd.Series(g["low"][v]).groupby(vb).min()
        h[hi.index.to_numpy()] = hi.to_numpy()
        l[lo.index.to_numpy()] = lo.to_numpy()
    close_ts = g["ts"][ends - 1] + np.timedelta64(1, "m") if m else np.array([], dtype="datetime64[m]")
    # for each base bar, the last bin complete by the close of that base bar (-1: none)
    mapping = np.searchsorted(close_ts, g["ts"] + np.timedelta64(1, "m"), side="right") - 1 if m \
        else np.full(n, -1, dtype=np.int64)
    return {"open": o, "high": h, "low": l, "close": c, "valid": ok, "close_ts": close_ts, "map": mapping, "n": m}


def broadcast(x: np.ndarray, mapping: np.ndarray) -> np.ndarray:
    if x.dtype == np.bool_:
        out = x[np.maximum(mapping, 0)].copy()
        out[mapping < 0] = False
        return out
    out = x[np.maximum(mapping, 0)].astype(np.float64, copy=True)
    out[mapping < 0] = np.nan
    return out


# -------------------------------------------------------------- indicators ---
def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.shape[0], np.nan)
    s, cnt = 0.0, 0
    for i in range(x.shape[0]):
        if math.isfinite(x[i]):
            s += x[i]
            cnt += 1
        if i >= n:
            old = x[i - n]
            if math.isfinite(old):
                s -= old
                cnt -= 1
        if cnt == n:
            out[i] = s / n
    return out


def ewm_seeded(x: np.ndarray, alpha: float, n: int) -> np.ndarray:
    """EMA-style recursion seeded with the SMA of the first n consecutive finite values."""
    out = np.full(x.shape[0], np.nan)
    run, s, seeded, prev = 0, 0.0, False, 0.0
    for i in range(x.shape[0]):
        v = x[i]
        if not seeded:
            if math.isfinite(v):
                run += 1
                s += v
                if run == n:
                    prev = s / n
                    out[i] = prev
                    seeded = True
            else:
                run, s = 0, 0.0
        elif math.isfinite(v):
            prev = alpha * v + (1.0 - alpha) * prev
            out[i] = prev
    return out


def ema(x, n: int) -> np.ndarray:
    return ewm_seeded(np.asarray(x, dtype=np.float64), 2.0 / (n + 1.0), int(n))


def rma(x, n: int) -> np.ndarray:
    return ewm_seeded(np.asarray(x, dtype=np.float64), 1.0 / n, int(n))


def _rolling(x: np.ndarray, n: int, func) -> np.ndarray:
    out = np.full(x.shape[0], np.nan)
    if x.shape[0] >= n:
        w = np.lib.stride_tricks.sliding_window_view(x, n)
        ok = np.isfinite(w).all(axis=1)
        out[n - 1:][ok] = func(w[ok])
    return out


def rolling_max(x, n):
    return _rolling(x, n, lambda w: w.max(axis=1))


def rolling_min(x, n):
    return _rolling(x, n, lambda w: w.min(axis=1))


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    """Population standard deviation, summed in bar order as the engine does."""
    out = np.full(x.shape[0], np.nan)
    for i in range(n - 1, x.shape[0]):
        s, ok = 0.0, True
        for j in range(i - n + 1, i + 1):
            if not math.isfinite(x[j]):
                ok = False
                break
            s += x[j]
        if not ok:
            continue
        mu, q = s / n, 0.0
        for j in range(i - n + 1, i + 1):
            q += (x[j] - mu) ** 2
        out[i] = math.sqrt(q / n)
    return out


def shift(x: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=np.float64)
    out[k:] = x[:-k]
    return out


def true_range(high, low, close) -> np.ndarray:
    pc = shift(close)
    tr = np.fmax(high - low, np.fmax(np.abs(high - pc), np.abs(low - pc)))
    tr[0] = high[0] - low[0]
    return tr


def _safe_div(a, b) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = a / b
    out[~np.isfinite(out)] = np.nan
    return out


def _indicator(name: str, high, low, close, **p) -> dict[str, np.ndarray]:
    if name == "rsi":
        d = np.diff(close, prepend=np.nan)
        up = np.where(np.isnan(d), np.nan, np.maximum(d, 0.0))
        dn = np.where(np.isnan(d), np.nan, np.maximum(-d, 0.0))
        au, ad = rma(up, p["n"]), rma(dn, p["n"])
        with np.errstate(divide="ignore", invalid="ignore"):
            r = 100.0 - 100.0 / (1.0 + au / ad)
        return {"rsi": np.where((ad == 0) & np.isfinite(au), 100.0, r)}
    if name == "roc":
        return {"roc": 100.0 * _safe_div(close - shift(close, p["n"]), shift(close, p["n"]))}
    if name == "macd":
        line = ema(close, p["fast"]) - ema(close, p["slow"])
        return {"hist": line - ema(line, p["signal"])}
    if name == "bbands":
        return {"upper": sma(close, p["n"]) + p["k"] * rolling_std(close, p["n"])}
    if name == "stoch":
        hh, ll = rolling_max(high, p["k"]), rolling_min(low, p["k"])
        return {"k": 100.0 * _safe_div(close - ll, hh - ll)}
    if name == "williams_r":
        hh, ll = rolling_max(high, p["n"]), rolling_min(low, p["n"])
        return {"wr": -100.0 * _safe_div(hh - close, hh - ll)}
    if name == "keltner":
        return {"upper": ema(close, p["n"]) + p["mult"] * rma(true_range(high, low, close), p["n"])}
    if name == "ema":
        return {"ma": ema(close, p["n"])}
    if name == "atr":
        return {"atr": rma(true_range(high, low, close), p["n"])}
    if name == "adx":
        n = p["n"]
        up, dn = high - shift(high), shift(low) - low
        pdm = np.where((up > dn) & (up > 0), up, 0.0)
        mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
        pdm[0] = mdm[0] = np.nan
        tr = true_range(high, low, close)
        tr[0] = np.nan
        atr_ = rma(tr, n)
        return {"plus_di": 100.0 * _safe_div(rma(pdm, n), atr_), "minus_di": 100.0 * _safe_div(rma(mdm, n), atr_)}
    raise KeyError(name)


def indicator(name: str, bars: dict, **params) -> dict[str, np.ndarray]:
    """Computed on the valid bars only (so a gap never poisons a recursion), NaN on the gaps."""
    idx = np.flatnonzero(bars["valid"])
    out = _indicator(name, *(np.ascontiguousarray(bars[c][idx]) for c in ("high", "low", "close")), **params)
    full = {}
    for k, v in out.items():
        f = np.full(bars["n"], np.nan)
        f[idx] = v
        full[k] = f
    return full


# --------------------------------------------------------------- operators ---
def _prev(x: np.ndarray) -> np.ndarray:
    out = np.empty_like(x)
    out[0] = np.nan
    out[1:] = x[:-1]
    return out


def _operand(x, vals: dict, bars: dict, n: int) -> np.ndarray:
    if isinstance(x, str):
        return vals[x] if x in vals else bars[x]
    return np.full(n, float(x))


def spec_state(name: str, g: dict, tfs: dict, cache: dict) -> np.ndarray:
    """One building block's state on the 1-minute clock."""
    ind, tf, params, op, lhs, rhs = SPECS[name]
    bars = tfs[tf]
    key = (ind, tf, tuple(sorted(params.items())))
    if key not in cache:
        cache[key] = indicator(ind, bars, **params)
    vals = cache[key]
    a, b = _operand(lhs, vals, bars, bars["n"]), _operand(rhs, vals, bars, bars["n"])
    with np.errstate(invalid="ignore"):
        if op == "lt":
            state = a < b
        elif op == "gt":
            state = a > b
        elif op == "cross_above":
            state = (_prev(a) <= _prev(b)) & (a > b)
        elif op == "cross_below":
            state = (_prev(a) >= _prev(b)) & (a < b)
        else:
            raise ValueError(op)
    return broadcast(np.asarray(state, dtype=bool), bars["map"]) & g["valid"]


# ------------------------------------------------------------------- fires ---
def entry_ok(g: dict) -> np.ndarray:
    """Signal bars i that may open a trade at open[i + 1]: both bars valid, same (usable) session."""
    n = g["n"]
    ok = np.zeros(n, dtype=bool)
    if n > 1:
        nxt = g["day_id"][1:]
        ok[:-1] = g["valid"][:-1] & g["valid"][1:] & g["day_ok"][nxt] & (nxt == g["day_id"][:-1])
    return ok


def member_events(member: Member, states: dict, eligible: np.ndarray) -> list[tuple[int, str]]:
    """Every rising edge of the member's trigger states, with what became of it:
    'gate' (a gate was off), 'cooldown', 'ineligible' (no valid next bar in the session, or outside
    the traded range) or 'fire'. A gated event restarts the cooldown whether or not it is eligible."""
    on = np.ones_like(eligible)
    for s in member.triggers:
        on = on & states[s]
    edge = on.copy()
    edge[1:] &= ~on[:-1]
    gate = np.ones_like(eligible)
    for s in member.gates:
        gate = gate & states[s]
    out, last = [], -(1 << 60)
    for x in np.flatnonzero(edge):
        x = int(x)
        if not gate[x]:
            out.append((x, "gate"))
            continue
        if x - last > COOLDOWN_BARS:
            last = x
            out.append((x, "fire" if eligible[x] else "ineligible"))
        else:
            out.append((x, "cooldown"))
    return out


# ------------------------------------------------------------------- exits ---
def simulate(g: dict, i: int, side: int, stop_dist: float, target_r: float) -> dict | None:
    """Walk one trade entered at open[i + 1]. None if the stop distance is unusable.

    Returns status 'closed' (exit_idx, exit price, reason stop|target|time|eod) or 'open' (the
    session in progress has not produced an exit yet)."""
    if not (stop_dist > 0.0) or not math.isfinite(stop_dist):
        return None
    o_, h_, l_, c_, valid, day_id = g["open"], g["high"], g["low"], g["close"], g["valid"], g["day_id"]
    n, n_known = g["n"], g["n_known"]
    j0 = i + 1
    p = float(o_[j0])
    day = day_id[j0]
    stop, target = p - side * stop_dist, p + side * target_r * stop_dist
    base = {"entry": p, "stop": stop, "target": target, "stop_dist": stop_dist}
    k, last_j, j = 0, -1, j0
    while True:
        if j >= n_known and day == g["live_day"]:
            return {**base, "status": "open", "held": k, "mark_idx": last_j}
        if j >= n or day_id[j] != day:
            if last_j < 0:
                return None
            return {**base, "status": "closed", "exit_idx": last_j, "exit": float(c_[last_j]), "reason": "eod",
                    "held": k}
        if not valid[j]:
            j += 1
            continue
        k += 1
        last_j = j
        o = float(o_[j])
        if side * (o - stop) <= 0.0:                     # opened beyond the stop
            return {**base, "status": "closed", "exit_idx": j, "exit": o, "reason": "stop", "held": k}
        if side * (o - target) >= 0.0:                   # opened beyond the target
            return {**base, "status": "closed", "exit_idx": j, "exit": o, "reason": "target", "held": k}
        adverse, fav = (l_[j], h_[j]) if side > 0 else (h_[j], l_[j])
        if side * (adverse - stop) <= 0.0:               # the stop wins when both are touched
            return {**base, "status": "closed", "exit_idx": j, "exit": stop, "reason": "stop", "held": k}
        if side * (fav - target) >= 0.0:
            return {**base, "status": "closed", "exit_idx": j, "exit": target, "reason": "target", "held": k}
        if k >= MAX_HOLD_BARS:
            return {**base, "status": "closed", "exit_idx": j, "exit": float(c_[j]), "reason": "time", "held": k}
        j += 1


# ------------------------------------------------------------------- merge ---
def first_in(a: list[dict], b: list[dict]) -> tuple[list[dict], list[dict]]:
    """The app's ``first_in`` rule for two trade lists: a trade is skipped while the OTHER side has a
    taken trade still open at its signal time; on the same minute side ``a`` wins. Returns (taken,
    skipped); a skipped trade carries ``blocked_by`` (the cid holding the position)."""
    rows = sorted([(t["signal_idx"], 0, t) for t in a] + [(t["signal_idx"], 1, t) for t in b],
                  key=lambda r: (r[0], r[1]))
    open_: dict[int, list[dict]] = {0: [], 1: []}
    taken, skipped = [], []
    for sig, side, t in rows:
        live = [x for x in open_[1 - side] if x["exit_idx"] is None or x["exit_idx"] > sig]
        open_[1 - side] = live
        if live:
            skipped.append({**t, "blocked_by": live[0]["cid"]})
            continue
        open_[side].append(t)
        taken.append(t)
    taken.sort(key=lambda t: t["entry_idx"])
    return taken, skipped


# ------------------------------------------------------------------ replay ---
def replay(frame: pd.DataFrame, start: date | str, cutoff: datetime | None = None,
           end: date | str | None = None) -> dict:
    """Run the six members and the merge over ``frame`` (rolled, back-adjusted 1-minute bars).

    Sessions before ``start`` (and after ``end``) are warm-up only: their signals take no trades
    but still run the cooldown, as in the engine. Returns ``{"grid", "trades", "members",
    "signals"}``: the combination's trades, each member's own trades, and every trigger event of
    every member with its outcome."""
    g = build_bars(frame, cutoff)
    start = date.fromisoformat(str(start)[:10])
    end = date.fromisoformat(str(end)[:10]) if end is not None else None
    in_range = np.array([d >= start and (end is None or d <= end) for d in g["days"]], dtype=bool) & g["day_ok"]
    n = g["n"]
    empty = {"grid": g, "trades": [], "members": {m.cid: [] for m in MEMBERS}, "signals": []}
    if n < 2:
        return empty
    window = in_range[g["day_id"]]
    eligible = entry_ok(g) & window
    eligible[:-1] &= window[1:]
    eligible[n - 1] = False
    eligible[g["n_known"] - 1:] = False               # the next bar's open is not known yet

    tfs = {tf: resample(g, tf) for tf in (5, 15, 30, 60)}
    cache: dict = {}
    states = {name: spec_state(name, g, tfs, cache) for name in SPECS}
    atr5 = broadcast(indicator("atr", tfs[ATR_TF], n=ATR_N)["atr"], tfs[ATR_TF]["map"])

    ts = g["ts"]
    members: dict[int, list[dict]] = {}
    signals: list[dict] = []
    for m in MEMBERS:
        trades, busy_until = [], -1
        for x, what in member_events(m, states, eligible):
            row = {"cid": m.cid, "signal_idx": x, "signal_ts": pd.Timestamp(ts[x]), "outcome": what}
            if what == "fire":
                if busy_until is None or x < busy_until:
                    row["outcome"] = "member_in_trade"
                else:
                    dist = float(g["open"][x + 1]) * m.stop_value / 100.0 if m.stop_kind == "pct" \
                        else float(atr5[x]) * m.stop_value
                    res = simulate(g, x, m.side, dist, m.target_r)
                    if res is None:
                        row["outcome"] = "no_stop_distance"
                    else:
                        closed = res["status"] == "closed"
                        mark = res["exit"] if closed else \
                            (float(g["close"][res["mark_idx"]]) if res["mark_idx"] >= 0 else res["entry"])
                        trades.append({
                            "cid": m.cid, "side": m.side, "signal_idx": x, "entry_idx": x + 1,
                            "exit_idx": res["exit_idx"] if closed else None,
                            "signal_ts": pd.Timestamp(ts[x]), "entry_ts": pd.Timestamp(ts[x + 1]),
                            "exit_ts": pd.Timestamp(ts[res["exit_idx"]]) if closed else None,
                            "trade_date": g["days"][g["day_id"][x + 1]].isoformat(),
                            "entry_price": res["entry"], "exit_price": mark, "stop_price": res["stop"],
                            "target_price": res["target"], "stop_dist": dist,
                            "status": res["status"], "reason": res.get("reason"), "bars_held": res["held"],
                            "points": m.side * (mark - res["entry"]),
                        })
                        busy_until = res["exit_idx"] if closed else None
                        row["outcome"] = "member_trade"
            signals.append(row)
        members[m.cid] = trades

    combo = list(members[MEMBERS[0].cid])
    blocked: dict[tuple[int, int], int] = {}
    for m in MEMBERS[1:]:
        combo, skipped = first_in(combo, members[m.cid])
        for t in skipped:
            blocked[(t["cid"], t["signal_idx"])] = t["blocked_by"]
    taken = {(t["cid"], t["signal_idx"]) for t in combo}
    for row in signals:
        if row["outcome"] == "member_trade":
            key = (row["cid"], row["signal_idx"])
            if key in taken:
                row["outcome"] = "taken"
            else:
                row["outcome"] = "position_held"
                row["blocked_by"] = blocked.get(key)
    signals.sort(key=lambda r: (r["signal_idx"], [m.cid for m in MEMBERS].index(r["cid"])))
    return {"grid": g, "trades": combo, "members": members, "signals": signals}


# -------------------------------------------------------------- contracts ---
def stitch(frames: list[tuple[str, date, pd.DataFrame]], today: date) -> tuple[pd.DataFrame, list[dict]]:
    """Join contracts ``(symbol, expiry, minute frame)`` into one front-month series, as the engine's
    ``continuous.stitch`` with its defaults: a contract's last session is the one before its expiry
    day, and at each roll the gap (next contract's close minus this one's, on this contract's last
    bar) is added to all earlier prices. Returns the series and one row per contract used."""
    cols = ["ts", "open", "high", "low", "close", "volume"]
    frames = sorted([f for f in frames if len(f[2])], key=lambda f: f[1])
    segs, rows, start_after = [], [], None
    for i, (sym, exp, df) in enumerate(frames):
        own = set(df["ts"].dt.date)
        days = np.array(sorted(own))
        if start_after is not None:
            days = days[days > start_after]
        days = days[days <= exp]
        if not len(days):
            continue
        rolls = i + 1 < len(frames) and exp <= today
        if rolls:
            nxt = set(frames[i + 1][2]["ts"].dt.date)
            cal = sorted(d for d in own | nxt if d <= exp)
            if cal[-1] != exp and exp < today:
                cal.append(exp)                       # expiry is a session even if no cache holds it
            if len(cal) <= 1:
                continue
            cut = cal[-2] if cal[-1] == exp else cal[-1]
            days = days[days <= cut]
            if not len(days):
                continue
        segs.append((sym, df[df["ts"].dt.date.isin(set(days))]))
        rows.append({"contract": sym, "expiry": exp.isoformat(), "first": days[0].isoformat(),
                     "last": days[-1].isoformat()})
        start_after = days[-1]
        if not rolls:
            break
    if not segs:
        return pd.DataFrame(columns=cols), []
    by_sym = {s: d for s, _, d in frames}
    gaps = [0.0] * len(rows)
    for j in range(len(rows) - 1):
        cur, nxt = segs[j][1], by_sym[rows[j + 1]["contract"]]
        t_last = cur["ts"].iloc[-1]
        same = nxt[(nxt["ts"].dt.date == t_last.date()) & (nxt["ts"] <= t_last)]
        if len(same):
            gaps[j] = float(same["close"].iloc[-1] - cur["close"].iloc[-1])
        else:
            after = nxt[nxt["ts"] > t_last]
            gaps[j] = float(after["open"].iloc[0] - cur["close"].iloc[-1]) if len(after) else 0.0
        rows[j]["roll_gap"] = round(gaps[j], 6)
    out = []
    for j, (_, seg) in enumerate(segs):
        adj = float(sum(gaps[j:]))
        rows[j]["adjustment"] = round(adj, 6)
        s = seg[[c for c in cols if c in seg.columns]].copy()
        if adj:
            s[["open", "high", "low", "close"]] = s[["open", "high", "low", "close"]] + adj
        out.append(s)
    return pd.concat(out, ignore_index=True), rows


# ------------------------------------------------------------------- live ---
def pending_fires(frame: pd.DataFrame, start: date | str, cutoff: datetime) -> list[dict]:
    """What a live runner needs at a minute boundary: the members that signal on the bar that has
    just closed (the minute before ``cutoff``) and would enter at the open of ``cutoff``.

    Judged only from bars complete before ``cutoff``. A member already in its own (replayed) trade,
    inside its cooldown or with a gate off is not returned. The list is in priority order: with no
    position held, the first one takes the trade. The stop distance of a percentage stop depends
    on the fill, so only the ATR stop carries a distance here.
    """
    cutoff = pd.Timestamp(cutoff).floor("min")
    f = frame[frame["ts"] < cutoff].sort_values("ts", kind="stable")
    minute = cutoff.hour * 60 + cutoff.minute
    if (f.empty or f["ts"].iloc[-1] != cutoff - pd.Timedelta(minutes=1) or cutoff.weekday() >= 5
            or not (SESSION_OPEN_MIN < minute <= SESSION_CLOSE_MIN)):
        return []
    px = float(f["close"].iloc[-1])
    # a stand-in for the bar about to open: it only makes the signal bar "have a next bar"
    probe = pd.concat([f, pd.DataFrame([{"ts": cutoff, "open": px, "high": px, "low": px, "close": px,
                                         "volume": 0.0}])], ignore_index=True)
    out = replay(probe, start, (cutoff + pd.Timedelta(minutes=1)).to_pydatetime())
    signal_ts = cutoff - pd.Timedelta(minutes=1)
    fires = []
    for m in MEMBERS:
        for t in out["members"][m.cid]:
            if t["signal_ts"] == signal_ts:
                fires.append({"cid": m.cid, "side": m.side, "stop_kind": m.stop_kind, "stop_value": m.stop_value,
                              "target_r": m.target_r, "signal_ts": signal_ts,
                              "atr_stop_dist": t["stop_dist"] if m.stop_kind == "atr" else None})
    return fires


def levels(member: Member, entry_price: float, atr_stop_dist: float | None = None) -> tuple[float, float, float]:
    """(stop distance, stop price, target price) for a fill at ``entry_price``."""
    dist = entry_price * member.stop_value / 100.0 if member.stop_kind == "pct" else float(atr_stop_dist)
    return dist, entry_price - member.side * dist, entry_price + member.side * member.target_r * dist
