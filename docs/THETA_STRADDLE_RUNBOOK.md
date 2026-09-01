# NIFTY 09:20 Theta Straddle Paper Book

## Rules

- Underlying: NIFTY.
- Entry: first common executable CE and PE quote from 09:20 to 09:25 IST.
- Strike: ATM rounded to the nearest 50 points using entry NIFTY spot.
- Expiry: nearest available weekly contract.
- Position: sell one 65-unit ATM CE and one 65-unit ATM PE.
- Exit: first common executable quote from 15:15 to 15:20 IST.
- Pricing: opening sells use bid; closing buys use ask.
- Scope: paper only. The tracker has no broker order call.

## Capital And P&L

Archived quote files do not contain historical broker SPAN snapshots. The
dashboard therefore labels capital as an estimate and uses a stable comparison
method:

```
estimated capital = entry NIFTY spot * 65 * 10%
```

Opening premium credit is stored separately. It is not subtracted from the
capital estimate. Gross P&L is sell premium less cover premium for both legs.
Net P&L deducts brokerage, STT, exchange charges, SEBI charges, stamp duty, and
GST. From 1 April 2026, the short-option STT rate is 0.15% of opening sell
premium.

## Operations

The existing `pa_paper_tracker_loop.py` updates the book during market hours.
No additional always-on task is needed. After deployment, restart that paper
tracker once so it imports the new module.

The `/labs/live?tab=theta_straddle` tab contains a bounded, idempotent backfill
button. It reads the shared NIFTY options archive from 1 June 2026 onward and
writes only `theta_straddle_daily` and `theta_straddle_trades`.
