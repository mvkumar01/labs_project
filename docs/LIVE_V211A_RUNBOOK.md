# Alpha v2.11A / Champion 2 Paper Runbook

## Status

Alpha v2.11A is a separate NIFTY paper book on `/labs/live?tab=alpha_v211a`.
The live page displays **Alpha v2.11A** only; the internal audit designation is
Champion 2. It does not replace or mutate the
Alpha v2.11 control ledger and it is not selectable in the real-money `/live`
strategy configuration.

## Strategy delta

Every v2.11 entry, filter, range, Alpha exit, drift stop, and EOD rule remains
unchanged except this cell:

```text
tier = PC400
gap direction = DOWN
position = PUT
resolved opening VIX is present and < 17

arm after 30 favourable NIFTY spot points
exit after a 20-point retrace from the best favourable spot
```

For a PUT, favourable movement is `entry_spot - minute_low`. After arming:

```text
stop = entry_spot - (peak_favourable - 20)
exit when minute_high >= stop
```

The existing champion simulator convention processes the one-minute low before
the high. Missing VIX does not qualify for the v2.11A overlay.

## Paper execution and persistence

- Tracker: `labs/engine/alpha_v211a_tracker.py`
- Tables: `alpha_v211a_daily`, `alpha_v211a_trades`
- Contract: nearest-weekly NIFTY ITM-200
- Entry: first executable two-sided quote at/after the event, paid at ask
- Exit: first executable two-sided quote at/after the event, sold at bid
- Charges: `labs.engine.charges.round_trip_charges`
- Runtime: `pa_paper_tracker_loop.py` under the existing paper-loop task

The `context_json` column records the resolved VIX/open/previous-close sources,
range, Alpha formula, the Champion 2 label, and the 30/20 parameters.

## Data sources

| Input | Primary source | Fallback / note |
|---|---|---|
| One-minute NIFTY spot OHLC | Labs `data/live` or EOD `data/archive/<date>.tar.gz` | Legacy alphaIMB OHLC fills older gaps; shared-store spot is last-resort flat OHLC |
| OI and option quotes | `~/shared_market_data/live/<date>/NIFTY_options_1min.*` | `market_data.shared_store` transparently reads the compressed archive |
| Verified 09:15 open | alphaIMB `data/analytics/nifty_1min_ohlc.csv` | Required for audited gap classification |
| Opening VIX | exact-date alphaIMB `data/analytics/vix_history.csv:vix_open` or supplied locked state | Invalid exact-date rows fail closed; missing VIX never activates v2.11A |
| Previous session close | shared-store preceding captured session | Sparse VIX history is deliberately not used as a close fallback |
| Range/bucket | locked `hybrid_range_state` or audited backfill override | Stored with provenance in `context_json` |

Raw market files are read-only. The tracker writes only SQLite paper results.

## Deployment

After the approved Git push:

1. On PythonAnywhere, stash generated/untracked files with
   `git stash --include-untracked` and fast-forward to `origin/main`.
2. Reload the web app so the Alpha v2.11A tab appears.
3. Restart paper task **256994** so it imports the new tracker.
4. Run a bounded historical backfill only after checking input/quote coverage;
   do not overwrite an existing valid row with an unavailable replay.
5. Verify `/labs/live?tab=alpha_v211a` shows `Alpha v2.11A`, strategy version,
   quote coverage, and the expected daily/trade rows.

The real-money runner task is not part of this rollout because v2.11A is a
paper-only champion book.
