# CRUDEOIL MACD/Supertrend paper book — runbook

Tab: `/labs/live?tab=crude_macd_st`. Paper only; never places orders.

**Rule** (Strategy Tester `results/crudeoil/ui_20260919_084538_9143`, rank 194):
`macd(12,26,9)@5m hist x< 0 & supertrend(7,2)@5m dir x< 0`, gate `1m close > previous
session close`, long 1 lot (100), stop 1.5×ATR(14,5m), target 0.5R, no time stop, flat at
the 23:29 bar, 30-bar cooldown, one position at a time. Zerodha MCX futures charges, no
slippage.

**Parity**: replaying the Tester's cached CRUDEOIL26SEPFUT minutes reproduces all 39 rank-194
trades exactly (entries, exits, prices); a 60-day replay window gives identical results, and
minute-by-minute live replays never differ from the final replay.

**Data**: `labs/engine/crude_macd_st_tracker.py` pulls completed 1-minute candles from Kite
(labs token) into `crude_minute_bars`; `crude_minute_coverage` marks fetched past sessions.
Front contract = nearest listed expiry after the session (rolled on the expiry day). Kite
lists no expired contracts, so history before 21 Sept 2026 is CRUDEOIL26SEPFUT.

**Live**: `pa_paper_tracker_loop.py` runs `run_live()` every minute on weekdays 09:00–23:50.
A session is frozen (`final`) on the first run after 23:40; a session left `live` is frozen
by the next day's first run. The always-on task must be restarted after deploying.

**Backfill** (from 1 June; safe to re-run, frozen sessions are skipped):

```bash
cd ~/labs_project && python3 -m labs.engine.crude_macd_st_backfill 2026-06-01
```

or the tab's "Backfill" button. Add `--rebuild` to recompute frozen sessions.

**Tables**: `crude_macd_st_daily`, `crude_macd_st_trades`, `crude_minute_bars`,
`crude_minute_coverage` in `storage/labs.db`.

**Known limit**: the session grid is 09:00–23:29 as in the research. When US DST ends
(MCX closes at 23:55 from November) bars after 23:29 are ignored unless the session
constants are changed and the result re-validated.
