"""Build the frozen seed for the labs "NIFTY Tuesday put" paper book (read-only on every source).

- spot: the NIFTY index 09:20 minute open, from the same minute bars the research used
  (Strategy Tester RunData nifty50), for every Tuesday 2025-09-02 .. 2026-09-01;
- quotes: for every one of those Tuesdays, the nearest-expiry put rows at 09:20-09:22 and
  15:25-15:27 within 1,500 points of spot - from the shared store's 1-minute files (with bid and
  ask) where this PC has them, else from the alphaIMB 5-minute snapshot files
  (data/live/nifty_oi_DATE.csv, never modified; last traded price only). PythonAnywhere holds no
  5-minute files and its shared-store folders for 2026-04-28 to 2026-05-26 are empty.

Runs on the research PC only (it needs the Strategy Tester and the local alphaIMB files):

    python scripts/build_nifty_tuesday_put_seed.py data

The two CSVs it writes are uploaded to labs_project/data/ on PythonAnywhere.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ST = Path(r"C:\Users\vipin\Strategy Tester")
sys.path.insert(0, str(ST))
sys.path.insert(0, r"C:\Users\vipin\labs_project")
import os  # noqa: E402

os.chdir(ST)
from strategy_tester_v2.prepare import RunData  # noqa: E402
from market_data.expiry import select_expiry_code  # noqa: E402
from market_data.shared_store import load_options_frame  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
START, END = "2025-09-02", "2026-09-01"
tuesdays = [d.strftime("%Y-%m-%d") for d in pd.date_range(START, END, freq="W-TUE")]

rd = RunData("nifty50", "2025-06-01", "2026-09-18", families=[], outcomes=False).prepare()
b = rd.bars
tod = b.tod.astype(np.int64)
dates = pd.to_datetime(b.dates.astype("datetime64[D]")).strftime("%Y-%m-%d")
spot = []
for d in range(b.n_days):
    if dates[d] not in tuesdays:
        continue
    ix = np.flatnonzero((b.day_id == d) & b.valid)
    a = ix[tod[ix] == 9 * 60 + 20]
    if len(a):
        spot.append({"trade_date": dates[d], "spot_0920_open": float(b.open[a[0]]), "source": "Strategy Tester nifty50 minute bars"})
spot = pd.DataFrame(spot)
spot.to_csv(out / "nifty_tuesday_put_spot_seed.csv", index=False)
print("spot rows", len(spot), "of", len(tuesdays), "Tuesdays")

shared = [Path(r"C:\Users\vipin\shared_market_data\archive"), Path(r"C:\Users\vipin\shared_market_data\live")]
legacy = Path(r"C:\Users\vipin\alphaIMB\data\live")
rows = []
MINUTES = ["09:20", "09:21", "09:22", "15:25", "15:26", "15:27"]
for day in spot.trade_date:
    s0 = float(spot.loc[spot.trade_date == day, "spot_0920_open"].iloc[0])
    if any((r / day).is_dir() and any((r / day).glob("NIFTY_options_1min.*")) for r in shared):
        fr = load_options_frame("NIFTY", day, live_root=shared[1], archive_root=shared[0])
        name = fr.attrs.get("source_path") or fr.attrs.get("path") or "shared store"
        ts = pd.to_datetime(fr["timestamp"], errors="coerce")
        if ts.dt.tz is not None:
            ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        fr["minute"] = ts.dt.floor("min")
        fr["expiry"] = fr["expiry"].astype(str).str.upper()
        code = select_expiry_code(fr["expiry"].unique(), day, "nearest_weekly")
        strike = pd.to_numeric(fr["strike"], errors="coerce")
        keep = (fr["expiry"] == code) & (fr["option_type"].astype(str).str.upper().str[0] == "P")             & fr["minute"].dt.strftime("%H:%M").isin(MINUTES) & ((strike - s0).abs() <= 1500)
        for r in fr[keep].itertuples(index=False):
            rows.append({"trade_date": day, "timestamp": r.minute.strftime("%Y-%m-%d %H:%M:00"), "tradingsymbol": r.tradingsymbol,
                         "expiry": code, "strike": int(float(r.strike)), "option_type": "PE",
                         "ltp": float(r.ltp) if pd.notna(r.ltp) else None, "bid": float(r.bid) if pd.notna(r.bid) else None,
                         "ask": float(r.ask) if pd.notna(r.ask) else None, "kind": "1min", "source_file": str(Path(str(name)).name)})
        continue
    f = legacy / f"nifty_oi_{day}.csv"
    if not f.exists():
        print("no quotes anywhere:", day)
        continue
    df = pd.read_csv(f, low_memory=False)
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df["minute"] = ts.dt.floor("min")
    sym = df["tradingsymbol"].astype(str).str.upper()
    df["expiry"] = sym.str.extract(r"^NIFTY(\w{5})\d+(?:CE|PE)$")[0]
    df = df.dropna(subset=["expiry", "minute"])
    code = select_expiry_code(df["expiry"].unique(), day, "nearest_weekly")
    hm = df["minute"].dt.strftime("%H:%M")
    keep = (df["expiry"] == code) & sym.str.endswith("PE") & hm.isin(MINUTES) \
        & ((pd.to_numeric(df["strike"], errors="coerce") - s0).abs() <= 1500)
    g = df[keep]
    for r in g.itertuples(index=False):
        rows.append({"trade_date": day, "timestamp": r.minute.strftime("%Y-%m-%d %H:%M:00"), "tradingsymbol": r.tradingsymbol,
                     "expiry": code, "strike": int(float(r.strike)), "option_type": "PE",
                     "ltp": float(r.price) if pd.notna(r.price) else None, "bid": None, "ask": None,
                     "kind": "5min", "source_file": f.name})
q = pd.DataFrame(rows)
q.to_csv(out / "nifty_tuesday_put_quote_seed.csv", index=False)
print("quote rows", len(q), "days", q.trade_date.nunique() if len(q) else 0)
