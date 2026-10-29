# Gold CCI V2 in live trading

The `/live/configure` selector offers **Gold CCI V2** on Angel One and Zerodha connections.
Adding or selecting the strategy does not arm it. Save while disarmed and flat;
use the existing dry-run and live arming controls. MCX trading must be enabled
on the selected broker account. Angel One is the requested execution broker;
the shared Zerodha session supplies market data only when Angel is selected.
Angel GOLD symbols are matched to Kite contracts by exact exchange expiry and
validated MCX FUTCOM token, lot size and price tick. Orders use Angel's
CARRYFORWARD product (equivalent to NRML); margin comes from Angel's margin API.

This trades **GOLD futures, 1 kg per lot**, not the separate GOLDM dry runner.
Broker quantity is whole contracts; P&L and displayed ledger quantity use 100
quoted units per contract. The nearest contract expiring after today's session
is selected. NRML avoids an earlier broker MIS square-off; the bot attempts to
close at 23:29 IST. Exits and stops require the runner, broker and network to work.

Entries use V1's completed-candle signal, plus the selected V2 gate: no short
when completed 15-minute DI(14)+ > DI(14)− or the latest completed 15-minute candle
crosses upward through SMA20. Retain the 0.25% hard stop, move to entry after
1R (next minute), 3R trade target and 3% daily net target. There is no Bollinger
exit or re-entry rule. A signal more than 20 seconds late is skipped.

Lots are fixed. The adapter prepares an NRML SELL margin quote before signal
evaluation and refreshes it every five minutes or when lot size changes. Missing
quotes block entry. The first actual fill freezes the daily margin capital and
3% target; a partial fill scales that margin to filled contracts. The target
uses cumulative realized net plus the held short's net liquidation P&L. Actual
fills and MCX charges determine net results; no modeled slippage is deducted
from live fills. Live and dry day buckets are separate.

All intents pass through `live_executor.place_idempotent`, the existing mode,
account, daily-loss, proxy and ABI gates, and the order-only transport. A special
GOLD adapter sends SELL entries and BUY exits, both IOC limits. A buyback requires
a matching broker NRML short and cannot exceed its quantity. Existing positions
block entries. Broker/owned-position differences block automated management for
reconciliation. Manual orders in the same GOLD contract should be avoided.

`live_gold_v2_state` stores each connection's pending intent, owned position and
daily frozen target. Intents are committed before submission; a pending intent
keeps its key across restarts. ACK is not a fill. Terminal partial fills update
only the actual quantity. Uncertain outcomes remain blocked, with a reconciliation
message. Fill accounting, daily totals and state updates commit together; the
standard live position/trade displays use a futures-aware P&L calculation.

The existing `pa_live_runner.py` task dispatches Gold separately from NIFTY.
Deploy new modules and shared changes, restart that task, verify its fresh boot,
then reload the web app. Do not alter existing selections or arming state.
The paper V2 ledger and original V1 remain independent.
