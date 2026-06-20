"""Canonical option-expiry selection for every Labs execution path."""
from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Iterable

_WEEKLY_RE = re.compile(r"^(\d{2})(\d)(\d{2})$")
_MONTHLY_RE = re.compile(r"^(\d{2})([A-Z]{3})$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def expiry_code_from_symbol(symbol: str, underlying: str) -> str | None:
    """Return a Kite expiry code from an option tradingsymbol."""
    text = str(symbol).strip().upper()
    prefix = str(underlying).strip().upper()
    if not text.startswith(prefix):
        return None
    suffix = text[len(prefix):]
    match = re.match(r"^(\d{2}(?:\d\d{2}|[A-Z]{3}))", suffix)
    return match.group(1) if match else None


def is_monthly_expiry(code: object) -> bool:
    return bool(_MONTHLY_RE.fullmatch(str(code).strip().upper()))


def expiry_sort_date(code: object) -> date | None:
    """Decode an expiry code into a chronological ordering date.

    Monthly codes sort at calendar month-end. This is only an ordering key;
    the instrument master/captured chain remains authoritative for whether the
    contract is actually listed on the session.
    """
    text = str(code).strip().upper()
    if _ISO_RE.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    match = _WEEKLY_RE.fullmatch(text)
    if match:
        yy, month, day = match.groups()
        try:
            return date(2000 + int(yy), int(month), int(day))
        except ValueError:
            return None
    match = _MONTHLY_RE.fullmatch(text)
    if match:
        yy, month_name = match.groups()
        month = _MONTHS.get(month_name)
        if month is None:
            return None
        year = 2000 + int(yy)
        return date(year, month, calendar.monthrange(year, month)[1])
    return None


def select_expiry_code(
    expiries: Iterable[object],
    trade_date: str | date,
    mode: str = "nearest_weekly",
) -> str | None:
    """Select nearest, next, or monthly expiry and fail closed if unavailable."""
    session = date.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date
    normalized = str(mode or "nearest_weekly").strip().lower()
    if normalized not in {"nearest_weekly", "next_weekly", "monthly"}:
        return None

    decoded: list[tuple[date, str]] = []
    seen: set[str] = set()
    for raw in expiries:
        code = str(raw).strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        sort_date = expiry_sort_date(code)
        if sort_date is None:
            continue
        if is_monthly_expiry(code):
            if (sort_date.year, sort_date.month) < (session.year, session.month):
                continue
        elif sort_date < session:
            continue
        decoded.append((sort_date, code))

    if normalized == "monthly":
        decoded = [item for item in decoded if is_monthly_expiry(item[1])]
        index = 0
    else:
        index = 0 if normalized == "nearest_weekly" else 1
    decoded.sort(key=lambda item: (item[0], item[1]))
    return decoded[index][1] if len(decoded) > index else None


def select_symbol_for_expiry(
    symbols: Iterable[str],
    underlying: str,
    trade_date: str | date,
    mode: str = "nearest_weekly",
) -> str | None:
    """Select one symbol after applying the requested expiry mode."""
    by_expiry: dict[str, list[str]] = {}
    for symbol in symbols:
        code = expiry_code_from_symbol(symbol, underlying)
        if code:
            by_expiry.setdefault(code, []).append(str(symbol))
    selected = select_expiry_code(by_expiry, trade_date, mode)
    if selected is None:
        return None
    return sorted(by_expiry[selected])[0]
