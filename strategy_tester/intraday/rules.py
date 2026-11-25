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
    day_loss_rs: float | None = None      # stop for the day once the day's net P&L is this many rupees down
    day_target_rs: float | None = None    # ... or this many rupees up
    max_losses: int | None = None         # ... or after this many losing trades
    # Trade an option instead of the index (None = index points). Keys: mode buy|sell, strike atm|otm|itm|delta|premium,
    # value (steps / delta / rupees), expiry nearest|next, roll_on_expiry, expiry_day only|skip|None,
    # premium exits stop_pct / target_pct / stop_pts / target_pts / trail_pts.
    option: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in (d or {}).items() if k in keys})


SYMBOLS = [("bank nifty", "BANKNIFTY"), ("banknifty", "BANKNIFTY"), ("nifty bank", "BANKNIFTY"), ("sensex", "SENSEX"),
           ("nifty 50", "NIFTY"), ("nifty50", "NIFTY"), ("nifty", "NIFTY"), ("^nsebank", "BANKNIFTY"), ("^bsesn", "SENSEX"),
           ("^nsei", "NIFTY")]
OPTION_CUES = re.compile(r"\b(options?|ce|pe|calls?|puts?|premium|atm|itm|otm|strikes?|delta|expiry)\b", re.I)
INTRADAY_CUES = re.compile(
    r"\b(intraday|intra-day|scalp\w*|opening range|\borb\b|gap[- ]?(?:up|down)|gaps? (?:up|down)|at \d{1,2}[:.]\d{2}|"
    r"options?|ce|pe|calls?|puts?|premium|atm|itm|otm|"
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

    is_option = bool(OPTION_CUES.search(low))
    sell_option = False
    if is_option:
        sell_option = bool(re.search(r"\b(writ(?:e|es|ing)|option sell\w*|sell(?:ing)? (?:\w+ ){0,2}options?)\b", low)) or (
            bool(re.search(r"\b(sell|short|selling)\b[^.;]{0,30}?\b(options?|calls?|puts?|ce|pe|premium|straddles?|strangles?)\b", low))
            and not re.search(r"\b(buy|buying|long)\b", low))
    has_call = bool(re.search(r"\b(calls?|ce)\b", low))
    has_put = bool(re.search(r"\b(puts?|pe)\b", low))

    # ---- direction
    if re.search(r"\b(with the gap|gap direction|direction of the gap)\b", low):
        r.side = "gap"
    elif re.search(r"\b(with the (?:day'?s )?(?:trend|move)|direction of the day)\b", low):
        r.side = "trend"
    elif is_option and re.search(r"\b(both (?:sides|directions|ways)|either (?:side|direction|way))\b", low):
        r.side = "both"
    elif is_option and has_call != has_put:
        # buying calls / selling puts = up view; buying puts / selling calls = down view
        r.side = "long" if has_call != sell_option else "short"
    elif is_option:
        r.side = "long" if re.search(r"\b(bullish|upside only|long only)\b", low) else (
            "short" if re.search(r"\b(bearish|downside only|short only)\b", low) else "both")
    elif re.search(r"\b(with the gap|gap direction|direction of the gap)\b", low):
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

    if is_option:
        _option_spec(low, r, notes, sell_option)
    if not r.stop_pts and not r.trail_pts and not (r.option and (r.option.get("stop_pct") or r.option.get("stop_pts")
                                                                  or r.option.get("trail_pct") or r.option.get("trail_pts"))):
        notes.append("No stop given: trades run to the exit time.")
    return r, notes


def _option_spec(low: str, r: Rule, notes: list[str], sell: bool) -> None:
    """Option side of the rule: strike, expiry, premium exits (% or premium points), rupee day limits."""
    o: dict = {"mode": "sell" if sell else "buy", "strike": "atm", "value": None, "expiry": "nearest"}
    m = re.search(r"delta\s*(?:of|=|~|around|near|:)?\s*(0?\.\d+|\d+(?:\.\d+)?)", low)
    m_n = re.search(r"(\d+)\s*(?:strikes?|steps?)?\s*(otm|itm)\b", low)
    m_n2 = re.search(r"\b(otm|itm)\s*(\d+)\b(?!\s*[:.%])", low)
    m_p = re.search(r"premium\s*(?:of|around|near|about|~|close to)?\s*(?:rs\.?|inr)?\s*(\d+)(?!\s*(?:%|points?|pts|\d))", low)
    if m:
        v = float(m.group(1))
        o.update(strike="delta", value=v / 100 if v > 1 else v)
    elif m_n:
        o.update(strike=m_n.group(2), value=int(m_n.group(1)))
    elif m_n2:
        o.update(strike=m_n2.group(1), value=int(m_n2.group(2)))
    elif re.search(r"\b(otm|itm)\b", low):
        o.update(strike=re.search(r"\b(otm|itm)\b", low).group(1), value=1)
    elif m_p:
        o.update(strike="premium", value=float(m_p.group(1)))
    elif not re.search(r"\batm\b", low):
        notes.append("No strike given: the at-the-money option is used.")
    if re.search(r"next (?:week'?s? )?expiry|next week(?:ly)?|second expiry|far expiry", low):
        o["expiry"] = "next"
    if re.search(r"roll (?:over )?on expiry|next expiry on expiry day", low):
        o["roll_on_expiry"] = True
    if re.search(r"only on expiry days?|on expiry days? only|expiry days? only|\b0 ?dte\b", low):
        o["expiry_day"] = "only"
    elif re.search(r"(?:skip|avoid|no trades? on|not on|except(?: on)?)\s+(?:the\s+)?expiry days?", low):
        o["expiry_day"] = "skip"
    pct = r"(\d+(?:\.\d+)?)\s*%"
    for key, lead in (("stop", r"(?:stop(?:[- ]?loss)?|\bsl\b)"), ("target", r"(?:target|\btp\b|take[- ]?profit|profit)"),
                      ("trail", r"trail(?:ing)?(?:[- ]?stop)?")):
        v = _num(lead + r"\s*(?:of|at|=|:|by)?\s*" + pct, low) or _num(pct + r"\s*(?:premium\s*)?" + lead, low)
        if v:
            o[key + "_pct"] = v
            setattr(r, key + "_pts", None)
    index_pts = re.search(r"(?:index|spot|underlying|nifty|sensex|bank ?nifty)\s*points|points? (?:on|in) (?:the )?"
                          r"(?:index|spot|underlying)", low)
    if not index_pts:
        moved = False
        for key in ("stop", "target", "trail"):
            if getattr(r, key + "_pts") and not o.get(key + "_pct"):
                o[key + "_pts"] = getattr(r, key + "_pts")
                setattr(r, key + "_pts", None)
                moved = True
        if moved:
            notes.append("Stops / targets read as option premium points (say 'index points' to measure them on the index).")
    rs = r"(?:rs\.?|inr)\s*([\d,]+)"

    def rupees(pat):
        mm = re.search(pat, low)
        return float(mm.group(1).replace(",", "")) if mm else None

    r.day_loss_rs = rupees(r"(?:daily|day'?s?|per day|a day)\s*(?:max(?:imum)?\s*)?(?:loss|stop)\s*(?:cap|limit|of)?\s*" + rs) or \
        rupees(r"(?:stop (?:trading )?(?:for the day )?(?:after|at|once)|max(?:imum)? loss(?: of)?)\s*(?:a\s*)?" + rs)
    r.day_target_rs = rupees(r"(?:daily|day'?s?|per day|a day)\s*(?:profit\s*)?target\s*(?:of)?\s*" + rs)
    if r.day_loss_pts and not r.day_loss_rs:
        r.day_loss_rs, r.day_loss_pts = r.day_loss_pts, None
        notes.append(f"Daily loss limit read as Rs {r.day_loss_rs:,.0f}.")
    words = {"one": 1, "two": 2, "three": 3, "four": 4}
    ml = re.search(r"(?:after|max(?:imum)?)\s*(\d+|one|two|three|four)\s*(?:losing trades?|losses|losers|stop[- ]?outs?)", low)
    if ml:
        r.max_losses = int(words.get(ml.group(1), ml.group(1)))
    r.option = o
