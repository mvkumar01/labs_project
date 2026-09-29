"""Angel symbol resolution must stay on the NIFTY index option.

2026-09-25 / 09-28: on the monthly expiry FINNIFTY lists the same date, strike and
type as NIFTY. The resolver took the first match -- FINNIFTY29SEP26..., lot 60 --
and Angel rejected every 65-qty entry (AB4014).
"""

from __future__ import annotations

import pytest

from live.brokers import angel


def row(name, symbol, token, strike, expiry="29SEP2026", lot="65", itype="OPTIDX"):
    return {"token": token, "symbol": symbol, "name": name, "expiry": expiry,
            "strike": f"{strike * 100}.000000", "lotsize": lot, "instrumenttype": itype,
            "exch_seg": "NFO", "tick_size": "5.000000"}


MASTER = [
    row("FINNIFTY", "FINNIFTY29SEP2623150PE", "63024", 23150, lot="60"),   # listed first
    row("NIFTY", "NIFTY29SEP2623150PE", "73909", 23150),
    row("NIFTY", "NIFTY29SEP2623150CE", "73908", 23150),
    row("NIFTY", "NIFTY06OCT2623150PE", "80001", 23150, expiry="06OCT2026"),
    row("NIFTY", "NIFTY29SEP2623150PE_FUT", "1", 23150, itype="FUTIDX"),
]


class Fake(angel.AngelAdapter):
    def __init__(self, master, search_rows=()):
        self._symbol_cache, self._token_cache = {}, {}
        self._master = master
        self._search_rows = list(search_rows)

        class Smart:
            def searchScrip(_self, exchange, symbol):
                return {"data": self._search_rows}

        self._smart = Smart()

    def _ensure_instrument_master(self):
        return self._master


def test_monthly_contract_resolves_to_nifty_not_finnifty():
    meta = Fake(MASTER)._resolve_symbol_meta("NIFTY26SEP23150PE")
    assert meta == {"symbol": "NIFTY29SEP2623150PE", "token": "73909", "lotsize": 65}


def test_october_weekly_contract_resolves_to_its_own_expiry():
    # Zerodha writes Oct/Nov/Dec weeklies as O/N/D: 26O06 = 6 Oct 2026.
    meta = Fake(MASTER)._resolve_symbol_meta("NIFTY26O0623150PE")
    assert meta["symbol"] == "NIFTY06OCT2623150PE" and meta["lotsize"] == 65


@pytest.mark.parametrize("code, expected", [
    ("26922", "22SEP2026"), ("26O06", "06OCT2026"), ("26N03", "03NOV2026"),
    ("26D29", "29DEC2026"), ("26SEP", "29SEP2026"), ("26OCT", "27OCT2026"),
])
def test_expiry_codes(code, expected):
    assert angel.AngelAdapter._angel_expiry_from_zerodha(code) == expected


def test_undecodable_expiry_is_refused_not_matched_to_any_contract():
    with pytest.raises(RuntimeError, match="cannot decode expiry"):
        Fake(MASTER)._resolve_symbol_meta("NIFTY26X0623150PE")


def test_exact_angel_symbol_keeps_its_lot():
    meta = Fake(MASTER)._resolve_symbol_meta("NIFTY29SEP2623150PE")
    assert (meta["token"], meta["lotsize"]) == ("73909", 65)


def test_search_fallback_never_takes_a_near_miss():
    adapter = Fake([], search_rows=[{"tradingsymbol": "FINNIFTY29SEP2623150PE",
                                     "symboltoken": "63024"}])
    with pytest.raises(RuntimeError, match="symboltoken not found"):
        adapter._resolve_symbol_meta("NIFTY26SEP23150PE")


def test_health_check_is_reused_briefly_and_failures_are_not(monkeypatch):
    calls = []

    class Smart:
        ok = True

        def rmsLimit(self):
            calls.append(1)
            return {"status": True, "data": {"net": 1}} if Smart.ok else {"status": False}

    adapter = Fake(MASTER)
    adapter._smart = Smart()
    clock = [100.0]
    monkeypatch.setattr(angel.time, "monotonic", lambda: clock[0])
    assert adapter.is_connected() and adapter.is_connected() and len(calls) == 1
    clock[0] += angel._HEALTH_TTL_S + 1                 # expired -> pinged again
    Smart.ok = False
    assert adapter.is_connected() is False and len(calls) == 2
    assert adapter.is_connected() is False and len(calls) == 3   # a failure is never cached


def test_entry_refused_when_qty_is_not_a_lot_multiple():
    with pytest.raises(RuntimeError, match="lot size mismatch"):
        angel._check_lot_multiple({"symbol": "FINNIFTY29SEP2623150PE", "lotsize": 60}, 65)
    angel._check_lot_multiple({"symbol": "NIFTY29SEP2623150PE", "lotsize": 65}, 130)
    angel._check_lot_multiple({"symbol": "X", "lotsize": None}, 65)     # unknown lot: broker decides
