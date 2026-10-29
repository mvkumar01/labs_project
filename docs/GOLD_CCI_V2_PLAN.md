# Gold CCI V2 — paper, daily net target 3%

Implementation date: 29 October 2026. Separate Alpha Labs key `gold_cci_v2`.

## Rules

- One lot of GOLD futures (100 quoted units / 1 kg), short only.
- Original V1 signal: CCI(14) on completed 5-minute candles turns above 100;
  RSI(5) on 15-minute candles is above 50; hourly DI− exceeds DI+ (length 7).
- Original next-minute entry, cooldown, 0.25% hard stop, stop to entry after
  one stop distance in favour (effective next bar), 3R per-trade cap and EOD flat.
- At the first entry, freeze daily capital as estimated NRML SELL margin ratio
  × entry price × quantity. Target is 3% of that frozen capital.
- Exit when cumulative realized net P&L plus the active trade's net liquidation
  P&L reaches the daily target; take no further entries for the session.
- Charges follow the existing paper ledger's buyback/sell-entry convention.
  Every round trip includes one tick of modeled slippage per side.
- Original entries are retained. The exploratory DI/Bollinger entry vetoes and
  Bollinger exit/re-entry overlays are not part of this version.

## Margin and persistence

The margin ratio is a clearly labelled estimate from a broker quote. It is not
contract notional, rounded planning capital, or an actual historical margin.
The quote timestamp/contract/rate and first-entry capital are stored for the
session. Replays and restarts reuse them; cache refreshes cannot rewrite the goal.

A new session needs a same-day quote. Missing, stale or invalid quotes block new
paper entries and display `blocked_margin`. The paper tracker can refresh the
existing read-only margin cache. It never submits an order.

V2 uses its own `gold_cci_v2_daily`, `gold_cci_v2_trades` and
`gold_cci_v2_signals` tables. V1 and the real-time GOLDM dry runner remain separate.
Closed trades drive realized metrics; open marks are shown separately.

## Start and delivery

Book start is 29 October 2026; no historical backfill is initiated. A past session
without a stored quote remains blocked rather than borrowing today's margin.
Recorded live sessions can be finalized after a restart using their frozen basis.

Alpha Labs has a separate V2 selector, overview tile, date-scoped common ledger
and daily target history. The existing PA paper loop calls V2 after V1, with an
isolated exception handler. No live-money runner is changed.

Deployment requires the code on PA, a web reload and the paper-loop restart.
Commit/push approval is governed by `policy.md`; implementation alone does not
mean the production loop is already running V2.

## Validation

The pure replay reproduces the explored original-entry 3% sample: 94 trades,
51 winning / 20 losing days, net approximately ₹10,66,172 on one GOLD lot.
That sample (March–September 2026) was used for rule research and is not forward
validation. Tests cover net-cost target levels, daily halts, partial candles,
frozen capital, restart determinism, missing margin and V1 ledger isolation.
