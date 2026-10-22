# Proposer fixed and dynamic lot sizing

Implemented and tested on 22 October 2026. Vipin approved commit, push, PA deployment and the Proposer
runner restart in this chat. Deployment receipts are under ignored `storage/state/proposer_sizing_deploy/`.
Applies to Nifty Scalper V1 and all SENSEX Proposer presets. Alpha strategies and paper research books
retain their sizing rules. Existing connections default to Fixed; no account settings were changed.

## Configure

- **Fixed:** start with the selected lots, capped by the exchange freeze. A definitive broker size/funds
  rejection halves the entry (4 → 2 → 1). No additional balance read is made for sizing.
- **Dynamic:** selected lots become the maximum. Choose the largest affordable whole number of lots
  under that ceiling and the freeze limit. The strike-selection rule stays unchanged.

Dynamic quantity is calculated as:

```text
lots = min(configured lots, freeze-capped lots,
           floor(cached available funds / (lot size × entry price cap × 1.03)))
```

The price cap includes the existing IOC chase allowance. The extra 3% is funding headroom, not a
guarantee against broker rejection. The existing size-rejection fallback remains active in both modes.
The saved lot setting is never overwritten by a smaller actual entry.

## Timing and failure behavior

Each dynamic connection has a daemon worker that reads `adapter.available_funds()` in the background,
with 15 seconds between reads. Sizing on the entry path uses only a short memory lookup and arithmetic;
it never calls or waits for the funds API. Fixed mode starts no funds worker. Existing market quotes,
broker gates and order submission retain their normal timing.

Balance reads are timestamped when they start. Missing, invalid or >=60-second-old snapshots fall back
to Fixed sizing for that entry, including the broker-rejection halving rule. Dynamic remains selected
for future entries. No order waits for funds, and a balance arriving later never creates an extra order
for the same signal. A fresh balance showing one buffered lot is unaffordable still skips the entry.

Snapshots are invalidated before and after entry/exit attempts. Generation checks discard requests
that were in flight around an order, preventing a pre-order response from becoming a fresh balance
after funds have been used. Workers stop when a connection is disarmed, deleted, moved to another
runner, switched to Fixed, or outside the entry session. Exits do not depend on balance availability.

Dynamic also applies in DRY_RUN using a read-only broker balance; DRY_RUN never places orders or reserves
real funds. Fixed DRY_RUN retains its prior behavior.

## Compatibility and validation

The per-connection configuration key is `proposer_sizing_mode` (`fixed` or `dynamic`); no schema migration
is needed. Mode changes require a flat position. Configure rejects invalid modes and refuses Dynamic
until the runner publishes `proposer-dt25-live-v12`.

Validation after the missing/stale-funds Fixed fallback change: **127 focused tests passed**, including
**31 sizing cases**. The preceding full suite (before that fallback change and the last print case) returned
**849 passed, 7 failed, 2 errors**; all nine issues are the documented pre-existing exit-retry, order-proxy,
IOC-validity and tick-stop issues. Python compilation and `git diff --check` passed.

`tests/test_proposer_sizing.py` covers affordability, ceiling and buffer arithmetic, missing/stale-funds
Fixed fallback (including live 4 → 2 → 1 rejections), fresh insufficient funds,
account isolation, worker lifecycle, a deliberately blocked broker balance request, generation-based
invalidation, live broker fallback, NIFTY/SENSEX quantities, unchanged fixed sizing, exits, and Configure
validation. A local 10,000-iteration timing check measured about 3 microseconds for cached lookup plus
arithmetic; this is not a measurement or guarantee of total brokerage order latency.

Validation receipts are under ignored `storage/state/proposer-sizing-*.txt` and
`storage/state/proposer-sizing-timing.json`.

## Rollout after approval

Upload `live/proposer_sizing.py` before the runner and web modules that import it. Other runtime changes:
`live/proposer_runner.py`, `live/live_executor.py`, `labs/ui/live_routes.py`, `templates/live_configure.html`.
Recheck PA copies against the prior commit, preserve remote changes, and verify uploads. Reload the web
app and restart only Proposer task **274885**, outside 09:15–15:30 IST. Confirm ABI **v12** in its boot log.
The paper loop and NIFTY alpha runner need no restart. The user selects a sizing mode on Configure.
