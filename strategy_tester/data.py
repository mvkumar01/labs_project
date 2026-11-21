"""Market data from Yahoo Finance: symbol resolution, asset-class detection and caching.

Two interchangeable providers:
  * yfinance  - used when a recent version (>= 0.2.54) is installed.
  * direct    - a small built-in client for Yahoo's public chart/search/quoteSummary
                endpoints, used when yfinance is missing or too old to talk to Yahoo
                (e.g. a host with an outdated system package). Same data, same source.
Set ST_DATA_PROVIDER=direct or =yfinance to force one.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("strategy-tester.data")


class SymbolNotFound(ValueError):
    """Yahoo has no data for this symbol (as opposed to Yahoo being unreachable)."""

CACHE_DIR = Path(os.environ.get("ST_CACHE_DIR") or Path(__file__).resolve().parent / "data_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 6 * 3600


def _usable_yfinance():
    if os.environ.get("ST_DATA_PROVIDER") == "direct":
        return None
    try:
        import yfinance
    except Exception:
        return None
    ver = tuple(int(x) for x in re.findall(r"\d+", getattr(yfinance, "__version__", "0"))[:3])
    if ver < (0, 2, 54) and os.environ.get("ST_DATA_PROVIDER") != "yfinance":
        return None
    return yfinance


yf = _usable_yfinance()
PROVIDER = "yfinance" if yf else "yahoo-direct"


# --------------------------------------------------------------------------------------
# Built-in Yahoo Finance client (fallback provider)
# --------------------------------------------------------------------------------------
class _YahooDirect:
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130.0.0.0 Safari/537.36")
    HOSTS = ("query2", "query1")

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": self.UA, "Accept": "application/json,text/plain,*/*"})
        self._crumb: str | None = None
        self._lock = threading.Lock()

    def _get(self, path: str, params: dict, timeout: int = 30) -> requests.Response:
        last = None
        for h in self.HOSTS:
            try:
                r = self.s.get(f"https://{h}.finance.yahoo.com{path}", params=params, timeout=timeout)
            except requests.RequestException as e:
                last = e
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 404:          # Yahoo answers 404 for a symbol it does not know
                raise SymbolNotFound("Yahoo Finance has no such symbol")
            last = RuntimeError(f"Yahoo returned HTTP {r.status_code}")
        raise ValueError(f"Could not reach Yahoo Finance ({last}).")

    def history(self, ticker: str, interval: str) -> pd.DataFrame:
        params = {"interval": interval, "includeAdjustedClose": "true", "events": "div,split"}
        if interval in ("1h", "60m"):
            params["range"] = "730d"
        elif interval in ("30m", "15m"):
            params["range"] = "60d"
        else:
            params.update({"period1": -2208994789, "period2": int(time.time()) + 86400})
        j = self._get(f"/v8/finance/chart/{quote(ticker, safe='')}", params).json()
        res = (j.get("chart") or {}).get("result")
        if not res or not res[0].get("timestamp"):
            return pd.DataFrame()
        res = res[0]
        qd = res["indicators"]["quote"][0]
        tz = res.get("meta", {}).get("exchangeTimezoneName") or "UTC"
        idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(tz)
        df = pd.DataFrame({k.capitalize(): pd.to_numeric(pd.Series(qd.get(k)), errors="coerce").to_numpy()
                           for k in ("open", "high", "low", "close", "volume")}, index=idx)
        adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
        if adj is not None:
            adj = pd.to_numeric(pd.Series(adj), errors="coerce").to_numpy()
            factor = np.where(df["Close"].to_numpy() > 0, adj / df["Close"].to_numpy(), 1.0)
            factor = np.where(np.isfinite(factor), factor, 1.0)
            for c in ("Open", "High", "Low"):
                df[c] = df[c] * factor
            df["Close"] = np.where(np.isfinite(adj), adj, df["Close"])
        return df

    def search(self, query: str) -> list[dict]:
        j = self._get("/v1/finance/search", {"q": query, "quotesCount": 6, "newsCount": 0}, timeout=15).json()
        return j.get("quotes") or []

    def _crumb_value(self, refresh: bool = False) -> str | None:
        with self._lock:
            if self._crumb and not refresh:
                return self._crumb
            try:
                self.s.get("https://fc.yahoo.com", timeout=15, allow_redirects=True)
                r = self.s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=15)
                self._crumb = r.text.strip() if r.status_code == 200 and r.text and "<" not in r.text else None
            except requests.RequestException:
                self._crumb = None
            return self._crumb

    def info(self, ticker: str) -> dict:
        modules = "price,summaryDetail,defaultKeyStatistics,financialData,assetProfile"
        for attempt in range(2):
            crumb = self._crumb_value(refresh=attempt > 0)
            if not crumb:
                return {}
            try:
                r = self.s.get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{quote(ticker, safe='')}",
                               params={"modules": modules, "crumb": crumb}, timeout=20)
            except requests.RequestException:
                return {}
            if r.status_code == 401:
                continue
            if r.status_code != 200:
                return {}
            res = ((r.json().get("quoteSummary") or {}).get("result") or [{}])[0]
            flat: dict = {}
            for mod in res.values():
                if isinstance(mod, dict):
                    for k, v in mod.items():
                        v = v.get("raw") if isinstance(v, dict) else v
                        if v is not None and v != {} and k not in flat:
                            flat[k] = v
            if "dividendYield" in flat and isinstance(flat["dividendYield"], (int, float)):
                flat["dividendYield"] = flat["dividendYield"] * 100  # match yfinance (percent)
            return flat
        return {}


_direct = _YahooDirect()

# --------------------------------------------------------------------------------------
# Aliases: natural-language names -> Yahoo Finance tickers
# --------------------------------------------------------------------------------------
COMMODITIES = {
    "gold": "GC=F", "silver": "SI=F", "crude oil": "CL=F", "crude": "CL=F", "wti": "CL=F",
    "oil": "CL=F", "brent": "BZ=F", "brent crude": "BZ=F", "natural gas": "NG=F", "natgas": "NG=F",
    "nat gas": "NG=F", "copper": "HG=F", "platinum": "PL=F", "palladium": "PA=F", "corn": "ZC=F",
    "wheat": "ZW=F", "soybeans": "ZS=F", "soybean": "ZS=F", "soy": "ZS=F", "coffee": "KC=F",
    "sugar": "SB=F", "cotton": "CT=F", "cocoa": "CC=F", "heating oil": "HO=F", "gasoline": "RB=F",
    "live cattle": "LE=F", "cattle": "LE=F", "lean hogs": "HE=F", "hogs": "HE=F", "oats": "ZO=F",
    "orange juice": "OJ=F", "lumber": "LBS=F",
}

INDEXES = {
    "s&p 500": "^GSPC", "s&p500": "^GSPC", "sp500": "^GSPC", "s&p": "^GSPC", "spx": "^GSPC",
    "nasdaq 100": "^NDX", "nasdaq100": "^NDX", "ndx": "^NDX", "nasdaq": "^IXIC",
    "nasdaq composite": "^IXIC", "dow jones": "^DJI", "dow": "^DJI", "djia": "^DJI",
    "russell 2000": "^RUT", "russell": "^RUT", "vix": "^VIX",
    "nifty 50": "^NSEI", "nifty50": "^NSEI", "nifty": "^NSEI", "bank nifty": "^NSEBANK",
    "banknifty": "^NSEBANK", "nifty bank": "^NSEBANK", "nifty it": "^CNXIT", "sensex": "^BSESN",
    "ftse 100": "^FTSE", "ftse": "^FTSE", "dax": "^GDAXI", "cac 40": "^FCHI", "cac": "^FCHI",
    "nikkei 225": "^N225", "nikkei": "^N225", "hang seng": "^HSI", "euro stoxx 50": "^STOXX50E",
    "stoxx 50": "^STOXX50E", "asx 200": "^AXJO", "asx": "^AXJO", "kospi": "^KS11",
    "shanghai composite": "000001.SS", "tsx": "^GSPTSE", "ibovespa": "^BVSP",
    "dollar index": "DX-Y.NYB", "dxy": "DX-Y.NYB",
}

EQUITIES = {
    "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL", "alphabet": "GOOGL", "amazon": "AMZN",
    "tesla": "TSLA", "nvidia": "NVDA", "meta": "META", "facebook": "META", "netflix": "NFLX",
    "amd": "AMD", "intel": "INTC", "berkshire": "BRK-B", "jpmorgan": "JPM", "jp morgan": "JPM",
    "coca cola": "KO", "coca-cola": "KO", "pepsi": "PEP", "walmart": "WMT", "disney": "DIS",
    "visa": "V", "mastercard": "MA", "exxon": "XOM", "chevron": "CVX", "boeing": "BA",
    "broadcom": "AVGO", "oracle": "ORCL", "salesforce": "CRM", "adobe": "ADBE", "palantir": "PLTR",
    "reliance": "RELIANCE.NS", "tcs": "TCS.NS", "infosys": "INFY.NS", "hdfc bank": "HDFCBANK.NS",
    "icici bank": "ICICIBANK.NS", "sbi": "SBIN.NS", "state bank of india": "SBIN.NS",
    "itc": "ITC.NS", "wipro": "WIPRO.NS", "tata motors": "TATAMOTORS.NS", "tata steel": "TATASTEEL.NS",
    "bharti airtel": "BHARTIARTL.NS", "airtel": "BHARTIARTL.NS", "larsen": "LT.NS",
    "hindustan unilever": "HINDUNILVR.NS", "bajaj finance": "BAJFINANCE.NS", "adani enterprises": "ADANIENT.NS",
    "maruti": "MARUTI.NS", "asian paints": "ASIANPAINT.NS", "kotak bank": "KOTAKBANK.NS",
    "axis bank": "AXISBANK.NS", "sun pharma": "SUNPHARMA.NS", "hcl tech": "HCLTECH.NS",
    "bitcoin": "BTC-USD", "ethereum": "ETH-USD", "spy": "SPY", "qqq": "QQQ",
}

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "INR", "AUD", "CAD", "CHF", "NZD", "CNY", "SGD", "HKD",
              "ZAR", "MXN", "SEK", "NOK", "TRY", "BRL", "KRW"]
CURRENCY_NAMES = {
    "euro": "EUR", "dollar": "USD", "pound": "GBP", "sterling": "GBP", "cable": "GBPUSD",
    "yen": "JPY", "rupee": "INR", "aussie": "AUD", "loonie": "CAD", "franc": "CHF", "kiwi": "NZD",
    "yuan": "CNY",
}

ALL_ALIASES: dict[str, str] = {**EQUITIES, **COMMODITIES, **INDEXES}

ASSET_LABELS = {"equity": "Equity", "index": "Index", "commodity": "Commodity", "forex": "Forex",
                "crypto": "Crypto", "etf": "ETF"}


def is_indian(ticker: str) -> bool:
    """NSE / BSE listings and the Indian indices (used for the Indian cost model and the no-overnight-short rule)."""
    t = ticker.upper()
    return t.endswith((".NS", ".BO")) or t in {"^NSEI", "^NSEBANK", "^BSESN", "^CNXIT", "^NSMIDCP", "^CNXFIN"}


def asset_class(ticker: str) -> str:
    t = ticker.upper()
    if t.endswith("=X"):
        return "forex"
    if t.endswith("=F"):
        return "commodity"
    if t.startswith("^") or t in {"DX-Y.NYB", "000001.SS"}:
        return "index"
    if t.endswith("-USD"):
        return "crypto"
    if t in {"SPY", "QQQ", "IWM", "GLD", "SLV", "USO", "TLT", "DIA", "EEM", "VTI", "VOO"}:
        return "etf"
    return "equity"


FRIENDLY_NAMES = {v: k for k, v in reversed(list(ALL_ALIASES.items()))}


def display_name(ticker: str) -> str:
    if ticker.endswith("=X"):
        p = ticker[:-2]
        return f"{p[:3]}/{p[3:]}" if len(p) == 6 else p
    name = FRIENDLY_NAMES.get(ticker)
    if name:
        return name.upper() if len(name) <= 4 else name.title().replace("S&P", "S&P")
    return ticker


# --------------------------------------------------------------------------------------
# Symbol extraction from free text
# --------------------------------------------------------------------------------------
_TICKER_STOPWORDS = {
    "SMA", "EMA", "RSI", "MACD", "ATR", "ADX", "BB", "SL", "TP", "VS", "AND", "OR", "THE", "ON", "IN",
    "FOR", "OF", "TO", "A", "I", "IS", "IT", "MY", "ME", "WITH", "FROM", "SINCE", "BUY", "SELL",
    "LONG", "SHORT", "USD", "EUR", "GBP", "JPY", "INR", "CAGR", "ETF", "ETFS", "YTD", "DMA", "PE",
    "AI", "US", "UK", "OK", "DO", "IF", "AT", "BE", "AS", "AN", "BY", "UP", "VWAP", "OBV", "RS",
    "STOP", "LOSS", "TEST", "RUN", "WHAT", "HOW", "WHY", "SHOULD", "GOOD", "BAD", "NOW", "BEST",
    "MA", "KD", "CCI", "ROC", "OHLC", "FX", "PCT", "BPS", "X", "VERSUS", "ALL",
}

_FX_PAIR = re.compile(r"\b(" + "|".join(CURRENCIES) + r")\s*[/\-]?\s*(" + "|".join(CURRENCIES) + r")\b", re.I)
_EXPLICIT_TICKER = re.compile(r"(?<![\w^])(\^?[A-Z][A-Z0-9&]{0,9}(?:[.\-][A-Z]{1,4}|=[XF])?)(?![\w])")
_LOWER_TICKER = re.compile(r"\$([a-zA-Z][a-zA-Z0-9.\-]{0,9})")


def _fx_ticker(a: str, b: str) -> str:
    a, b = a.upper(), b.upper()
    return f"{a}{b}=X"


def extract_symbols(text: str) -> list[str]:
    """Find every instrument mentioned in a prompt, preserving order of appearance."""
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= ts or s >= te for ts, te in taken)

    def add(pos: int, end: int, ticker: str):
        if free(pos, end):
            taken.append((pos, end))
            found.append((pos, ticker))

    # FX pairs (EURUSD, EUR/USD, eur usd)
    for m in _FX_PAIR.finditer(text):
        if m.group(1).upper() != m.group(2).upper():
            add(m.start(), m.end(), _fx_ticker(m.group(1), m.group(2)))

    low = text.lower()
    # Named currencies: "euro vs dollar" / "dollar rupee"
    names = [(m.start(), m.end(), CURRENCY_NAMES[m.group(1)]) for m in
             re.finditer(r"\b(" + "|".join(CURRENCY_NAMES) + r")\b", low)]
    i = 0
    while i < len(names):
        s, e, code = names[i]
        if len(code) == 6:
            add(s, e, code + "=X"); i += 1; continue
        if i + 1 < len(names) and names[i + 1][0] - e < 12 and len(names[i + 1][2]) == 3:
            add(s, names[i + 1][1], _fx_ticker(code, names[i + 1][2])); i += 2; continue
        i += 1

    # Aliases, longest first
    for alias in sorted(ALL_ALIASES, key=len, reverse=True):
        for m in re.finditer(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", low):
            add(m.start(), m.end(), ALL_ALIASES[alias])

    # $aapl style
    for m in _LOWER_TICKER.finditer(text):
        add(m.start(), m.end(), m.group(1).upper())

    # Explicit upper-case tickers (AAPL, RELIANCE.NS, ^GSPC, GC=F)
    for m in _EXPLICIT_TICKER.finditer(text):
        tok = m.group(1)
        if tok.upper() in _TICKER_STOPWORDS or tok.isdigit():
            continue
        if len(tok) == 1 and tok not in {"V", "F", "T", "C"}:
            continue
        add(m.start(), m.end(), tok.upper())

    found.sort()
    out: list[str] = []
    for _, t in found:
        if t not in out:
            out.append(t)
    return out


def search_symbol(query: str) -> str | None:
    """Fallback: ask Yahoo's search endpoint for the best match."""
    query = query.strip()
    if not query:
        return None
    try:
        quotes = yf.Search(query, max_results=5).quotes if yf is not None and hasattr(yf, "Search") \
            else _direct.search(query)
    except Exception:
        return None
    preferred = ["EQUITY", "INDEX", "FUTURE", "CURRENCY", "ETF", "CRYPTOCURRENCY"]
    quotes = [q for q in quotes if q.get("symbol")]
    quotes.sort(key=lambda q: preferred.index(q.get("quoteType")) if q.get("quoteType") in preferred else 99)
    return quotes[0]["symbol"] if quotes else None


def suggest_symbol(raw: str) -> tuple[str, str] | None:
    """Best guess for a symbol Yahoo does not know (usually a typo): (ticker, how it was read). Close names in the
    alias table first ("nvdia" -> nvidia -> NVDA), then close tickers ("NVDIA" -> NVDA), then Yahoo's search."""
    word = raw.strip().lstrip("$^")
    low = word.lower()
    m = difflib.get_close_matches(low, list(ALL_ALIASES), n=1, cutoff=0.75)
    if m:
        return ALL_ALIASES[m[0]], f"{m[0]}"
    tickers = sorted(set(ALL_ALIASES.values()) | {t.split(".")[0] for t in ALL_ALIASES.values()})
    m = difflib.get_close_matches(word.upper(), tickers, n=1, cutoff=0.8)
    if m:
        t = m[0] if m[0] in ALL_ALIASES.values() else next(v for v in ALL_ALIASES.values() if v.split(".")[0] == m[0])
        return t, FRIENDLY_NAMES.get(t, t)
    s = search_symbol(word)
    if s and s.upper() != word.upper():
        return s, s
    return None


# --------------------------------------------------------------------------------------
# Price history
# --------------------------------------------------------------------------------------
@dataclass
class PriceData:
    ticker: str
    name: str
    asset_class: str
    interval: str
    df: pd.DataFrame  # columns: Open High Low Close Volume, tz-naive DatetimeIndex
    currency: str | None = None


_mem_cache: dict[str, tuple[float, pd.DataFrame]] = {}


CACHE_VERSION = "v2"  # v2: intraday bars kept on the exchange clock (v1 cached them in UTC)


def _cache_key(ticker: str, interval: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{ticker}_{interval}_{CACHE_VERSION}")


def _download(ticker: str, interval: str) -> pd.DataFrame:
    if yf is not None:
        tk = yf.Ticker(ticker)
        if interval in ("1h", "60m", "30m", "15m"):
            period = "730d" if interval in ("1h", "60m") else "60d"
            df = tk.history(period=period, interval=interval, auto_adjust=True)
        else:
            df = tk.history(period="max", interval=interval, auto_adjust=True)
    else:
        df = _direct.history(ticker, interval)
    if df is None or df.empty:
        raise SymbolNotFound(f"No data returned by Yahoo Finance for '{ticker}'. Check the symbol.")
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    if df.index.tz is not None:
        # Both providers return bars on the exchange's own clock; keep it (NIFTY's first hourly bar is 09:15,
        # not 03:45 UTC). Charts receive these wall-clock times as-is.
        df.index = df.index.tz_localize(None)
    if interval in ("1d", "1wk", "1mo"):
        df.index = df.index.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["Close"])
    # Repair occasional zero/NaN OHLC values in futures/FX feeds
    for c in ("Open", "High", "Low"):
        bad = df[c].isna() | (df[c] <= 0)
        df.loc[bad, c] = df.loc[bad, "Close"]
    df["High"] = df[["Open", "High", "Low", "Close"]].max(axis=1)
    df["Low"] = df[["Open", "High", "Low", "Close"]].min(axis=1)
    df["Volume"] = df["Volume"].fillna(0)
    return df


def get_history(ticker: str, interval: str = "1d") -> pd.DataFrame:
    key = _cache_key(ticker, interval)
    now = time.time()
    if key in _mem_cache and now - _mem_cache[key][0] < CACHE_TTL_SECONDS:
        return _mem_cache[key][1]
    path = CACHE_DIR / f"{key}.pkl"
    if path.exists() and now - path.stat().st_mtime < CACHE_TTL_SECONDS:
        df = pd.read_pickle(path)
    else:
        df = _download(ticker, interval)
        df.to_pickle(path)
    _mem_cache[key] = (now, df)
    return df


def load(ticker: str, start: str | None, end: str | None, interval: str = "1d",
         warmup_bars: int = 0) -> tuple[PriceData, pd.Timestamp]:
    """Load data for [start, end], plus `warmup_bars` before start for indicator warm-up.

    Returns the PriceData and the timestamp at which trading should begin.
    """
    df = get_history(ticker, interval)
    end_ts = pd.Timestamp(end) if end else None
    if end_ts is not None:
        # the end date is inclusive: every bar ON that date, none from the next trading day
        df = df[df.index < end_ts.normalize() + pd.Timedelta(days=1)]
    start_ts = pd.Timestamp(start) if start else df.index[0]
    if start_ts < df.index[0]:
        start_ts = df.index[0]
    first_idx = int(df.index.searchsorted(start_ts))
    lo = max(0, first_idx - warmup_bars)
    sliced = df.iloc[lo:]
    if len(sliced) - (first_idx - lo) < 20:
        raise ValueError(f"Not enough data for {ticker} in the requested period.")
    trade_start = df.index[first_idx]
    pdata = PriceData(ticker=ticker, name=display_name(ticker), asset_class=asset_class(ticker),
                      interval=interval, df=sliced)
    return pdata, trade_start


def ticker_info(ticker: str) -> dict:
    """Fundamentals snapshot (best-effort; Yahoo sometimes omits fields)."""
    try:
        info = (yf.Ticker(ticker).info if yf is not None else _direct.info(ticker)) or {}
    except Exception:
        return {}
    keys = ["longName", "shortName", "sector", "industry", "country", "currency", "marketCap",
            "trailingPE", "forwardPE", "priceToBook", "pegRatio", "trailingPegRatio", "profitMargins",
            "operatingMargins", "revenueGrowth", "earningsGrowth", "returnOnEquity", "debtToEquity",
            "dividendYield", "beta", "recommendationKey", "recommendationMean", "targetMeanPrice",
            "currentPrice", "numberOfAnalystOpinions", "freeCashflow", "quoteType", "fiftyTwoWeekHigh",
            "fiftyTwoWeekLow"]
    return {k: info.get(k) for k in keys if info.get(k) is not None}


def today() -> str:
    return date.today().isoformat()
