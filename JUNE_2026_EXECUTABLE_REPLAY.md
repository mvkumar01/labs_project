# June 2026 executable option replay notes

Date: 2026-06-23

## Question

Test whether selecting deeper NIFTY executable contracts by absolute delta improves
the Alpha v2.11 champion option P&L, and add a separate paper view where the same
NIFTY v2.11 signal timestamps are executed in SENSEX options.

## NIFTY executable replay

Scope: June 2026 sessions covered by the v2.11 paper range map through
2026-06-19. Signal side and entry/exit timestamps are unchanged across variants.
Entry uses exact option ask; exit uses exact option bid. No LTP fallback.

| Variant | Trades | Gross Rs | Charges Rs | Net Rs | Avg abs delta |
|---|---:|---:|---:|---:|---:|
| ATM | 16 | -1,755.00 | 1,017.63 | -2,772.63 | 0.50 |
| Delta >= 0.60 | 16 | -3,617.25 | 1,137.66 | -4,754.91 | 0.63 |
| Delta >= 0.80 | 16 | -5,892.25 | 1,434.57 | -7,326.82 | 0.82 |
| ITM 200 | 16 | -1,725.75 | 1,257.48 | -2,983.23 | 0.73 |

Conclusion: delta >= 0.60 and delta >= 0.80 do not improve the champion. They
materially worsen June executable option P&L versus both ATM and ITM 200. ATM is
only slightly better than ITM 200 in this window and is not a robust upgrade.

## SENSEX execution of NIFTY v2.11 signals

Scope: same NIFTY v2.11 signal timestamps from the PythonAnywhere canonical
Labs replay, executed as one nearest-expiry ATM SENSEX option lot. CALL signal
buys SENSEX CALL; PUT signal buys SENSEX PUT. Entry requires exact ask and exit
requires exact bid. No LTP fallback.

Backfill expectation for the new `sensex_v211_*` paper tables on PA:

| Days | Trades | Gross option Rs | Unavailable trades |
|---:|---:|---:|---:|
| 14 | 16 | -6,730.00 | 0 |

Note: an earlier local cross-replay produced `-725.00`, but the PA deployment
guard rejected it. The mismatch came from stale local signal timestamps on
2026-06-15. PA's canonical v2.11 replay for that date enters at 10:55 and exits
at 12:40; the SENSEX 76800 CE exact ask/bid execution is -3,157.00 for that
session.
