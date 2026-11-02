# CRUDEOILM Combo in live trading

The `/live/configure` selector offers **CRUDEOILM Combo** on Angel One connections. Selecting it does not arm it.
Save while disarmed and flat, then use the existing dry-run and live arming controls. One connection runs one
strategy, so selecting this replaces what that connection ran before.

**Instrument.** MCX CRUDEOILM futures, 10 barrels per lot, nearest contract expiring after today. Order quantity is
barrels (lots x 10); the master row's lot size and tick are checked and a change stops orders. Product is
CARRYFORWARD: the 2026-10-09 probe found the same margin as INTRADAY, and it avoids a broker square-off before the
strategy's 23:29 close. Market data comes from Kite, orders and margin quotes from Angel.

**Entries.** The engine's six rules, on the same rolled series as the paper book. The runner reads the closed candle
5 s into the minute, because the dry runner's decisions at 0-2 s differed from paper on candles that were not final.
The first rule to fire takes the trade; a signal more than 25 s into the minute is skipped. Each entry is an IOC limit
two ticks through the touch. A failed margin or funds read blocks entries, and funds below the margin skip them.

**Exits.** The fill sets stop and target, on the Rs 1 tick. The stop rests at Angel as a stop-limit with its limit 20
points past the trigger. The bot takes the target and closes at 23:29. Before any exit by the bot the stop is
cancelled and seen dead, so two exits cannot both fill. If Angel refuses or kills the stop the position is closed at
once (`no_broker_stop`). If price runs 20 points past the trigger with the position still open the bot closes it
(`stop_missed`).

**Known exposures.**
- If the bot is down at 23:29 the position stays open overnight, and the day's stop has gone. The next session's
  first cycle closes it (`eod_missed`).
- Disarming with a position open leaves the stop resting but no target or close. Close such a position by hand.
- Manual orders in CRUDEOILM on this account block the bot.

**Reconciliation.** Intents are committed before they are sent, and a pending intent keeps its key across restarts. A
request with an unknown outcome, or a broker position that still differs from the bot's after 10 s, sets
`reconcile_blocked` and sends a Telegram alert. It is cleared by the configure page's broker-flat refresh.

**Not proven until the first real order.** Angel's handling of a STOPLOSS_LIMIT order on MCX, and whether its fills
and `netqty` are in barrels as its margin calculator is. A mismatch alerts at once and says when no stop is resting.
Watch the first live trade at the broker.

**Deployment.** `pa_live_runner.py` (task 253170) dispatches this strategy. An old runner would trade an unknown version
as v2.11, so restart that task, verify the boot line `crudem_abi=crudem-combo-c8-live-v1`, and only then reload the web
app. `live/crudem_runner.py` (task 275942) stays as the dry comparison and needs no restart.

The combination is a fit to 1 Jun-6 Oct 2026, and the engine is frozen: never tune it from paper or live results.
