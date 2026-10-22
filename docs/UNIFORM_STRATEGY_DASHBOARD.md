# Uniform strategy dashboard

Implemented for `/labs/live`. Vipin approved commit, push and PA deployment on 22 October 2026 after local validation.

## Scope

Every registered strategy has a shared performance panel. Existing strategy-specific tables,
research descriptions and controls remain inside an expandable section. Replay and real-time
Gold/Crude dry runs have separate panels and never share a P&L total.

- Summary: gross winning/losing P&L, gross total, costs, net after costs, trade and day win rates.
- Monthly results use exactly the selected strategy-date range; partial months stay partial.
- Trade ledger: date, exact contract when stored, IST entry/exit times, elapsed duration,
  direction, units, entry capital/margin, entry/exit prices, exit premium/value, spot points,
  recorded target, gross/net P&L, costs, net return on entry capital, exit rule and excursions.
- Rows per page: 10, 20, 25, 50, 100 (default), 150 or 200. Changing size resets page one,
  retains date bounds and does not truncate summaries or monthly totals.
- Straddles and iron flies count as one round trip; their individual legs remain in the
  original strategy detail table. Basket variants are separate reports.
- Risk Core and Crypto XS are daily allocation books rather than round-trip ledgers. Their
  shared panels show daily/monthly P&L and day win rate; trade-only fields are inapplicable.
- Overview filter scopes historical P&L and win rates. Today/position status stays explicitly
  current; detail links retain the selected dates. Dollar books remain USD.

## Accounting and dates

Closed, priced net results define win rates. Break-even trades and days stay in denominators.
Open marked results are separate. Missing data stays missing, with coverage counts; it is never
replaced by zero or a different contract. Known partial sums carry an incomplete-pricing warning.

Date membership follows the source ledger: intraday entry/strategy session, expiry/settlement
for expiry strangles, entry cohort for pairs, and UTC decision date for crypto. All actual trade
timestamps display IST, including a date note when the displayed date differs from its cohort.
Daily-only records do not imply that an unrecorded execution happened at midnight.

Long-option capital is entry premium times units. Futures use recorded margin/allocation;
notional exposure is not invented as margin. Portfolio/multi-leg allocated capital is used when
recorded. P&L percent is net divided by that capital, otherwise unavailable. Charges and explicit
slippage are separate; the source net result is authoritative.

## Excursions

A read-only button calculates the current page in batches of up to 20 trade IDs. Normal page
loads never scan historical market files. This endpoint does not fetch broker data, create
trades, change strategy state or rebuild ledgers.

- Held peak: favorable gross P&L while holding, measured against the original entry.
- Day peak: synchronized one-minute close/snapshot strategy P&L (realized plus open gross),
  not the sum of independent trade peaks.
- Post-exit peak/trough: hypothetical P&L if the original quantity had been held from the
  original entry through the remaining recorded session; not additional realized earnings.
- Futures use the exact contract's cached candles, excluding partial entry/exit bars.
- Options use the exact contract's recorded LTP snapshots from the canonical shared store.
- Multi-leg estimates combine synchronized exact-contract leg marks, never independent leg
  maxima. Estimates exclude charges and cannot establish tick-level extrema. Coverage/through
  time appears on hover. Multi-session and missing-contract histories remain unavailable where
  a defensible series does not exist. No underlying/other-contract proxy.

Gold and Crude legacy dry-run queries now apply the date filter to both summaries and rows;
the former implicit latest-60-trades summary cap has been removed.

## Verification / rollout

Focused tests cover filter boundaries, empty ranges, row counts, full-range pagination totals,
closed/open/unpriced separation, break-even denominators, multi-leg grain, crypto equity/FX,
source adapters, actual-time display, and excursion direction/timestamp boundaries.
Visual review uses explicitly labeled isolated test data, in both themes and a 390px viewport.
No production database export is required. Deploy only the dashboard files, then reload the
web app. No collector, live runner or paper loop restart is needed.
