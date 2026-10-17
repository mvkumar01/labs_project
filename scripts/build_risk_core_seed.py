"""Build the frozen research inputs of the Risk core (trend x vol) paper book (run on the research PC).

The V2 Strategy Tester computed its research book from its own caches; PythonAnywhere cannot rebuild the
older history (Binance answers the US server's API calls with HTTP 451 and its archive starts later). The
seed carries exactly those inputs, so the book's research period reproduces the research run, and the
server's feeds (labs/engine/risk_core_data.py) add only the days after each seed ends:

  data/risk_core_seed_bars.csv      sleeve, trade_date, open, high, low, close   (Kite daily NIFTY 50,
                                    NIFTY BANK, MCX CRUDEOIL continuous - raw, not roll-adjusted;
                                    Binance USD-M daily klines BTCUSDT, ETHUSDT - UTC days)
  data/risk_core_seed_funding.csv   sleeve, trade_date, funding                  (8-hourly rates summed by
                                    UTC day, paid by a long)
  data/risk_core_seed_fx.csv        trade_date, usd_inr                          (ECB via frankfurter.dev)
  data/risk_core_research_ref.csv   trade_date, trend_vol                        (the research book's daily
                                    returns, for the parity test)

    python scripts/build_risk_core_seed.py [strategy_tester_dir]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ST = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Strategy Tester"
CACHE = ST / "data" / "cache"
FIRST = "2018-01-01"                 # enough history for every rule (SMA 200, 180-day return) before 2020-07


def main() -> None:
    k, b = CACHE / "kite_daily", CACHE / "binance_daily"
    sources = {"nifty50": k / "NSE_NIFTY_50.parquet", "banknifty": k / "NSE_NIFTY_BANK.parquet",
               "crudeoil": k / "MCX_CRUDEOIL_CONT.parquet", "btcusdt": b / "BTCUSDT_1d.parquet",
               "ethusdt": b / "ETHUSDT_1d.parquet"}
    bars = []
    for sleeve, path in sources.items():
        d = pd.read_parquet(path)
        d.index = pd.to_datetime(d.index).normalize()
        d = d[~d.index.duplicated(keep="last")].sort_index()
        d = d[d.index >= FIRST][["open", "high", "low", "close"]].astype(float)
        bars.append(d.assign(sleeve=sleeve, trade_date=d.index.strftime("%Y-%m-%d")))
    bars = pd.concat(bars)[["sleeve", "trade_date", "open", "high", "low", "close"]]
    bars.to_csv(ROOT / "data" / "risk_core_seed_bars.csv", index=False, float_format="%.6f")
    fund = []
    for sleeve, sym in (("btcusdt", "BTCUSDT"), ("ethusdt", "ETHUSDT")):
        f = pd.read_parquet(b / f"{sym}_funding.parquet")["funding"]
        f.index = pd.to_datetime(f.index).normalize()
        f = f[f.index >= FIRST]
        fund.append(pd.DataFrame({"sleeve": sleeve, "trade_date": f.index.strftime("%Y-%m-%d"), "funding": f.to_numpy()}))
    pd.concat(fund).to_csv(ROOT / "data" / "risk_core_seed_funding.csv", index=False, float_format="%.10f")
    rates = json.loads((CACHE / "fx" / "USD_INR.json").read_text())["rates"]
    pd.DataFrame(sorted(rates.items()), columns=["trade_date", "usd_inr"]).to_csv(
        ROOT / "data" / "risk_core_seed_fx.csv", index=False)
    ref = pd.read_parquet(ST / "research" / "output" / "risk_core_all_five.parquet")["trend_vol"]
    pd.DataFrame({"trade_date": ref.index.strftime("%Y-%m-%d"), "trend_vol": ref.to_numpy()}).to_csv(
        ROOT / "data" / "risk_core_research_ref.csv", index=False, float_format="%.12f")
    ends = bars.groupby("sleeve").trade_date.max().to_dict()
    print(f"bars {len(bars)} rows, ends {ends}; funding to {pd.concat(fund).trade_date.max()}; "
          f"fx {len(rates)} days to {max(rates)}; research reference {len(ref)} days")


if __name__ == "__main__":
    main()
