"""Intraday rule spec and the plain-English reader for it.

A Rule is plain data (JSON-able) so it can be shown, edited, stored and later promoted to a paper book.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass
class Rule:
    symbol: str = "NIFTY"
    side: str = "long"                    # long | short | both | gap (with the opening gap) | trend (with the day's move)
    entry: dict = field(default_factory=lambda: {"type": "time", "at": "09:20"})
    filters: list = field(default_factory=list)
    stop_pts: float | None = None
    target_pts: float | None = None
    trail_pts: float | None = None
    trail_after_pts: float | None = None
    exit_time: str | None = None          # "15:15"; default end of day 15:25
    max_trades: int = 1
    day_loss_pts: float | None = None
    lots: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in (d or {}).items() if k in keys})


SYMBOLS = [("bank nifty", "BANKNIFTY"), ("banknifty", "BANKNIFTY"), ("nifty bank", "BANKNIFTY"), ("sensex", "SENSEX"),
           ("nifty 50", "NIFTY"), ("nifty50", "NIFTY"), ("nifty", "NIFTY"), ("^nsebank", "BANKNIFTY"), ("^bsesn", "SENSEX"),
           ("^nsei", "NIFTY")]
INTRADAY_CUES = re.compile(
    r"\b(intraday|intra-day|scalp\w*|opening range|\borb\b|gap[- ]?(?:up|down)|gaps? (?:up|down)|at \d{1,2}[:.]\d{2}|"
    r"\d{1,2}[:.]\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm)\b|points?\b|pts\b|cpr|previous day'?s? high|previous day'?s? low|"
    r"pdh|pdl|\d+[- ]?min(?:ute)?s?\b|same day|square off|exit at)\b", re.I)


def _hhmm(h: str, m: str | None, ampm: str | None) -> str:
    hh, mm = int(h), int(m or 0)
    if ampm and ampm.lower() == "pm" and hh < 12:
        hh += 12
    if not ampm and hh < 8:              # "3:15" in a trading context means 15:15
        hh += 12
    return f"{hh:02d}:{mm:02d}"


TIME_RE = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?"


def find_time(lead: str, low: str) -> str | None:
    """The first clock time after `lead` (e.g. "at", "exit at"): needs minutes (9:20, 15.15) or am/pm (3 pm)."""
    for m in re.finditer(lead + r"\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?(?![\d.%])", low):
        if m.group(2) or m.group(3):
            hh = int(m.group(1))
            if hh <= 23 and (not m.group(2) or int(m.group(2)) < 60):
                return _hhmm(m.group(1), m.group(2), m.group(3))
    return None


def symbol_of(text: str) -> str | None:
    low = text.lower()
    for k, v in SYMBOLS:
        if k in low:
            return v
    return None


def is_intraday(text: str) -> bool:
    return bool(symbol_of(text)) and bool(INTRADAY_CUES.search(text))


def _num(pat: str, low: str) -> float | None:
    m = re.search(pat, low)
    return float(m.group(1)) if m else None


def parse(text: str) -> tuple[Rule, list[str]]:
    """Read an intraday request into a Rule; notes explain the assumptions made."""
    low = text.lower().replace("’", "'")
    notes: list[str] = []
    r = Rule(symbol=symbol_of(text) or "NIFTY")

    # ---- direction
    if re.search(r"\b(with the gap|gap direction|direction of the gap)\b", low):
        r.side = "gap"
    elif re.search(r"\b(with the (?:day'?s )?(?:trend|move)|direction of the day)\b", low):
        r.side = "trend"
    elif re.search(r"\b(both (?:sides|directions|ways)|either (?:side|direction|way)|long (?:and|or|/) short|"
                   r"breakout either)\b", low):
        r.side = "both"
    elif re.search(r"\b(short|sell|puts?|bearish)\b", low) and not re.search(r"\b(buy|long|calls?|bullish)\b", low):
        r.side = "short"
    elif re.search(r"\b(buy|long|calls?|bullish)\b", low):
        r.side = "long"
    else:
        r.side = "both"

    # ---- entry event
    m_orb = re.search(r"(?:opening range|orb)\D{0,25}?(\d+)\s*(?:-|\s)?min", low) or \
        re.search(r"(\d+)\s*(?:-|\s)?min(?:ute)?s?\s*(?:opening range|orb)", low)
    m_ema = re.search(r"ema\s*\(?(\d+)\s*[/,&]\s*(\d+)", low) or re.search(r"(\d+)\s*/\s*(\d+)\s*ema", low)
    m_rsi = re.search(r"rsi\s*\(?(\d+)?", low)
    tf = _num(r"(?:on|using)\s*(?:the\s*)?(\d+)\s*[- ]?min", low) or 5
    at = find_time(r"(?:\bat|\bfrom|\benter(?:ing)? at)", low)
    if m_orb or re.search(r"\b(opening range|orb)\b", low):
        r.entry = {"type": "orb", "minutes": int(m_orb.group(1)) if m_orb else 15}
        if not m_orb:
            notes.append("Opening range taken as the first 15 minutes.")
    elif re.search(r"previous day'?s? (?:high|low)|\bpdh\b|\bpdl\b|yesterday'?s? (?:high|low)", low):
        r.entry = {"type": "prev_day"}
    elif re.search(r"\bcpr\b", low) and re.search(r"break|cross|above|below", low):
        r.entry = {"type": "cpr"}
    elif m_ema:
        r.entry = {"type": "ema_cross", "fast": int(m_ema.group(1)), "slow": int(m_ema.group(2)), "tf": int(tf)}
    elif m_rsi:
        lo = _num(r"(?:below|under|<)\s*(\d+)", low) or 30
        hi = _num(r"(?:above|over|>)\s*(\d+)", low) or 70
        r.entry = {"type": "rsi", "period": int(m_rsi.group(1) or 14), "tf": int(tf), "lower": lo, "upper": hi}
    elif at:
        r.entry = {"type": "time", "at": at}
    else:
        r.entry = {"type": "time", "at": "09:20"}
        notes.append("No entry rule found, so the trade is entered at 09:20.")
    if r.entry["type"] == "time" and r.side == "both":
        r.side = "long"
        notes.append("A timed entry needs a direction; taken as buy (say 'sell' or 'with the gap').")

    # ---- exits (points)
    pts = r"(\d+(?:\.\d+)?)\s*(?:points?|pts|pt)?"
    r.stop_pts = _num(r"(?:stop(?:[- ]?loss)?|\bsl\b)\s*(?:of|at|=|:)?\s*" + pts, low)
    r.target_pts = _num(r"(?:target|\btp\b|take[- ]?profit|profit)\s*(?:of|at|=|:)?\s*" + pts, low)
    r.trail_pts = _num(r"trail(?:ing)?(?:[- ]?stop)?\s*(?:of|at|=|:|by)?\s*" + pts, low)
    if r.trail_pts:
        r.trail_after_pts = _num(r"(?:after|once (?:up|in profit))\s*(\d+)\s*(?:points?|pts)", low)
    r.exit_time = find_time(r"(?:\bexit|\bsquare[- ]?off|\bclose|\bsell|\bcover|\bout|\bbook)(?:\s*it)?(?:\s*(?:at|by|before))?", low)
    if r.entry["type"] == "time" and r.exit_time and r.exit_time <= r.entry.get("at", "00:00"):
        r.exit_time = None
    mt = re.search(r"(?:max(?:imum)?|up to|at most)\s*(\d+)\s*trades?", low) or re.search(r"(\d+)\s*trades? (?:a|per) day", low)
    if mt:
        r.max_trades = int(mt.group(1))
    elif re.search(r"one trade (?:a|per) day|once a day", low):
        r.max_trades = 1
    r.day_loss_pts = _num(r"(?:daily|day) (?:loss|stop)\s*(?:cap|limit|of)?\s*" + pts, low)
    lots = _num(r"(\d+)\s*lots?", low)
    if lots:
        r.lots = int(lots)

    # ---- filters
    g = re.search(r"gap(?:s)?[- ]?(up|down)\s*(?:of\s*)?(?:(?:more than|above|over|>|at least)\s*)?(\d+(?:\.\d+)?)?\s*%?", low)
    if g:
        size = float(g.group(2)) if g.group(2) else 0.0
        if g.group(1) == "up":
            r.filters.append({"type": "gap", "min": size, "with_side": False})
        else:
            r.filters.append({"type": "gap", "max": -size, "with_side": False})
    elif re.search(r"with the gap|gap direction", low):
        pass
    days = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}
    wd = [v for k, v in days.items() if re.search(rf"\b{k}s?\b", low)]
    if wd:
        r.filters.append({"type": "weekday", "days": wd})
    v_lo = _num(r"vix\s*(?:is\s*)?(?:below|under|<)\s*(\d+(?:\.\d+)?)", low)
    v_hi = _num(r"vix\s*(?:is\s*)?(?:above|over|>)\s*(\d+(?:\.\d+)?)", low)
    if v_lo or v_hi:
        r.filters.append({"type": "vix", "max": v_lo, "min": v_hi})
    w = re.search(r"(?:between|only between)\s+" + TIME_RE + r"\s*(?:and|to|-)\s*" + TIME_RE, low)
    if w and r.entry["type"] != "time":
        r.filters.append({"type": "window", "from": _hhmm(w.group(1), w.group(2), w.group(3)),
                          "to": _hhmm(w.group(4), w.group(5), w.group(6))})
    if re.search(r"outside (?:the )?cpr|not inside (?:the )?cpr", low):
        r.filters.append({"type": "cpr_position", "where": "outside"})
    elif re.search(r"inside (?:the )?cpr", low) and r.entry["type"] != "cpr":
        r.filters.append({"type": "cpr_position", "where": "inside"})

    if not r.stop_pts and not r.trail_pts:
        notes.append("No stop given: trades run to the exit time.")
    return r, notes
