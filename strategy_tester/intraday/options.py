"""Intraday rules traded in real index options, priced from the labs one-minute option archive.

Data: the labs shared market store (`shared_market_data/archive/<day>/<SYM>_options_1min.parquet.zst`, or the live
CSV for recent days): one quote snapshot per strike per minute with bid and ask, taken a few seconds after the
minute starts (the chain's spot matches that minute's index OPEN: median gap 0.8 NIFTY points). Each session is
reduced once to a compact file under labs `data/strategy_tester/options/<SYM>/<day>.npz`: the two nearest expiries,
CE and PE, 25 strikes either side of the opening at-the-money strike, bid and ask per minute (0.05 ticks; 0 = no
quote).

Execution (no look-ahead):
  * the signal comes from the index rule (decided on a completed minute, entered at the next minute's open);
  * the contract is chosen on that minute's quotes: ATM / N strikes OTM or ITM / nearest a delta (Black-Scholes on
    the mid, r = 0) / nearest a premium; nearest expiry unless the rule says the next one or to roll on expiry day;
  * buying fills at the ask, selling at the bid of that minute's quote;
  * index exits (stop / target / trail in index points) touched inside a minute fill at the NEXT minute's quote,
    the first quote after the touch; time exits and bars that open through a level use that minute's quote;
  * premium exits (stop / target / trail on the option price) are checked on each minute's quote and fill at it;
  * no quote at the entry minute = no trade (counted); a missing exit quote takes the next one (up to 5 minutes);
  * charges: the labs schedule - brokerage Rs 20 an order, STT 0.15% of the sell premium (rate from 2026-04-01),
    exchange charges (NSE 0.03503%, BSE 0.0325%), SEBI, stamp 0.003% on the buy, GST 18%.
Nothing here places orders.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import engine as E
from . import store
from .rules import Rule

log = logging.getLogger("strategy-tester.options")
IST = timezone(timedelta(hours=5, minutes=30))

OPT_ROOT = Path(os.environ.get("ST_OPTION_DIR") or store.ROOT.parent / "options")
WINDOW = 25                     # strikes either side of the opening at-the-money strike
NS = 2 * WINDOW + 1
SCALE = 20.0                    # stored price units: 0.05 rupee
MIN_MINUTES = 356               # a session needs quotes on 95% of its 375 minutes to be used
EXPIRY_WEEKDAY = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 3}      # monthly expiry: last Tuesday / Thursday
EXCH_TXN = {"NIFTY": 0.0003503, "BANKNIFTY": 0.0003503, "SENSEX": 0.000325}
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                      "DEC"], 1)}
WEEK_MONTH = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}
COLUMNS = ["timestamp", "strike", "option_type", "expiry", "bid", "ask", "spot"]

_ensure_lock = threading.Lock()
_last_ensure: dict[str, float] = {}
_books: dict[str, tuple[float, "Book"]] = {}


class OptionsUnavailable(RuntimeError):
    pass


# ------------------------------------------------------------------ charges
def charges(symbol: str, buy_value, sell_value):
    """Round-trip charges in rupees for one option trade (works on arrays). buy/sell values = premium x quantity."""
    turnover = buy_value + sell_value
    brokerage = 40.0
    txn = EXCH_TXN[symbol] * turnover
    sebi = 1e-6 * turnover
    return brokerage + 0.0015 * sell_value + txn + sebi + 0.00003 * buy_value + 0.18 * (brokerage + txn + sebi)


# ------------------------------------------------------------------ expiries
def expiry_date(code, symbol: str, trading_days: set | None = None) -> date | None:
    """Exchange expiry code -> date. Weekly codes carry the date (26D01 = 2026-12-01, 26602 = 2026-06-02); monthly codes
    (26NOV) expire on the last Tuesday (NSE) / Thursday (SENSEX) of the month, a session earlier on a holiday."""
    c = str(code).strip().upper()
    if c.endswith(".0"):
        c = c[:-2]
    try:
        y = 2000 + int(c[:2])
        if c[2:] in MONTHS:
            m = MONTHS[c[2:]]
            d = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
            while d.weekday() != EXPIRY_WEEKDAY[symbol]:
                d -= timedelta(days=1)
            if trading_days and d <= max(trading_days):
                while d not in trading_days and d > min(trading_days):
                    d -= timedelta(days=1)
            return d
        if len(c) == 5 and c[2] in WEEK_MONTH:
            return date(y, WEEK_MONTH[c[2]], int(c[3:]))
    except (ValueError, KeyError):
        return None
    return None


# ------------------------------------------------------------------ store
def _path(symbol: str, day: str) -> Path:
    return OPT_ROOT / symbol / f"{day}.npz"


def _roots():
    from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR     # labs shared market store
    return Path(SHARED_LIVE_DIR), Path(SHARED_ARCHIVE_DIR)


def raw_session(symbol: str, day: str) -> pd.DataFrame:
    from market_data.shared_store import load_options_frame
    live, arch = _roots()
    return load_options_frame(symbol, day, live_root=live, archive_root=arch, columns=COLUMNS)


def source_days(symbol: str) -> list[str]:
    """Sessions in the shared store (archive + live), completed ones only."""
    live, arch = _roots()
    stem = f"{symbol}_options_1min"
    days = set()
    if arch.is_dir():
        for p in arch.iterdir():
            if p.is_dir() and any((p / f"{stem}{s}").is_file() for s in (".parquet.zst", ".parquet.gz", ".parquet")):
                days.add(p.name)
    if live.is_dir():
        for p in live.iterdir():
            if p.is_dir() and (p / f"{stem}.csv").is_file():
                days.add(p.name)
    now = datetime.now(IST)                              # PA's clock is UTC
    today = now.date().isoformat()
    done_today = now.time() >= time(15, 35)
    return sorted(d for d in days if len(d) == 10 and (d < today or (d == today and done_today)))


def build_day(symbol: str, day: str, frame: pd.DataFrame, trading_days: set | None = None) -> dict | None:
    """Reduce one session's option chain to the compact file; returns a summary (None when unusable)."""
    f = frame
    ts = pd.to_datetime(f["timestamp"])
    k = ((ts.dt.hour * 60 + ts.dt.minute) - (9 * 60 + 15)).to_numpy()
    keep = (k >= 0) & (k < E.MINUTES)
    f, k = f.loc[keep], k[keep]
    if not len(f):
        return None
    dday = date.fromisoformat(day)
    codes = f["expiry"].astype(str).str.upper().str.replace(r"\.0$", "", regex=True).to_numpy()
    exps = {}
    for c in pd.unique(codes):
        d = expiry_date(c, symbol, trading_days)
        if d and d >= dday:
            exps[c] = d
    order = sorted(exps, key=exps.get)[:2]
    if not order:
        return None
    step = store.INDICES[symbol]["step"]
    sp = pd.Series(f["spot"].to_numpy(float)).groupby(k).median()
    sp = sp[sp > 0]
    if not len(sp):
        return None
    spot = np.full(E.MINUTES, np.nan, np.float32)
    spot[sp.index.to_numpy()] = sp.to_numpy()
    atm = int(round(float(sp.iloc[0]) / step) * step)
    strikes = atm + step * np.arange(-WINDOW, WINDOW + 1)
    bid = np.zeros((2, 2, NS, E.MINUTES), np.uint16)
    ask = np.zeros_like(bid)
    si = (f["strike"].to_numpy(float) - strikes[0]) / step
    ok = (si == np.round(si)) & (si >= 0) & (si < NS)
    ti = np.where(f["option_type"].astype(str).str.upper().to_numpy() == "CE", 0, 1)
    b, a = f["bid"].to_numpy(float), f["ask"].to_numpy(float)
    valid = (b > 0) & (a > 0) & (a >= b) & (a * SCALE < 65535)
    for ei, c in enumerate(order):
        m = ok & (codes == c)
        idx = (ei, ti[m], si[m].astype(int), k[m])
        bid[idx] = np.where(valid[m], np.round(b[m] * SCALE), 0).astype(np.uint16)
        ask[idx] = np.where(valid[m], np.round(a[m] * SCALE), 0).astype(np.uint16)
    quoted = int((ask[0] > 0).any(axis=(0, 1)).sum())
    expiries = [exps[c].isoformat() for c in order] + [""] * (2 - len(order))
    out = _path(symbol, day)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.npz")
    np.savez_compressed(tmp, strikes=strikes.astype(np.int32), expiries=np.array(expiries), bid=bid, ask=ask,
                        spot=spot, quoted=np.int32(quoted))
    os.replace(tmp, out)
    return {"day": day, "expiries": expiries, "atm": atm, "quoted_minutes": quoted}


def _trading_days(symbol: str) -> set:
    try:
        idx = store.load(symbol, auto_refresh=False).index
        return set(pd.DatetimeIndex(idx.normalize().unique()).date)
    except Exception:
        return set()


def ensure(symbol: str, max_new: int | None = 5, min_gap_s: float = 1800, force: bool = False) -> dict:
    """Build compact files for sessions not yet converted. Web requests convert at most `max_new` sessions (the
    backfill job converts everything); at most once per `min_gap_s` per symbol; failures are logged, never raised."""
    with _ensure_lock:
        if not force and _time.time() - _last_ensure.get(symbol, 0) < min_gap_s:
            return {"built": 0}
        _last_ensure[symbol] = _time.time()
    try:
        todo = [d for d in source_days(symbol) if force or not _path(symbol, d).is_file()]
    except Exception as exc:
        log.warning("option store listing %s failed: %s", symbol, exc)
        return {"built": 0, "error": str(exc)}
    if max_new is not None:
        todo = todo[-max_new:]
    tdays = _trading_days(symbol) if todo else set()
    built, failed = 0, []
    for d in todo:
        try:
            if build_day(symbol, d, raw_session(symbol, d), tdays):
                built += 1
        except Exception as exc:
            failed.append(d)
            log.warning("option store %s %s failed: %s", symbol, d, exc)
    if built:
        _books.pop(symbol, None)
    return {"built": built, "failed": failed, "pending": len(todo) - built}


class Book:
    """All stored sessions of one index as stacked arrays: bid / ask [day, expiry, CE|PE, strike, minute] in 0.05s."""

    def __init__(self, symbol: str, files: list[Path]):
        self.symbol = symbol
        self.step = store.INDICES[symbol]["step"]
        days, strikes0, exps, quoted, bids, asks, spots = [], [], [], [], [], [], []
        for p in files:
            try:
                with np.load(p) as z:
                    bids.append(z["bid"]); asks.append(z["ask"])
                    strikes0.append(int(z["strikes"][0])); spots.append(z["spot"])
                    exps.append([date.fromisoformat(x) if x else None for x in z["expiries"]])
                    quoted.append(int(z["quoted"]))
                    days.append(date.fromisoformat(p.stem))
            except Exception as exc:                      # a damaged file is skipped, not fatal
                log.warning("option file %s unreadable: %s", p, exc)
        self.days = days
        self.pos = {d: i for i, d in enumerate(days)}
        self.strike0 = np.array(strikes0, float)
        self.expiries = exps
        self.quoted = np.array(quoted, int)
        self.bid = np.stack(bids) if bids else np.zeros((0, 2, 2, NS, E.MINUTES), np.uint16)
        self.ask = np.stack(asks) if asks else np.zeros_like(self.bid)
        self.spot = np.stack(spots) if spots else np.zeros((0, E.MINUTES), np.float32)

    def usable(self, i: int) -> bool:
        return self.quoted[i] >= MIN_MINUTES

    def path(self, kind: str, i: int, ei: int, ti: int, si: int) -> np.ndarray:
        arr = (self.bid if kind == "bid" else self.ask)[i, ei, ti, si].astype(float) / SCALE
        arr[arr <= 0] = np.nan
        return arr


def load(symbol: str, refresh: bool = True) -> Book:
    if symbol not in store.INDICES:
        raise OptionsUnavailable(f"Options are priced for {', '.join(store.INDICES)} only.")
    if refresh and os.environ.get("ST_OPTION_REFRESH", "1") == "1":
        ensure(symbol)
    hit = _books.get(symbol)
    if hit and _time.time() - hit[0] < 900:
        return hit[1]
    files = sorted((OPT_ROOT / symbol).glob("????-??-??.npz"))
    if not files:
        raise OptionsUnavailable(f"No {symbol} option quotes stored yet (the option backfill has not run).")
    book = Book(symbol, files)
    _books[symbol] = (_time.time(), book)
    return book


# ------------------------------------------------------------------ Black-Scholes (r = 0) for delta strikes
def _ncdf(x):
    """Standard normal CDF (Abramowitz-Stegun 7.1.26 erf, |error| < 1.5e-7), vectorised."""
    z = np.abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-z * z)
    return 0.5 * (1.0 + np.sign(x) * y)


def bs_price(S, K, T, v, call):
    sd = v * np.sqrt(T)
    d1 = (np.log(S / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    c = S * _ncdf(d1) - K * _ncdf(d2)
    return np.where(call, c, c - S + K)


def bs_delta(S, K, T, v, call):
    sd = v * np.sqrt(T)
    d1 = (np.log(S / K) + 0.5 * sd * sd) / sd
    return np.where(call, _ncdf(d1), _ncdf(d1) - 1.0)


def implied_vol(price, S, K, T, call, lo=0.01, hi=4.0, iters=60):
    lo_a, hi_a = np.full_like(price, lo, dtype=float), np.full_like(price, hi, dtype=float)
    for _ in range(iters):
        mid = 0.5 * (lo_a + hi_a)
        high = bs_price(S, K, T, mid, call) > price
        hi_a = np.where(high, mid, hi_a)
        lo_a = np.where(high, lo_a, mid)
    return 0.5 * (lo_a + hi_a)


def years_to_expiry(day: date, k: int, expiry: date) -> float:
    now = datetime.combine(day, time(9, 15)) + timedelta(minutes=int(k))
    secs = (datetime.combine(expiry, time(15, 30)) - now).total_seconds()
    return max(secs, 60.0) / (365.0 * 86400.0)


# ------------------------------------------------------------------ contract choice
def expiry_index(book: Book, i: int, opt: dict) -> int:
    day = book.days[i]
    ei = 1 if opt.get("expiry") == "next" else 0
    if ei == 0 and opt.get("roll_on_expiry") and book.expiries[i][0] == day:
        ei = 1
    return ei if book.expiries[i][ei] is not None else -1


def choose(book: Book, i: int, opt: dict, ti: int, k: int, S: float, sell: bool) -> tuple[int, int] | None:
    """(expiry index, strike index) for an entry at minute k, or None when there is no tradeable quote."""
    ei = expiry_index(book, i, opt)
    if ei < 0:
        return None
    step = book.step
    atm = int(round((S - book.strike0[i]) / step))
    kind, val = opt.get("strike", "atm"), opt.get("value")
    bid = book.bid[i, ei, ti, :, k].astype(float) / SCALE
    ask = book.ask[i, ei, ti, :, k].astype(float) / SCALE
    quoted = (bid > 0) & (ask > 0)
    if kind == "atm":
        si = atm
    elif kind in ("otm", "itm"):
        n = int(val or 1)
        si = atm + (n if (ti == 0) == (kind == "otm") else -n)
    elif kind in ("delta", "premium"):
        cand = np.flatnonzero(quoted)
        if not len(cand):
            return None
        if kind == "premium":
            px = (bid if sell else ask)[cand]
            si = int(cand[np.argmin(np.abs(px - float(val)))])
        else:
            K = book.strike0[i] + step * cand
            T = years_to_expiry(book.days[i], k, book.expiries[i][ei])
            mid = (bid[cand] + ask[cand]) / 2
            call = ti == 0
            intrinsic = np.maximum(S - K, 0) if call else np.maximum(K - S, 0)
            iv = implied_vol(np.maximum(mid, intrinsic + 0.01), S, K, T, call)
            dl = np.abs(bs_delta(S, K, T, iv, call))
            si = int(cand[np.argmin(np.abs(dl - float(val)))])
    else:
        raise ValueError(f"Unknown strike rule '{kind}'")
    if not (0 <= si < NS) or not quoted[si]:
        return None
    return ei, si


def contract_name(book: Book, i: int, ei: int, ti: int, si: int) -> str:
    k = int(book.strike0[i] + book.step * si)
    return f"{k} {'CE' if ti == 0 else 'PE'} {book.expiries[i][ei]:%d %b}"


# ------------------------------------------------------------------ simulation
def usable_days(mk: E.Market, book: Book, start: str | None, end: str | None) -> np.ndarray:
    days = mk.slice_days(start, end)
    keep = [d for d in days if (i := book.pos.get(mk.days[d].date())) is not None and book.usable(i)]
    return np.array(keep, int)


def _close_price(path: np.ndarray, m: int) -> tuple[float, int, bool]:
    """Quote to close at minute m; a missing quote takes the next one within 5 minutes, else the last one before."""
    for q in range(m, min(m + 6, E.MINUTES)):
        if np.isfinite(path[q]):
            return float(path[q]), q, q != m
    before = np.flatnonzero(np.isfinite(path[:m]))
    return (float(path[before[-1]]), int(before[-1]), True) if len(before) else (float("nan"), m, True)


def simulate(mk: E.Market, rule: Rule, book: Book, day_idx: np.ndarray, ev: np.ndarray | None = None,
             ov: dict | None = None) -> tuple[list[dict], np.ndarray, dict]:
    """Trades and per-day net rupee P&L. `ov` overrides exits for the sensitivity grid: stop / target / trail (index
    points) and pstop / ptarget / ptrail (premium, in the rule's unit)."""
    ev = E.events(mk, rule) if ev is None else ev
    ov = ov or {}
    opt = rule.option or {}
    sell = opt.get("mode") == "sell"
    sp = ov.get("stop", rule.stop_pts)
    tp = ov.get("target", rule.target_pts)
    tr = ov.get("trail", rule.trail_pts)
    def limit(name):
        """Premium exit distance as a function of the entry premium (stop_pct = % of it, stop_pts = rupees)."""
        v_pct, v_pts = ov.get(name + "_pct", opt.get(name + "_pct")), ov.get(name + "_pts", opt.get(name + "_pts"))
        if v_pct:
            return lambda e: e * float(v_pct) / 100.0
        return (lambda e: float(v_pts)) if v_pts else None

    pstop, ptgt, ptrail = limit("stop"), limit("target"), limit("trail")
    k_exit = E.exit_minute(rule)
    lot = store.INDICES[rule.symbol]["lot"]
    qty = lot * max(rule.lots, 1)
    eday = opt.get("expiry_day")
    trades: list[dict] = []
    pnl = np.zeros(len(day_idx))
    stats = {"unpriced": 0, "late_exit_quotes": 0}
    for j, d in enumerate(day_idx):
        i = book.pos[mk.days[d].date()]
        is_exp = book.expiries[i][0] == book.days[i]
        if (eday == "only" and not is_exp) or (eday == "skip" and is_exp):
            continue
        row = ev[d]
        cand = np.flatnonzero(row[:k_exit])
        k_from, n_tr, n_loss, day_rs = 0, 0, 0, 0.0
        for k in cand:
            if k < k_from or n_tr >= rule.max_trades:
                continue
            if E.day_stopped(rule, day_rs / qty, day_rs, n_loss):
                break
            sign = int(np.sign(row[k]))
            if not E.day_filter(mk, rule, sign, d, k):
                continue
            ti = 0 if (sign > 0) != sell else 1          # buy: CE up / PE down; sell: PE up / CE down
            pick = choose(book, i, opt, ti, int(k), float(mk.o[d, k]), sell)
            if pick is None:
                stats["unpriced"] += 1
                continue
            ei, si = pick
            bid, ask = book.path("bid", i, ei, ti, si), book.path("ask", i, ei, ti, si)
            entry = float(bid[k] if sell else ask[k])
            close = ask if sell else bid                 # the price the position is closed at
            k_out, idx_px, why, at_open = E.index_exit(mk, rule, d, int(k), sign, sp, tp, tr, k_exit)
            q = k_out if at_open else min(k_out + 1, E.MINUTES - 1)
            exit_k, reason = q, why
            if pstop or ptgt or ptrail:
                seg = close[k + 1:q]
                pl = (entry - seg) if sell else (seg - entry)
                hit = np.zeros(len(seg), bool)
                why_seg = np.array([""] * len(seg), dtype=object)
                if ptrail:
                    best_prev = np.r_[0.0, np.fmax.accumulate(np.nan_to_num(pl, nan=-np.inf))[:-1]]
                    best_prev = np.maximum(best_prev, 0.0)
                    m_tr = pl <= best_prev - ptrail(entry)
                    why_seg[m_tr & ~hit] = "Premium trail"; hit |= m_tr
                if pstop:
                    m_s = pl <= -pstop(entry)
                    why_seg[m_s] = "Premium stop"; hit |= m_s
                if ptgt:
                    m_t = pl >= ptgt(entry)
                    why_seg[m_t & ~hit] = "Premium target"; hit |= m_t
                if hit.any():
                    first = int(np.argmax(hit))
                    exit_k, reason = k + 1 + first, why_seg[first]
            px, q_used, late = _close_price(close, exit_k)
            if not np.isfinite(px):
                stats["unpriced"] += 1
                continue
            stats["late_exit_quotes"] += late
            buy_px, sell_px = (px, entry) if sell else (entry, px)
            gross = (sell_px - buy_px) * qty
            cost = float(charges(rule.symbol, buy_px * qty, sell_px * qty))
            net = gross - cost
            day_rs += net
            n_tr += 1
            n_loss += net < 0
            k_from = (exit_k if exit_k < q else k_out) + 1
            atm = int(round((mk.o[d, k] - book.strike0[i]) / book.step))
            trades.append({"day": int(d), "k_in": int(k), "k_out": int(q_used), "side": "Long" if sign > 0 else "Short",
                           "contract": ("Sell " if sell else "Buy ") + contract_name(book, i, ei, ti, si),
                           "ei": ei, "ti": ti, "offset": si - atm, "entry_price": entry, "exit_price": px,
                           "index_in": float(mk.o[d, k]), "index_out": float(mk.o[d, min(q_used, E.MINUTES - 1)]),
                           "spread_in": float(ask[k] - bid[k]), "gross_rs": gross, "charges": cost, "net_rs": net,
                           "premium_pts": (sell_px - buy_px), "exit_reason": reason, "qty": qty})
        pnl[j] = day_rs
    return trades, pnl, stats


def random_entries(mk: E.Market, rule: Rule, book: Book, trades: list[dict], days: np.ndarray, n: int = 400,
                   seed: int = 7) -> dict | None:
    """Each trade re-taken on random sessions: same minute in and out, same option side, same distance from ATM,
    same expiry rule, ask in / bid out (no stops). Mean net rupees a trade vs the strategy's own."""
    if len(trades) < 10:
        return None
    opt = rule.option or {}
    sell = opt.get("mode") == "sell"
    rng = np.random.default_rng(seed)
    bi = np.array([book.pos[mk.days[d].date()] for d in days])
    ei_day = np.array([max(expiry_index(book, i, opt), 0) for i in bi])
    total = np.zeros(n)
    count = np.zeros(n)
    for t in trades:
        pick = rng.integers(0, len(days), n)
        dd, ii, ee = days[pick], bi[pick], ei_day[pick]
        si = np.round((mk.o[dd, t["k_in"]] - book.strike0[ii]) / book.step).astype(int) + t["offset"]
        ok = (si >= 0) & (si < NS)
        si = np.clip(si, 0, NS - 1)
        b_in, a_in = book.bid[ii, ee, t["ti"], si, t["k_in"]] / SCALE, book.ask[ii, ee, t["ti"], si, t["k_in"]] / SCALE
        b_out, a_out = book.bid[ii, ee, t["ti"], si, t["k_out"]] / SCALE, book.ask[ii, ee, t["ti"], si, t["k_out"]] / SCALE
        ok &= (b_in > 0) & (a_in > 0) & (b_out > 0) & (a_out > 0)
        buy_px, sell_px = (a_out, b_in) if sell else (a_in, b_out)
        q = t["qty"]
        net = (sell_px - buy_px) * q - charges(rule.symbol, buy_px * q, sell_px * q)
        total += np.where(ok, net, 0.0)
        count += ok
    rand = total / np.maximum(count, 1)
    own = float(np.mean([t["net_rs"] for t in trades]))
    return {"trades": len(trades), "actual_avg_trade_rs": own, "random_median_rs": float(np.median(rand)),
            "random_p05_rs": float(np.percentile(rand, 5)), "random_p95_rs": float(np.percentile(rand, 95)),
            "percentile": float((rand < own).mean() * 100), "p_value": float((rand >= own).mean()), "samples": n,
            # the shared tile shows these as % a trade: here, of the average premium paid
            "actual_avg_trade": own / float(np.mean([t["entry_price"] * t["qty"] for t in trades])) * 100,
            "random_median": float(np.median(rand)) / float(np.mean([t["entry_price"] * t["qty"] for t in trades])) * 100}


def describe(rule: Rule) -> str:
    opt = rule.option or {}
    kind, val = opt.get("strike", "atm"), opt.get("value")
    if kind in ("otm", "itm"):
        n = int(val or 1)
        strike = f"{n} strike{'s' if n > 1 else ''} {kind.upper()}"
    elif kind == "delta":
        strike = f"delta {float(val):g}"
    elif kind == "premium":
        strike = f"premium near Rs {float(val):g}"
    else:
        strike = "ATM"
    exp = "next expiry" if opt.get("expiry") == "next" else (
        "nearest expiry, next on expiry day" if opt.get("roll_on_expiry") else "nearest expiry")
    what = "sell" if opt.get("mode") == "sell" else "buy"
    base = E.describe(rule).replace(" (points)", "")
    ex = []
    for key, label in (("stop", "premium stop"), ("target", "premium target"), ("trail", "premium trail")):
        if opt.get(key + "_pct"):
            ex.append(f"{label} {opt[key + '_pct']:g}%")
        elif opt.get(key + "_pts"):
            ex.append(f"{label} {opt[key + '_pts']:g} pts")
    if opt.get("expiry_day"):
        ex.append("expiry days only" if opt["expiry_day"] == "only" else "no trades on expiry days")
    if rule.max_trades > 1:
        ex.append(f"up to {rule.max_trades} trades a day")
    if rule.max_losses:
        ex.append(f"stop after {rule.max_losses} losses")
    if rule.day_loss_rs:
        ex.append(f"day loss cap Rs {rule.day_loss_rs:,.0f}")
    if rule.day_target_rs:
        ex.append(f"day target Rs {rule.day_target_rs:,.0f}")
    return f"{base} · {what} {strike} {'CE/PE' if rule.side in ('both', 'gap', 'trend') else ('CE' if (rule.side == 'long') != (what == 'sell') else 'PE')}, {exp}" + (
        " · " + ", ".join(ex) if ex else "")
