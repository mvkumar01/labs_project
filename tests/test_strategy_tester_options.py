"""Strategy Tester option pricing (strategy_tester/intraday/options.py): fills at the quoted ask / bid, the first quote
after an intrabar index stop, premium exits, strike choice, expiry codes, daily limits and the option reader - on a
synthetic Black-Scholes chain, offline."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from strategy_tester.intraday import engine as E, options as O, rules as R

VOL = 0.15
EXPIRY = date(2024, 1, 4)                  # weekly code 24104 (stored as an int in older archive files)


def _day(day: str, path=None, base=20000.0, seed=0):
    rng = np.random.default_rng(seed)
    close = base + np.cumsum(rng.normal(0, 3, E.MINUTES))
    if path is not None:
        close = np.asarray(path, float)
    open_ = np.r_[base, close[:-1]]
    idx = pd.date_range(f"{day} 09:15", periods=E.MINUTES, freq="min")
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) + 1, "Low": np.minimum(open_, close) - 1,
                         "Close": close}, index=idx)


def _chain(mk: E.Market, d: int) -> pd.DataFrame:
    day = mk.days[d].date()
    K = 20000 + 50 * np.arange(-30, 31)
    rows = []
    for k in range(E.MINUTES):
        S = float(mk.o[d, k])
        T = O.years_to_expiry(day, k, EXPIRY)
        for call, typ in ((True, "CE"), (False, "PE")):
            p = np.maximum(np.round(O.bs_price(S, K.astype(float), T, VOL, call) / 0.05) * 0.05, 0.6)
            ts = pd.Timestamp(day) + pd.Timedelta(minutes=555 + k)
            rows.append(pd.DataFrame({"timestamp": ts, "strike": K, "option_type": typ, "expiry": 24104,
                                      "bid": p - 0.5, "ask": p + 0.5, "spot": S}))
    return pd.concat(rows, ignore_index=True)


def _setup(tmp_path, monkeypatch, day2=None):
    monkeypatch.setattr(O, "OPT_ROOT", tmp_path)
    monkeypatch.setattr(E, "COST_BPS", (0.0, 0.0))
    mk = E.Market("NIFTY", pd.concat([_day("2024-01-01", seed=1), day2 if day2 is not None else _day("2024-01-02", seed=2)]))
    info = O.build_day("NIFTY", "2024-01-02", _chain(mk, 1))
    assert info["expiries"][0] == "2024-01-04"
    book = O.Book("NIFTY", sorted(tmp_path.glob("NIFTY/*.npz")))
    return mk, book


def _rule(**kw):
    opt = {"mode": "buy", "strike": "atm", "value": None, "expiry": "nearest"}
    opt.update(kw.pop("opt", {}))
    return R.Rule(symbol="NIFTY", option=opt, **kw)


def _contract(book, mk, k, ti=0, offset=0):
    si = int(round((mk.o[1, k] - book.strike0[0]) / 50)) + offset
    return book.path("bid", 0, 0, ti, si), book.path("ask", 0, 0, ti, si), si


def test_buy_fills_at_ask_and_time_exit_sells_at_that_minute_bid(tmp_path, monkeypatch):
    mk, book = _setup(tmp_path, monkeypatch)
    rule = _rule(side="long", entry={"type": "time", "at": "09:20"}, exit_time="15:00")
    tr, pnl, st = O.simulate(mk, rule, book, np.array([1]))
    t = tr[0]
    bid, ask, si = _contract(book, mk, 5)
    kx = E.k_of("15:00")
    assert t["contract"].startswith("Buy ") and "CE" in t["contract"]
    assert t["entry_price"] == pytest.approx(ask[5]) and t["exit_price"] == pytest.approx(bid[kx]) and t["k_out"] == kx
    gross = (bid[kx] - ask[5]) * 65
    assert t["net_rs"] == pytest.approx(gross - O.charges("NIFTY", ask[5] * 65, bid[kx] * 65))
    assert pnl[0] == pytest.approx(t["net_rs"]) and st["unpriced"] == 0


def test_sell_mode_sells_the_put_at_bid_on_an_up_signal(tmp_path, monkeypatch):
    mk, book = _setup(tmp_path, monkeypatch)
    rule = _rule(side="long", entry={"type": "time", "at": "09:20"}, exit_time="15:00", opt={"mode": "sell"})
    t = O.simulate(mk, rule, book, np.array([1]))[0][0]
    bid, ask, _ = _contract(book, mk, 5, ti=1)
    assert "PE" in t["contract"] and t["contract"].startswith("Sell ")
    assert t["entry_price"] == pytest.approx(bid[5]) and t["exit_price"] == pytest.approx(ask[E.k_of("15:00")])


def test_intrabar_index_stop_exits_on_the_next_minute_quote(tmp_path, monkeypatch):
    d2 = _day("2024-01-02", seed=2)
    k = 6
    d2.iloc[k + 2, d2.columns.get_loc("Low")] = d2["Open"].iloc[k] - 60
    mk, book = _setup(tmp_path, monkeypatch, d2)
    rule = _rule(side="long", entry={"type": "time", "at": "09:21"}, stop_pts=30)
    t = O.simulate(mk, rule, book, np.array([1]))[0][0]
    bid, _, _ = _contract(book, mk, k)
    assert t["exit_reason"] == "Stop loss" and t["k_out"] == k + 3 and t["exit_price"] == pytest.approx(bid[k + 3])


@pytest.mark.parametrize("opt,edit,reason,k_exit", [
    ({"stop_pct": 20}, {4: 0.5}, "Premium stop", 4),
    ({"target_pct": 50}, {6: 2.0}, "Premium target", 6),
    ({"trail_pts": 20}, {3: "+40", 4: "+38", 5: "+15"}, "Premium trail", 5),
])
def test_premium_exits_on_the_minute_quote(tmp_path, monkeypatch, opt, edit, reason, k_exit):
    mk, book = _setup(tmp_path, monkeypatch)
    k = 5
    _, ask, si = _contract(book, mk, k)
    entry = ask[k]
    for m, v in edit.items():
        px = entry * v if isinstance(v, float) else entry + float(v)
        book.bid[0, 0, 0, si, k + m] = round(px * O.SCALE)
        book.ask[0, 0, 0, si, k + m] = round((px + 1) * O.SCALE)
    rule = _rule(side="long", entry={"type": "time", "at": "09:20"}, exit_time="15:00", opt=opt)
    t = O.simulate(mk, rule, book, np.array([1]))[0][0]
    assert t["exit_reason"] == reason and t["k_out"] == k + k_exit
    assert t["exit_price"] == pytest.approx(book.bid[0, 0, 0, si, k + k_exit] / O.SCALE)


def test_otm_and_delta_strikes(tmp_path, monkeypatch):
    mk, book = _setup(tmp_path, monkeypatch)
    k, S = 5, float(mk.o[1, 5])
    atm = int(round((S - book.strike0[0]) / 50))
    assert O.choose(book, 0, {"strike": "otm", "value": 2}, 0, k, S, False) == (0, atm + 2)    # CE: higher strike
    assert O.choose(book, 0, {"strike": "otm", "value": 2}, 1, k, S, False) == (0, atm - 2)    # PE: lower strike
    assert O.choose(book, 0, {"strike": "itm", "value": 1}, 0, k, S, False) == (0, atm - 1)
    ei, si = O.choose(book, 0, {"strike": "delta", "value": 0.3}, 0, k, S, False)
    K = book.strike0[0] + 50 * np.arange(O.NS)
    true = np.abs(O.bs_delta(S, K, O.years_to_expiry(book.days[0], k, EXPIRY), VOL, True))
    assert si == int(np.argmin(np.abs(true - 0.3)))
    ei, si = O.choose(book, 0, {"strike": "premium", "value": 40}, 1, k, S, False)
    assert abs(book.ask[0, 0, 1, si, k] / O.SCALE - 40) == min(abs(a / O.SCALE - 40) for a in book.ask[0, 0, 1, :, k] if a)


def test_expiry_codes():
    assert O.expiry_date("26NOV", "NIFTY") == date(2026, 11, 24)
    assert O.expiry_date("26NOV", "SENSEX") == date(2026, 11, 26)
    assert O.expiry_date(26602, "NIFTY") == date(2026, 6, 2)
    assert O.expiry_date("26D01", "NIFTY") == date(2026, 12, 1)
    tdays = {date(2026, 11, d) for d in (20, 23, 25, 26, 27)}             # 24 Nov a holiday
    assert O.expiry_date("26NOV", "NIFTY", tdays) == date(2026, 11, 23)


def test_daily_limits_stop_new_trades(tmp_path, monkeypatch):
    falling = 20000 - 2.0 * np.arange(1, E.MINUTES + 1)                  # every long loses
    mk, book = _setup(tmp_path, monkeypatch, _day("2024-01-02", path=falling))
    ev = np.zeros_like(mk.c)
    ev[1, [5, 20, 40, 60, 80]] = 1
    base = dict(side="long", entry={"type": "time", "at": "09:20"}, stop_pts=5, max_trades=10)
    assert len(O.simulate(mk, _rule(**base), book, np.array([1]), ev)[0]) == 5
    assert len(O.simulate(mk, _rule(**base, max_losses=2), book, np.array([1]), ev)[0]) == 2
    one = O.simulate(mk, _rule(**base), book, np.array([1]), ev)[0][0]["net_rs"]
    assert len(O.simulate(mk, _rule(**base, day_loss_rs=abs(one) * 1.5), book, np.array([1]), ev)[0]) == 2


def test_random_entries_shape(tmp_path, monkeypatch):
    mk, book = _setup(tmp_path, monkeypatch)
    ev = np.zeros_like(mk.c)
    ev[1, 5:300:20] = 1
    rule = _rule(side="long", entry={"type": "time", "at": "09:20"}, stop_pts=8, target_pts=8, max_trades=50)
    tr = O.simulate(mk, rule, book, np.array([1]), ev)[0]
    assert len(tr) >= 10
    r = O.random_entries(mk, rule, book, tr, np.array([1]), n=50)
    assert 0 <= r["p_value"] <= 1 and r["trades"] == len(tr)


@pytest.mark.parametrize("text,check", [
    ("buy nifty atm call on 15 min opening range breakout, stop 20% target 40%",
     lambda r: r.side == "long" and r.option["mode"] == "buy" and r.option["strike"] == "atm"
     and r.option["stop_pct"] == 20 and r.option["target_pct"] == 40 and r.stop_pts is None),
    ("sell sensex 2 strikes otm puts at 9:30 stop 30 points daily loss rs 5000, stop after 2 losses",
     lambda r: r.symbol == "SENSEX" and r.side == "long" and r.option["mode"] == "sell"
     and (r.option["strike"], r.option["value"]) == ("otm", 2) and r.option["stop_pts"] == 30
     and r.day_loss_rs == 5000 and r.max_losses == 2),
    ("nifty delta 0.6 option both sides on orb stop 40 nifty points",
     lambda r: r.side == "both" and r.option["strike"] == "delta" and r.option["value"] == 0.6 and r.stop_pts == 40),
    ("buy bank nifty put at 9:20 next week expiry, skip expiry day, trail 15 points",
     lambda r: r.side == "short" and r.option["expiry"] == "next" and r.option["expiry_day"] == "skip"
     and r.option["trail_pts"] == 15 and r.trail_pts is None),
])
def test_option_reader(text, check):
    assert R.is_intraday(text)
    assert check(R.parse(text)[0])


def test_index_rules_are_not_read_as_options():
    r = R.parse("buy nifty at 9:20 stop 30 points target 60 points exit at 3:15 pm")[0]
    assert r.option is None and r.stop_pts == 30


def test_request_route_runs_options(tmp_path, monkeypatch):
    from strategy_tester import service
    from strategy_tester.intraday import run as RUN, store
    global EXPIRY
    monkeypatch.setattr(O, "OPT_ROOT", tmp_path)
    days = pd.bdate_range("2024-01-01", "2024-02-09")
    bars = pd.concat([_day(str(d.date()), seed=i) for i, d in enumerate(days)])
    mk = E.Market("NIFTY", bars)
    for d in range(1, len(days)):
        monkeypatch.setattr(__import__(__name__), "EXPIRY", (days[d] + pd.Timedelta(days=7)).date())
        frame = _chain(mk, d)
        frame["expiry"] = (days[d] + pd.Timedelta(days=7)).strftime("%y") + str(((days[d] + pd.Timedelta(days=7)).month)) + (days[d] + pd.Timedelta(days=7)).strftime("%d")
        O.build_day("NIFTY", str(days[d].date()), frame)
    monkeypatch.setattr(store, "load", lambda symbol, *a, **k: bars)
    monkeypatch.setattr(store, "daily_vix", lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr(RUN, "_markets", {})
    monkeypatch.setattr(O, "_books", {})
    monkeypatch.setenv("ST_OPTION_REFRESH", "0")
    r = service.query("buy nifty atm call on 15 min opening range breakout both sides stop 20% target 40% max 3 trades")
    x = r["results"][0]
    assert x["mode"] == "intraday_options" and x["metrics"]["trades"] > 10
    assert all(t["contract"] for t in x["trades"]) and x["validation"]["walk_forward"] is None
    assert x["robustness"]["sensitivity"]["x_param"] == "Premium stop (%)"
    assert x["verdict"]["label"] != "Promising"
