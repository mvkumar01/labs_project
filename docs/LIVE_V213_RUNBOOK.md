# Alpha v2.13 Live Parity Runbook

## Strategy contract

Alpha v2.13 runs the complete v2.11 champion lifecycle as its risk authority.
The entry-spot overlay may exit and recover the same side only while that
shadow lifecycle is active. Any v2.11 exit closes the active overlay segment
or cancels a pending recovery.

The original v2.11 signal-entry spot remains the stop and recovery barrier.
A recovery execution spot is separate and is used for option strike selection.

## Paper and live paths

- Paper engine: `live/engine/champion_v213.py`
- Paper ledger: `alpha_v213_daily` and `alpha_v213_trades`
- Paper task: PythonAnywhere task `256994`
- Live decision path: `live/engine/champion_decider.py`
- Live runner: PythonAnywhere task `253170`
- Live selector values: `decision_engine=champion_replay`,
  `strategy_version=v2.13`

Paper replay and live decisions call the same v2.13 engine. Intraday paper rows
add a holding mark for display and pricing without closing the shadow v2.11
lifecycle.

## Cursor safety

Closed overlay segments are consumed through the persisted champion cursor.
The cursor is scoped by trade date and strategy version. On the first deploy
with the new version column, an already-open legacy v2.12 row keeps its prior
count and event ID; only the version is stamped. A flat strategy change adopts
the selected replay's current cursor so historical exits are not sent live.

## Operating rules

1. Change strategy only while the broker and database are flat.
2. Deploy with commit, push, PythonAnywhere update, web reload, then restart
   tasks `253170` and `256994`.
3. Confirm the paper tab at `/labs/live?tab=alpha_v213` has a current row.
4. Confirm the live configure page lists Alpha v2.13. Do not switch a user's
   selected live strategy as part of deployment.
5. Check task logs for input errors, cursor repeats, or unavailable quotes.
