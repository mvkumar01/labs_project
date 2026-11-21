"""Rule-based natural-language prompt -> backtest/analysis spec.

Works offline with no API key. When Claude is configured (see llm.py) it is used first and
this parser is the fallback.
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

from . import data
from .strategies import STRATEGIES

NUM = r"(\d+(?:\.\d+)?)"

ANALYZE_RE = re.compile(
    r"\b(should i|shall i|is it (?:a )?(?:good|wise|right|smart|worth|safe|the right time)|worth (?:it|investing|buying)|"
    r"good (?:investment|buy|stock|time)|invest(?:ing)? in|buy or sell|recommend|analy[sz]e|analysis of|outlook|"
    r"fundamentals?|is .{1,40} (?:a )?good|undervalued|overvalued|long[- ]term hold|evaluate)\b", re.I)
COMPARE_RE = re.compile(r"\b(compare|comparison|versus|vs\.?|against|which (?:one|is better)|better)\b", re.I)
ALL_STRATS_RE = re.compile(r"\b(all strategies|every strategy|which strateg(?:y|ies)|what strateg(?:y|ies)|"
                           r"best strateg(?:y|ies)|top strateg(?:y|ies)|strategies work)\b", re.I)

STRATEGY_PATTERNS: list[tuple[str, str]] = [
    ("buy_hold", r"buy[\s\-&]*(?:and|n|&)?[\s\-]*hold"),
    ("sma_cross", r"golden cross|death cross"),
    ("supertrend", r"super\s*trend"),
    ("macd", r"\bmacd\b"),
    ("rsi", r"\brsi\b|relative strength index"),
    ("bollinger", r"bollinger|\bbb\b|bands?\b"),
    ("donchian", r"donchian|turtle|channel breakout|\d+[- ]?(?:day|bar|week)s?[- ]high"),
    ("stochastic", r"stochastic|\bstoch\b"),
    ("momentum", r"momentum"),
    ("ema", r"\bema\b|exponential moving average"),
    ("sma", r"\bsma\b|\bdma\b|\bma\b|simple moving average|moving averages?|crossover|cross[- ]over"),
    ("donchian", r"breakouts?"),
]


def _numbers(seg: str) -> list[float]:
    out = []
    seg = re.sub(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4}", " ", seg)
    for m in re.finditer(r"(?<![\w.$₹€£])(\d+(?:\.\d+)?)(?![\d.])(?!\s*%|\s*(?:years?|yrs?|bps|basis|k\b|lakh|crore))", seg):
        v = float(m.group(1))
        if 1900 <= v <= 2100 and v.is_integer():
            continue
        out.append(v)
    return out


def _find_strategies(text: str) -> list[tuple[str, int, int]]:
    low = text.lower()
    hits: list[tuple[str, int, int]] = []
    for key, pat in STRATEGY_PATTERNS:
        for m in re.finditer(pat, low):
            if any(not (m.end() <= s or m.start() >= e) for _, s, e in hits):
                continue
            hits.append((key, m.start(), m.end()))
    hits.sort(key=lambda x: x[1])
    # merge duplicates of the same family that sit next to each other ("sma crossover")
    merged: list[tuple[str, int, int]] = []
    for h in hits:
        # "bollinger band breakout": the generic word "breakout" qualifies the previous strategy
        if merged and low[h[1]:h[2]].startswith("breakout") and h[1] - merged[-1][2] < 25:
            merged[-1] = (merged[-1][0], merged[-1][1], h[2])
            continue
        if merged and merged[-1][0] == h[0] and h[1] - merged[-1][2] < 25:
            merged[-1] = (h[0], merged[-1][1], h[2])
        elif merged and {merged[-1][0], h[0]} <= {"sma", "ema"} and h[1] - merged[-1][2] < 25:
            continue
        else:
            merged.append(h)
    return merged


def _strategy_params(key: str, seg: str, notes: list[str]) -> tuple[str, dict]:
    low = seg.lower()
    nums = _numbers(low)
    if key == "buy_hold":
        return "buy_hold", {}
    if key in ("sma", "ema"):
        if "golden" in low or "death" in low:
            return "sma_cross", {"fast": 50, "slow": 200}
        ints = [n for n in nums if n >= 2]
        if len(ints) >= 2:
            return f"{key}_cross", {"fast": min(ints[:2]), "slow": max(ints[:2])}
        if len(ints) == 1:
            if key == "sma":
                return "price_sma", {"period": ints[0]}
            notes.append(f"Interpreted a single {int(ints[0])}-EMA as an EMA {max(2, int(ints[0] // 3))}/{int(ints[0])} crossover.")
            return "ema_cross", {"fast": max(2, ints[0] // 3), "slow": ints[0]}
        if key == "sma" and re.search(r"above|below|filter|trend filter", low) and not re.search(r"cross", low):
            return "price_sma", {}
        return f"{key}_cross", {}
    if key == "sma_cross":
        return "sma_cross", {"fast": 50, "slow": 200}
    if key == "rsi":
        p: dict = {}
        m = re.search(r"rsi\s*\(?\s*(\d+)", low) or re.search(r"(\d+)[- ]?(?:period|day|bar)s?\s*rsi", low)
        if m:
            p["period"] = float(m.group(1))
        lo = re.search(r"(?:below|under|<|oversold(?: at| level| of)?|drops? (?:below|under)|less than|crosses below)\s*(\d+)", low)
        hi = re.search(r"(?:above|over|>|overbought(?: at| level| of)?|greater than|exceeds?|crosses above|rises above)\s*(\d+)", low)
        if lo:
            p["lower"] = float(lo.group(1))
        if hi:
            p["upper"] = float(hi.group(1))
            p["exit_level"] = float(hi.group(1))
        ex = re.search(r"exit (?:at|when rsi (?:is )?(?:above|over|crosses)?)\s*(\d+)", low)
        if ex:
            p["exit_level"] = float(ex.group(1))
        if not lo and not hi and len(nums) >= 3 and "period" in p:
            p["lower"], p["upper"] = nums[1], nums[2]
        return "rsi", p
    if key == "macd":
        ints = [n for n in nums if n >= 2]
        names = ["fast", "slow", "signal"]
        return "macd", {names[i]: v for i, v in enumerate(ints[:3])}
    if key == "bollinger":
        mode = "bollinger_breakout" if re.search(r"breakout|break out|momentum|above the upper", low) else "bollinger_reversion"
        p = {}
        periods = [n for n in nums if n >= 5]
        stds = [n for n in nums if n < 5]
        m = re.search(NUM + r"\s*(?:std|standard dev|sd|sigma)", low)
        if m:
            p["std"] = float(m.group(1))
        elif stds:
            p["std"] = stds[0]
        if periods:
            p["period"] = periods[0]
        return mode, p
    if key == "donchian":
        ints = [n for n in nums if n >= 2]
        if "turtle" in low and not ints:
            return "donchian", {"entry": 20, "exit": 10}
        if len(ints) >= 2:
            return "donchian", {"entry": ints[0], "exit": ints[1]}
        if len(ints) == 1:
            return "donchian", {"entry": ints[0], "exit": max(2, ints[0] // 2)}
        return "donchian", {}
    if key == "momentum":
        m = re.search(r"(\d+)[- ]?(?:month|mo)s?", low)
        if m:
            return "momentum", {"lookback": float(m.group(1)) * 21}
        m = re.search(r"(\d+)[- ]?(?:week|wk)s?", low)
        if m:
            return "momentum", {"lookback": float(m.group(1)) * 5}
        ints = [n for n in nums if n >= 5]
        return "momentum", ({"lookback": ints[0]} if ints else {})
    if key == "supertrend":
        p = {}
        ints = [n for n in nums]
        if ints:
            p["period"] = ints[0]
        if len(ints) > 1:
            p["mult"] = ints[1]
        return "supertrend", p
    if key == "stochastic":
        ints = [n for n in nums if n >= 1]
        p = {}
        if ints:
            p["k"] = ints[0]
        if len(ints) > 1:
            p["d"] = ints[1]
        return "stochastic", p
    return key, {}


def _parse_date(s: str, end: bool = False) -> str | None:
    s = s.strip().strip(",.")
    if re.fullmatch(r"\d{4}", s):
        return f"{s}-12-31" if end else f"{s}-01-01"
    try:
        ts = pd.Timestamp(s)
    except Exception:
        return None
    if end and re.fullmatch(r"[A-Za-z]+\.?\s+\d{4}", s):
        ts = ts + pd.offsets.MonthEnd(0)
    return ts.strftime("%Y-%m-%d")


DATE_TOKEN = r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|[A-Za-z]{3,9}\.?\s+\d{4}|\d{4})"


def _dates(text: str, interval: str, notes: list[str]) -> tuple[str | None, str | None]:
    low = text.lower()
    today = date.today()
    m = re.search(r"(?:from|between)\s+" + DATE_TOKEN + r"\s+(?:to|and|until|till|through|-)\s+" + DATE_TOKEN, text, re.I)
    if m:
        return _parse_date(m.group(1)), _parse_date(m.group(2), end=True)
    m = re.search(r"\b" + r"(\d{4})\s*[-–]\s*(\d{4})\b", text)
    if m and 1900 < int(m.group(1)) < 2100 and 1900 < int(m.group(2)) < 2100:
        return f"{m.group(1)}-01-01", f"{m.group(2)}-12-31"
    m = re.search(r"(?:since|from|starting(?: in| from)?|after|beginning)\s+" + DATE_TOKEN, text, re.I)
    if m:
        return _parse_date(m.group(1)), None
    m = re.search(r"(?:last|past|over|previous|prior|recent)\s+(?:the\s+)?(\d+|one|two|three|five|ten|twenty)?\s*(years?|yrs?|y\b|months?|mos?|weeks?|days?|decade)", low)
    if m:
        words = {"one": 1, "two": 2, "three": 3, "five": 5, "ten": 10, "twenty": 20}
        n = m.group(1)
        n = words.get(n, None) if n and not n.isdigit() else (int(n) if n else 1)
        unit = m.group(2)
        if unit.startswith("decade"):
            off = pd.DateOffset(years=10 * (n or 1))
        elif unit.startswith("y"):
            off = pd.DateOffset(years=n)
        elif unit.startswith("mo"):
            off = pd.DateOffset(months=n)
        elif unit.startswith("w"):
            off = pd.DateOffset(weeks=n)
        else:
            off = pd.DateOffset(days=n)
        return (pd.Timestamp(today) - off).strftime("%Y-%m-%d"), None
    if re.search(r"\bytd\b|year to date|this year", low):
        return f"{today.year}-01-01", None
    m = re.search(r"\b(?:in|during|for)\s+(\d{4})\b", text)
    if m and 1900 < int(m.group(1)) <= today.year:
        return f"{m.group(1)}-01-01", f"{m.group(1)}-12-31"
    if re.search(r"\b(all|full|entire|max(?:imum)?)\s+(history|data|period)\b", low):
        return None, None
    years = 2 if interval == "1h" else 10
    notes.append(f"No date range given, so the test uses the last {years} years.")
    return (pd.Timestamp(today) - pd.DateOffset(years=years)).strftime("%Y-%m-%d"), None


def _interval(low: str) -> str:
    if re.search(r"\b(hourly|1h|60m|1 hour|intraday|h1)\b", low):
        return "1h"
    if re.search(r"\b(weekly|1wk|weekly bars|w1)\b", low):
        return "1wk"
    if re.search(r"\bmonthly (?:bars|candles|data)\b", low):
        return "1mo"
    return "1d"


def _money(v: str, unit: str | None) -> float:
    x = float(v.replace(",", ""))
    unit = (unit or "").lower()
    return x * {"k": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5,
                "crore": 1e7, "cr": 1e7}.get(unit, 1)


def _config(text: str) -> dict:
    low = text.lower()
    cfg: dict = {}
    m = re.search(r"(?:capital|starting|initial|account(?: size)?|portfolio|invest(?:ing|ment)?)\s*(?:of|with|is|=|:)?\s*[$₹€£]?\s*([\d,]+(?:\.\d+)?)\s*(k|m|mn|million|lakhs?|lac|crore|cr)?\b", low) \
        or re.search(r"[$₹€£]\s*([\d,]+(?:\.\d+)?)\s*(k|m|mn|million|lakhs?|lac|crore|cr)?\b", low) \
        or re.search(r"\b([\d,]+(?:\.\d+)?)\s*(lakhs?|lac|crore|cr)\b", low)
    if m:
        amt = _money(m.group(1), m.group(2))
        if amt >= 100:
            cfg["capital"] = amt
    m = re.search(r"trailing\s*(?:stop)?\s*(?:loss)?\s*(?:of|at|=)?\s*" + NUM + r"\s*%", low) or \
        re.search(NUM + r"\s*%\s*trailing", low)
    if m:
        cfg["trailing_stop_pct"] = float(m.group(1))
    stop_text = re.sub(r"trailing\s*(?:stop)?\s*(?:loss)?\s*(?:of|at|=)?\s*\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*%\s*trailing\s*(?:stop)?\s*(?:loss)?", "", low)
    m = re.search(r"(?:stop[- ]?loss|\bsl\b|\bstop\b)\s*(?:of|at|=|:)?\s*" + NUM + r"\s*%", stop_text) or \
        re.search(NUM + r"\s*%\s*(?:stop[- ]?loss|\bsl\b|\bstop\b)", stop_text)
    if m:
        cfg["stop_loss_pct"] = float(m.group(1))
    m = re.search(r"(?:take[- ]?profit|\btp\b|profit target|\btarget\b)\s*(?:of|at|=|:)?\s*" + NUM + r"\s*%", low) or \
        re.search(NUM + r"\s*%\s*(?:take[- ]?profit|\btp\b|profit target|\btarget\b)", low)
    if m:
        cfg["take_profit_pct"] = float(m.group(1))
    if re.search(r"\b(?:no|zero|without)\s+(?:fees|commissions?|costs|transaction costs|brokerage)\b", low):
        cfg["commission_bps"] = 0
        cfg["slippage_bps"] = 0
    else:
        m = re.search(r"(?:commission|fees?|brokerage|costs?)\s*(?:of|at|=|:)?\s*" + NUM + r"\s*(%|bps|basis points?)", low) or \
            re.search(NUM + r"\s*(%|bps|basis points?)\s*(?:per side\s*)?(?:commission|fees?|brokerage|costs?)", low)
        if m:
            v = float(m.group(1))
            cfg["commission_bps"] = v * 100 if m.group(2) == "%" else v
        m = re.search(r"slippage\s*(?:of|at|=|:)?\s*" + NUM + r"\s*(%|bps|basis points?)", low) or \
            re.search(NUM + r"\s*(%|bps|basis points?)\s*slippage", low)
        if m:
            v = float(m.group(1))
            cfg["slippage_bps"] = v * 100 if m.group(2) == "%" else v
    if re.search(r"long[\s/&\-]*(?:and|&|/|\+)?[\s\-]*short|both (?:sides|directions)|allow(?:ing)? shorts?|with shorts?|shorting|short selling|go short", low):
        cfg["allow_short"] = True
    if re.search(r"long[\s\-]only", low):
        cfg["allow_short"] = False
    m = re.search(NUM + r"\s*%\s*(?:of (?:capital|equity|the portfolio|portfolio|account)|position size|allocation|per trade)", low) \
        or re.search(r"(?:position size|allocation|risk)\s*(?:of|=|:)?\s*" + NUM + r"\s*%", low)
    if m:
        cfg["position_size"] = max(0.01, min(1.0, float(m.group(1)) / 100))
    return cfg


_SKIP_WORDS = r"\b(stock|stocks|shares?|share price|futures?|price|index|the|a|an|ltd|limited|inc|company|corp|equity|now|today|right now|currently|long term|long-term|short term|for me|my money)\b"


def _fallback_symbol(text: str) -> str | None:
    low = text.lower()
    pats = [r"(?:invest(?:ing)? in|buy(?:ing)?|on|for|of|in|into|about|analy[sz]e|trade|trading|hold(?:ing)?)\s+([a-z][a-z0-9 .&'\-]{1,40}?)(?=\s+(?:from|since|between|with|using|over|for|during|in|stock|shares?|now|today|right|at|as|if|and|or|vs|versus|\d)\b|[,?.!]|$)"]
    for pat in pats:
        for m in re.finditer(pat, low):
            cand = re.sub(_SKIP_WORDS, " ", m.group(1)).strip()
            cand = re.sub(r"\s+", " ", cand)
            if not cand or len(cand) < 2:
                continue
            if any(re.search(p, cand) for _, p in STRATEGY_PATTERNS):
                continue
            if re.fullmatch(r"(it|this|that|them|these|those|which|what|gold|me|us|strategy|strategies|backtest|test)", cand):
                continue
            sym = data.search_symbol(cand)
            if sym:
                return sym
    return None


def parse(prompt: str) -> dict:
    notes: list[str] = []
    text = prompt.strip()
    low = text.lower()
    interval = _interval(low)
    symbols = data.extract_symbols(text)
    if not symbols:
        fb = _fallback_symbol(text)
        if fb:
            symbols = [fb]
            notes.append(f"Resolved the instrument to {fb} using Yahoo Finance search.")

    hits = _find_strategies(text)
    strategies: list[dict] = []
    for j, (key, s, e) in enumerate(hits):
        seg_start = max(hits[j - 1][2] if j else 0, s - 22)
        seg_end = hits[j + 1][1] if j + 1 < len(hits) else min(len(text), e + 70)
        seg = text[seg_start:seg_end]
        # don't let dates / money / risk settings bleed into params
        seg = re.sub(r"(?:since|from|to|between|and|until|in)\s+\d{4}(?:-\d\d-\d\d)?", " ", seg, flags=re.I)
        seg = re.sub(r"[$₹€£]\s*[\d,]+(?:\.\d+)?\s*[kKmM]?", " ", seg)
        seg = re.sub(r"\d+(?:\.\d+)?\s*%", " ", seg)
        for sym in symbols:
            seg = seg.replace(sym, " ")
        k, p = _strategy_params(key, seg, notes)
        if not any(x["key"] == k and x["params"] == p for x in strategies):
            strategies.append({"key": k, "params": p})

    want_all = bool(ALL_STRATS_RE.search(text))
    analyze = bool(ANALYZE_RE.search(text)) and not re.search(r"\bbacktest|back-test|back test\b", low)

    if want_all:
        strategies = [{"key": k, "params": {}} for k in STRATEGIES if k != "buy_hold"]
        intent = "compare"
    elif analyze and not [s for s in strategies if s["key"] != "buy_hold"]:
        intent = "analyze"
        strategies = []
    elif not strategies:
        intent = "compare"
        strategies = [{"key": k, "params": {}} for k in STRATEGIES if k != "buy_hold"]
        notes.append("No strategy was named, so every built-in strategy was tested and ranked.")
    elif len(strategies) > 1 or len(symbols) > 1:
        intent = "compare"
    else:
        intent = "backtest"

    start, end = _dates(text, interval, notes if intent != "analyze" else [])
    return {
        "intent": intent,
        "evaluate": analyze,
        "symbols": symbols[:6],
        "strategies": strategies,
        "start": start, "end": end, "interval": interval,
        "config": _config(text),
        "notes": notes,
        "parser": "rules",
    }
