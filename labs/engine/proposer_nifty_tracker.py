"""Paper book: NIFTY on Sensex V5 signals - the Sensex V5 2.5% book's SENSEX signals traded on NIFTY options
with NIFTY's own exits (Vipin, 2026-10-21: "the one with the best win days rate").

Signal side, unchanged from the SENSEX books (proposer_px_tracker): the same prints (gap regime read at the
close of the 09:15 SENSEX bar, the 5-class on SENSEX bars and chain every 300 s, acted on 25 s into the
minute), the same engine (Proposer v3 entry gate with the 75-point SENSEX with-the-day filter, signal-flip
exit, 270 s wait after an exit, recovery book).

Position side, NIFTY:
- contract: nearest NIFTY weekly; on NIFTY's own expiry day (Tuesday) the next weekly instead - the
  same-day contract is so cheap that a fixed capital buys a huge quantity (28 Jul 2026: -6.47L on one
  floor exit at Rs 12L, -2.36L the day with the next weekly);
- strike: Sentinel's rule scaled to NIFTY - the shallowest ITM strike at ATM -/+50..150 (step 50) with
  |delta| >= 0.60 on the snapshot at or before the entry, else the deepest-delta liquid one;
- size: Rs 12L of premium per entry in whole NIFTY lots;
- exits, in NIFTY points scaled by NIFTY/SENSEX at the day's open (~0.31): spot targets 1.5 x the scaled
  100/40 (~46/19), the 150-point floor scaled (~46), no premium floor; the day target at 1.5% of the
  entry capital; signal flip and 15:25 as the SENSEX books;
- prices: as the SENSEX books - the NIFTY spot path open -> extreme -> extreme -> close of each 1-min bar,
  the option at its recorded snapshot plus a fitted delta times the spot move, half the recorded spread
  each way; NSE option charges.

Chosen on 1 Jul - 16 Oct 2026 from 27 variants as the best green-day rate (alphaIMB REBUILD sec. 41-42):
69 of 72 traded days green, +6.29L, worst day -1.86L, max drawdown 3.53L. In-sample; no NIFTY tick data.
Paper only: it never calls a broker order API.
"""
from __future__ import annotations

import io
import tarfile
from datetime import date, datetime, timedelta

import pandas as pd

from config.labs_config import ARCHIVE_DIR, DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine import proposer_px_tracker as tr
from labs.engine.charges import round_trip_charges
from labs.engine.expiry_strangle_tracker import expiry_date_of
from live.engine import proposer_strike as ps
from market_data.shared_store import load_options_frame

pe = tr.pe
SYMBOL = "NIFTY"
LOT_SIZE = int(UNDERLYINGS[SYMBOL]["lot_size"])
CAPITAL_RS = 1_200_000.0


def nifty_bars(day: str) -> list[tuple]:
    """Collector NIFTY 1-min bars (t, o, h, l, c): live dir first, then the day's archive."""
    live = DATA_DIR / f"{day}_{SYMBOL}_spot_1min.csv"
    if live.is_file():
        return tr._read_spot_csv(live)
    tar = ARCHIVE_DIR / f"{day}.tar.gz"
    if tar.is_file():
        with tarfile.open(tar) as tf:
            m = next((x for x in tf.getmembers() if x.name.endswith(f"_{SYMBOL}_spot_1min.csv")
                      and "BANKNIFTY" not in x.name), None)
            if m is not None:
                return tr._read_spot_csv(io.BytesIO(tf.extractfile(m).read()))
    return []


def nifty_frame(day: str) -> pd.DataFrame:
    try:
        frame = load_options_frame(SYMBOL, day, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
    except FileNotFoundError as exc:
        raise tr.ProposerPxInputError(f"No NIFTY option quotes for {day}") from exc
    frame = frame.copy()
    if "underlying" in frame.columns:
        frame = frame[frame["underlying"].astype(str).str.upper() == SYMBOL]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    if frame["timestamp"].dt.tz is not None:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    frame["timestamp"] = frame["timestamp"].dt.floor("min")
    frame["option_type"] = frame["option_type"].astype(str).str.upper()
    frame["expiry"] = frame["expiry"].astype(str).str.upper()
    for column in ("spot", "strike", "bid", "ask", "ltp"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["timestamp", "spot", "strike"])


def pick_strike(snap: pd.DataFrame, typ: str, spot: float, expiry: date, day: date) -> tuple[int | None, str | None]:
    """(strike, tradingsymbol) from the shared live/paper NIFTY picker."""
    symbol, strike, _, _ = ps.delta_itm_symbol_nifty(
        snap, "CALL" if typ == "CE" else "PUT", spot, day, expiry)
    return strike, symbol


def _params(day: str, book, ratio: float) -> "pe.ProposerParams":
    return pe.scaled_for_nifty(tr.params_on(day, book), ratio)


def simulate_day(day: str, *, book_before: float = 0.0, now: datetime | None = None, book=None) -> dict:
    """Replay one session to its last recorded minute. Pure: reads market data, writes nothing."""
    dday = date.fromisoformat(day)
    if dday.weekday() >= 5:
        raise tr.ProposerPxInputError(f"{day} is not a trading weekday")
    # ---- SENSEX side: the prints, exactly as the SENSEX books ----
    frame = tr._session_frame(day)
    sx_expiry = tr.select_expiry_code(frame["expiry"].unique(), day, "nearest_weekly")
    if sx_expiry is None:
        raise tr.ProposerPxInputError(f"No nearest SENSEX expiry for {day}")
    near = frame[(frame["expiry"] == str(sx_expiry)) & (frame["ltp"] > 0)]
    piv = near.pivot_table(index=["timestamp", "strike"], columns="option_type", values="oi",
                           aggfunc="last").fillna(0)
    chains = {}
    for ts, g in piv.groupby(level=0):
        g = g.droplevel(0)
        zero = 0 * g.iloc[:, 0]
        chains[ts.to_pydatetime()] = (g.index.to_numpy(dtype=float), g.get("CE", zero).to_numpy(),
                                      g.get("PE", zero).to_numpy())
    sx_last = frame["timestamp"].max().to_pydatetime()
    sbars = [b for b in tr.spot_ohlc(day) if tr.SESSION_OPEN <= b[0].time()]
    if not sbars:
        raise tr.ProposerPxInputError(f"No SENSEX spot bars for {day}")

    # ---- NIFTY side ----
    nf = nifty_frame(day)
    nbars = [b for b in nifty_bars(day) if tr.SESSION_OPEN <= b[0].time()]
    if not nbars:
        raise tr.ProposerPxInputError(f"No NIFTY spot bars for {day}")
    codes = {c: expiry_date_of(c, SYMBOL) for c in nf["expiry"].unique()}
    live_codes = {c: d for c, d in codes.items() if d is not None and d >= dday}
    if not live_codes:
        raise tr.ProposerPxInputError(f"No NIFTY expiry for {day}")
    if min(live_codes.values()) == dday:                     # NIFTY's own expiry day: the next weekly
        live_codes = {c: d for c, d in live_codes.items() if d > dday} or live_codes
    code = min(live_codes, key=live_codes.get)
    exp_date = live_codes[code]
    chain = nf[(nf["expiry"] == code) & (nf["ltp"] > 0)]
    snap_spot = {t.to_pydatetime(): float(v) for t, v in nf.groupby("timestamp")["spot"].first().items()}
    cols = ["ltp", "bid", "ask"] + (["tradingsymbol"] if "tradingsymbol" in chain.columns else [])
    quote_book = {(typ, int(k)): g.drop_duplicates("timestamp", keep="last").set_index("timestamp")[cols]
                  for (typ, k), g in chain.groupby(["option_type", "strike"])}

    s_at, n_at = {b[0]: b for b in sbars}, {b[0]: b for b in nbars}
    through = min(sbars[-1][0], nbars[-1][0], sx_last, max(snap_spot))
    if now is not None and now.date().isoformat() == day:
        through = min(through, now.replace(second=0, microsecond=0, tzinfo=None) - timedelta(minutes=1))
    session_end = datetime.combine(dday, tr.EOD)
    final = through >= session_end - timedelta(minutes=1) or (now is None or now.date().isoformat() != day)

    prior = tr._prior_closes(day)
    open_ref = sbars[0][4] if sbars[0][0].time() == tr.SESSION_OPEN else None
    prints, regime = tr._prints(day, prior, [(b[0], b[4]) for b in sbars], open_ref, chains,
                                min(through, session_end), gap_s=book.print_gap_s)
    by_min = {}
    for p in prints:
        by_min.setdefault(p["t"], []).append(p)

    ratio = nbars[0][1] / sbars[0][1]
    eng = pe.ProposerEngine(_params(day, book, ratio))
    pos, trades, realized, snap, shist = None, [], 0.0, {}, []

    def book_state():
        eng.set_book(day_realized=realized, book_net=book_before + realized,
                     day_losses=sum(1 for x in trades if (x.get("gross_rs") or 0) < 0 and x.get("exit_ts")))

    def close(now_, px, spot_, rule):
        nonlocal realized, pos
        qty = pos["qty"]
        gross = (px - pos["ein"]) * qty
        rc = round_trip_charges(pos["ein"], px, qty)
        charges = float(rc.get("raw_total", rc.get("total", 0.0)))
        trades[-1].update({"exit_ts": now_.isoformat(timespec="seconds"), "exit_spot": round(spot_, 2),
                           "exit_price": round(px, 2), "gross_rs": round(gross, 2), "charges_rs": round(charges, 2),
                           "net_rs": round(gross - charges, 2), "exit_rule": rule})
        realized += gross
        pos = None

    t = datetime.combine(dday, tr.SESSION_OPEN)
    while t <= min(through, session_end):
        nb, sb = n_at.get(t), s_at.get(t)
        if nb is None or sb is None:
            if sb is not None:
                shist.append(sb)
            t += timedelta(minutes=1)
            continue
        if t >= session_end:
            if pos:
                q = pos["c"].quote(t)
                close(t, (q[0] - q[1]) if q else pos["last"] - pos["half"], nb[1], "eod")
            break
        pending = by_min.get(t, [])
        anchor_spot, anchor = nb[1], None
        if pos:
            q = pos["c"].quote(t)
            anchor = (q[0], q[1], pos["c"].delta(t)) if q else None
        path, i = tr._dense(tr._nodes(nb)), 0
        move = (shist[-1][4] - open_ref) if shist and open_ref else None     # the SENSEX with-the-day input
        while i < len(path):
            sec, spot = path[i]
            now_ = t + timedelta(seconds=sec)
            fresh = False
            if pending and sec >= pending[-1]["sec"]:
                snap, pending, fresh = pending[-1], [], True
            book_state()
            if pos:
                ltp = None
                if anchor:
                    sgn = 1 if pos["side"] == "CE" else -1
                    ltp = max(0.05, anchor[0] + sgn * anchor[2] * (spot - anchor_spot))
                    pos["last"], pos["half"] = ltp, anchor[1]
                if snap:
                    sig = eng.evaluate(now_, snap, pe.Position(pos["side"], pos["ein"], pos["espot"], pos["qty"]),
                                       option_ltp=ltp, spot=spot)
                    if sig.action == "EXIT":
                        mark = ltp if ltp else pos["last"]
                        if sig.reason == "daily_target":
                            need = max(eng.params.daily_target_pct * pos["ein"] * pos["qty"] - realized,
                                       -(book_before + realized))
                            mark = min(mark, pos["ein"] + need / pos["qty"])
                        close(now_, mark - pos["half"], spot, sig.reason)
                        book_state()
                        eng.evaluate(now_, snap, pe.Position(), option_ltp=None, spot=spot)
            elif fresh and snap:
                sig = eng.evaluate(now_, snap, pe.Position(), option_ltp=None, spot=spot, day_move=move)
                if sig.action == "ENTER" and now_.time() < tr.ENTRY_CUTOFF:
                    typ = "CE" if sig.side == "CALL" else "PE"
                    seen = chain[chain["timestamp"] <= t]
                    snapdf = seen[seen["timestamp"] == seen["timestamp"].max()] if len(seen) else seen
                    strike, sym = pick_strike(snapdf, typ, spot, exp_date, dday)
                    quotes = quote_book.get((typ, strike)) if strike else None
                    c = tr._Contract(quotes, snap_spot) if quotes is not None else None
                    q = c.quote(t) if c else None
                    if q:
                        d = c.delta(t)
                        est = max(0.05, q[0] + (1 if typ == "CE" else -1) * d * (spot - anchor_spot))
                        qty = max(1, int(CAPITAL_RS // ((est + q[1]) * LOT_SIZE))) * LOT_SIZE
                        pos = {"c": c, "side": typ, "ein": est + q[1], "espot": spot, "last": est,
                               "half": q[1], "qty": qty}
                        anchor = (q[0], q[1], d)
                        eng.mark_entry(snap)
                        trades.append({"signal": sig.reason, "side": typ, "strike": strike,
                                       "tradingsymbol": sym or c.symbol(t), "expiry_code": str(code),
                                       "entry_ts": now_.isoformat(timespec="seconds"),
                                       "entry_spot": round(spot, 2), "entry_price": round(est + q[1], 2),
                                       "qty": qty, "exit_ts": None})
                        path = path[:i + 1] + tr._dense(tr._rest_of_minute(sec, spot, nb))[1:]
            i += 1
        shist.append(sb)
        t += timedelta(minutes=1)

    if pos:                                             # still open at the last replayed minute
        q = pos["c"].quote(through + timedelta(minutes=1)) or pos["c"].quote(through)
        mark = (q[0] - q[1]) if q else pos["last"] - pos["half"]
        if final:
            close(through, mark, nbars[-1][4], "eod")
        else:
            trades[-1].update({"exit_price": round(mark, 2), "exit_rule": "open",
                               "gross_rs": round((mark - pos["ein"]) * pos["qty"], 2)})
    closed = [x for x in trades if x.get("exit_ts")]
    prev_close = prior[-1][1] if prior else None
    return {
        "trade_date": day, "status": ("closed" if closed else "no_trade") if final else "live",
        "expiry_code": str(code), "regime": regime.label,
        "gap_pct": round((open_ref / prev_close - 1) * 100, 3) if prev_close and open_ref else None,
        "n_trades": len(trades), "n_losses": sum(1 for x in closed if x["gross_rs"] < 0),
        "day_banked": bool(eng.day_done),
        "gross_rs": round(sum(x["gross_rs"] for x in closed), 2),
        "charges_rs": round(sum(x["charges_rs"] for x in closed), 2),
        "net_rs": round(sum(x["net_rs"] for x in closed), 2),
        "book_gross_before": round(book_before, 2),
        "through_ts": through.isoformat(timespec="minutes"), "trades": trades,
        "prints": [{"minute": p["t"].strftime("%H:%M"), "sec": p["sec"], "x5": p["x5"],
                    "x5_conf": round(float(p["x5_conf"]), 4)} for p in prints],
    }
