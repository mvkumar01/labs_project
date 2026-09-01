"""Indian NIFTY-options round-trip charges (discount-broker model).

Used by the paper-strategy tracker so the displayed net PnL reflects real
trading costs. Rates are the standard NSE F&O options schedule (Angel/Zerodha
discount model) as constants — adjust here if the schedule changes.

A position is BUY-to-open then SELL-to-close (the strategy is long naked
options). buy_value = entry_premium * qty; sell_value = exit_premium * qty.
"""
from __future__ import annotations

# ── Rate schedule (NSE options) ──────────────────────────────────────────────
BROKERAGE_PER_LEG = 20.0        # flat ₹20 per executed order (2 legs/round-trip)
STT_SELL = 0.001                # 0.10% of premium, SELL side only (since Oct-2024)
EXCH_TXN = 0.0003503            # NSE F&O txn charge, % of premium turnover (both legs)
SEBI = 0.000001                 # ₹10 per crore of turnover (both legs)
STAMP_BUY = 0.00003             # 0.003% of premium value, BUY side only
GST = 0.18                      # 18% on (brokerage + txn + SEBI)


def round_trip_charges(entry_premium: float, exit_premium: float, qty: int) -> dict:
    """Return the charge breakdown + total ₹ for one long-option round trip."""
    try:
        buy_value = float(entry_premium) * int(qty)
        sell_value = float(exit_premium) * int(qty)
    except (TypeError, ValueError):
        return {"total": 0.0}
    turnover = buy_value + sell_value
    brokerage = BROKERAGE_PER_LEG * 2
    stt = STT_SELL * sell_value
    txn = EXCH_TXN * turnover
    sebi = SEBI * turnover
    stamp = STAMP_BUY * buy_value
    gst = GST * (brokerage + txn + sebi)
    total = brokerage + stt + txn + sebi + stamp + gst
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exch_txn": round(txn, 2),
        "sebi": round(sebi, 4),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        # Preserve the unrounded value for ledgers whose research benchmark
        # aggregates charges before applying paise rounding. Existing callers
        # continue to use the rounded ``total`` field unchanged.
        "raw_total": total,
        "total": round(total, 2),
    }


def short_option_round_trip_charges(
    entry_sell_premium: float, exit_buy_premium: float, qty: int
) -> dict:
    """Current NSE charge model for one short-option intraday round trip.

    STT is charged on the opening sell premium. The 0.15% rate applies from
    2026-04-01; it is intentionally isolated from the legacy long-option
    benchmark above so historical Alpha ledgers do not change underneath us.
    """
    sell_value = float(entry_sell_premium) * int(qty)
    buy_value = float(exit_buy_premium) * int(qty)
    turnover = sell_value + buy_value
    brokerage = BROKERAGE_PER_LEG * 2
    stt = 0.0015 * sell_value
    txn = EXCH_TXN * turnover
    sebi = SEBI * turnover
    stamp = STAMP_BUY * buy_value
    gst = GST * (brokerage + txn + sebi)
    total = brokerage + stt + txn + sebi + stamp + gst
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exch_txn": round(txn, 2),
        "sebi": round(sebi, 4),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "raw_total": total,
        "total": round(total, 2),
    }


# ── SENSEX (BSE) options round-trip charges ──────────────────────────────────
# BSE F&O schedule differs from NSE: STT 0.15% on sell, txn 0.0325% turnover,
# lot size 20. Supplied by the operator (2026-06-29).
SENSEX_LOT_SIZE = 20
SENSEX_STT_SELL = 0.0015         # 0.15% of premium, SELL side only
SENSEX_EXCH_TXN = 0.000325       # BSE option txn 0.0325% of premium turnover


def sensex_round_trip_charges(entry_premium: float, exit_premium: float, qty: int) -> dict:
    """SENSEX (BSE) long-option round-trip charges. qty = lots * 20.

    Mirrors round_trip_charges but with the BSE rate schedule (higher STT and a
    different exchange txn rate). Brokerage/SEBI/stamp/GST are the same.
    """
    try:
        buy_value = float(entry_premium) * int(qty)
        sell_value = float(exit_premium) * int(qty)
    except (TypeError, ValueError):
        return {"total": 0.0, "raw_total": 0.0}
    turnover = buy_value + sell_value
    brokerage = BROKERAGE_PER_LEG * 2
    stt = SENSEX_STT_SELL * sell_value
    txn = SENSEX_EXCH_TXN * turnover
    sebi = SEBI * turnover
    stamp = STAMP_BUY * buy_value
    gst = GST * (brokerage + txn + sebi)
    total = brokerage + stt + txn + sebi + stamp + gst
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exch_txn": round(txn, 2),
        "sebi": round(sebi, 4),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "raw_total": total,
        "total": round(total, 2),
    }
