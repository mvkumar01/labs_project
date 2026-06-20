from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

import pandas as pd

from collector.instruments import build_option_symbols
from collector.run_collector import _market_open
from collector.spot_collector import MarketSessionClosed, collect_spot
from labs.engine.backtest import _option_session_quality, _resolve_contract_from_options
from labs.engine.strategy_runner import _market_open as _runner_market_open
from market_data.expiry import select_expiry_code, select_symbol_for_expiry


class ExpirySelectionTests(unittest.TestCase):
    def test_month_end_week_selects_monthly_before_next_week(self):
        expiries = ["26MAY", "26602"]
        self.assertEqual(select_expiry_code(expiries, "2026-05-25", "nearest_weekly"), "26MAY")
        self.assertEqual(select_expiry_code(expiries, "2026-05-25", "next_weekly"), "26602")
        self.assertEqual(select_expiry_code(expiries, "2026-05-25", "monthly"), "26MAY")

    def test_june_rollover_does_not_jump_to_july(self):
        expiries = ["26JUN", "26707"]
        self.assertEqual(select_expiry_code(expiries, "2026-06-24", "nearest_weekly"), "26JUN")
        self.assertEqual(select_expiry_code(expiries, "2026-06-24", "next_weekly"), "26707")

    def test_unavailable_monthly_fails_closed(self):
        self.assertIsNone(select_expiry_code(["26623", "26707"], "2026-06-17", "monthly"))

    def test_symbol_selection_uses_mode_not_symbol_length(self):
        symbols = ["NIFTY2660225000CE", "NIFTY26MAY25000CE"]
        self.assertEqual(
            select_symbol_for_expiry(symbols, "NIFTY", "2026-05-25", "nearest_weekly"),
            "NIFTY26MAY25000CE",
        )

    def test_backtest_resolver_honors_each_mode(self):
        rows = pd.DataFrame([
            {"timestamp": "2026-05-25 09:20:00", "tradingsymbol": "NIFTY26MAY25000CE", "strike": 25000, "expiry": "26MAY", "ltp": 120},
            {"timestamp": "2026-05-25 09:20:00", "tradingsymbol": "NIFTY2660225000CE", "strike": 25000, "expiry": "26602", "ltp": 180},
        ])
        bot = {"underlying": "NIFTY"}
        base = {"strike_mode": "atm", "strike_offset_pts": 0}
        nearest = _resolve_contract_from_options(bot, {**base, "expiry_mode": "nearest_weekly"}, "CE", 25000, rows, "2026-05-25")
        next_expiry = _resolve_contract_from_options(bot, {**base, "expiry_mode": "next_weekly"}, "CE", 25000, rows, "2026-05-25")
        monthly = _resolve_contract_from_options(bot, {**base, "expiry_mode": "monthly"}, "CE", 25000, rows, "2026-05-25")
        self.assertEqual(nearest["expiry"], "26MAY")
        self.assertEqual(next_expiry["expiry"], "26602")
        self.assertEqual(monthly["expiry"], "26MAY")

    def test_collector_includes_nearest_monthly_as_third_expiry(self):
        today = date.today()
        instruments = []
        for expiry, code in [
            (today + timedelta(days=7), "99101"),
            (today + timedelta(days=14), "99108"),
            (today + timedelta(days=40), "99DEC"),
        ]:
            for side in ("CE", "PE"):
                instruments.append({
                    "name": "NIFTY",
                    "instrument_type": side,
                    "expiry": expiry,
                    "tradingsymbol": f"NIFTY{code}20000{side}",
                    "strike": 20000,
                })

        class Kite:
            def instruments(self, _exchange):
                return instruments

        symbols = build_option_symbols(Kite(), "NIFTY", 20000)
        self.assertEqual(len(symbols), 6)
        self.assertTrue(any("99DEC" in symbol for symbol in symbols))


def _session_frame(trade_date: str, start: str = "09:15", periods: int = 376, frozen: bool = False) -> pd.DataFrame:
    timestamps = pd.date_range(f"{trade_date} {start}", periods=periods, freq="1min")
    rows = []
    for i, ts in enumerate(timestamps):
        for expiry, symbol in [("26623", "NIFTY2662325000CE"), ("26JUN", "NIFTY26JUN25000CE")]:
            rows.append({
                "timestamp": ts,
                "tradingsymbol": symbol,
                "expiry": expiry,
                "ltp": 100 if frozen else 100 + i / 10,
                "spot": 25000 if frozen else 25000 + i / 10,
            })
    return pd.DataFrame(rows)


class SessionSafetyTests(unittest.TestCase):
    def test_complete_weekday_session_is_valid(self):
        quality = _option_session_quality(_session_frame("2026-06-19"), "2026-06-19")
        self.assertTrue(quality["valid"], quality)

    def test_weekend_frozen_capture_is_rejected(self):
        quality = _option_session_quality(_session_frame("2026-06-20", frozen=True), "2026-06-20")
        self.assertFalse(quality["valid"])
        self.assertIn("weekend capture", quality["reason"])
        self.assertIn("frozen spot series", quality["reason"])

    def test_partial_session_is_rejected(self):
        quality = _option_session_quality(
            _session_frame("2026-06-01", start="10:37", periods=293),
            "2026-06-01",
        )
        self.assertFalse(quality["valid"])
        self.assertIn("late start", quality["reason"])
        self.assertIn("only 293", quality["reason"])

    def test_collector_never_opens_on_weekend(self):
        self.assertFalse(_market_open(datetime(2026, 6, 20, 10, 0)))
        self.assertTrue(_market_open(datetime(2026, 6, 19, 10, 0)))
        self.assertFalse(_runner_market_open(datetime(2026, 6, 20, 10, 0)))
        self.assertTrue(_runner_market_open(datetime(2026, 6, 19, 10, 0)))

    def test_stale_quote_blocks_holiday_capture(self):
        class Kite:
            def historical_data(self, **_kwargs):
                return []

            def quote(self, symbols):
                return {
                    symbols[0]: {
                        "last_price": 25000,
                        "timestamp": datetime(2026, 6, 18, 15, 30),
                    }
                }

        with self.assertRaises(MarketSessionClosed):
            collect_spot(Kite(), "NIFTY", "2026-06-19", datetime(2026, 6, 19, 9, 15))


if __name__ == "__main__":
    unittest.main()
