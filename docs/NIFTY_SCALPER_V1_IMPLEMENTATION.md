# Nifty Scalper V1 implementation — 21 October 2026

Implementation complete and validated. Vipin approved commit, push, PA deployment, web reload and restart
of Proposer task 274885 on 21 October 2026. Account switching and LIVE arming remain Vipin's actions.
Deployment receipts and backups are kept under the ignored `storage/state/nifty_scalper_deploy/` directory.

## Behavior

- New Configure preset `nifty_scalper_v1` selects `proposer_nifty_scalper_v1`, owned by the Proposer runner.
- Signals and the 75-point direction filter remain on SENSEX. NIFTY options execute on NFO, with NIFTY spot exits.
- The shared scaling helper uses the NIFTY/SENSEX 09:15 candle-open ratio. Broker candles are attempted after
  09:16:10, with exact collector 09:15 opens as fallback. Entries wait for a valid ratio. A resolved ratio
  is cached and saved per connection/date for restart recovery; parameters rebuild each session.
- Nearest NIFTY expiry strictly after the session date: on expiry day choose the next listed contract,
  including holiday-shifted weekly codes and the monthly contract. If unavailable, skip entry.
- Shared live/paper strike picker preserves the existing paper book's IV/delta calculations and
  shallowest eligible ATM +/-50/100/150 selection. The plan calls this Black-76, but the existing reference
  implementation is Black-Scholes (`implied_vol` / `bs_delta`); its numerical behavior is preserved.
- Quantity is configured lots x 65, capped at 27 lots (1,755 quantity under the 1,800 freeze), with existing
  size-rejection step-down handling. IOC entry, DAY exit, gates, daily-loss cap, kill switch and ledger remain
  on the existing executor path.
- Day target defaults to 1.5%, editable from 0.5–10%; Configure explicitly says only 1.5% was tested on NIFTY.
  The UI retains the flat-position and runner-version checks. Decision ABI is `proposer-dt25-live-v11`.
- Vipin approved 25% bank-confirmation tolerance in this chat. Both the new live preset and
  `proposer_nifty_v5` use it. Other SENSEX presets retain their existing tolerance.
- Exact Kite quote symbols are retained independently of Angel execution symbols, so weekly/monthly
  NIFTY positions remain priceable after a restart.

## Validation

`tests/test_nifty_scalper_v1.py` covers parameter parity, invalid/missing ratio, session rollover and
restart, batch spot reads, expiry-day and monthly contracts, strike parity, NIFTY sizing/spot exits,
SENSEX direction gating, concurrent NIFTY/SENSEX connections, Angel symbol mapping, approved banking
tolerance, ABI, editable target, and Configure rejection on an open position or older runner.

Results: **33 new NIFTY tests passed**. The final full-suite collection (before the last rollover test
was added) returned **820 passed, 7 failed, 2 errors**; all nine issues match the documented baseline
(`test_exit_retry` x3, market-data routing, IOC validity x1 + 2 errors, tick-stop double-fire and overlay).
The final focused NIFTY/paper run, including rollover, returned **39 passed**. The broader Proposer
focused run returned **86 passed** before the last four NIFTY cases were added. `git diff --check` passed.

Recorded-session replay results, with the approved 25% tolerance:

| Session | Runner / paper trade count | Side, contract, exit rule | Largest entry/exit timestamp difference |
| --- | --- | --- | --- |
| 2026-10-13 | 1 / 1 | All match | 0.750 s |
| 2026-10-16 | 1 / 1 | All match | 0.526 s |
| 2026-10-19 | 1 / 1 | All match | 0.366 s |
| 2026-10-21 | 1 / 1 | All match | 0.714 s |

An initial five-samples-per-minute harness missed day-target crossings on two days, producing later
spot-target exits. Using the reference's dense intraminute price path resolved both mismatches.

Read-only PA preflight confirmed all eight application files matched local HEAD before implementation
deployment. Archived chains include next-expiry coverage:

| Session | NIFTY expiries in latest snapshot |
| --- | --- |
| 2026-10-13 | 26O13, 26O19, 26OCT |
| 2026-10-16 | 26O19, 26OCT |
| 2026-10-19 | 26O19, 26OCT |
| 2026-10-21 | 26OCT, 26N03 |

Snapshots, test logs and replay results are isolated under the ignored
`storage/state/nifty_scalper_preflight/` directory. Raw `data/live/` was not modified.

Reproduce the read-only data acquisition and DRY_RUN replay:

```powershell
python scripts/check_nifty_scalper_pa.py
python scripts/check_nifty_scalper_pa.py --days 2026-10-12 2026-10-15 --spots-only
python scripts/replay_nifty_scalper.py
python -m pytest -q -p no:cacheprovider tests --basetemp=storage/state/pytest-scalper-check
```

The replay drives `process_connection` against a fake broker and a separate database, minute by minute,
using the reference paper book's dense OHLC/fitted-delta path inside each minute. Live sizing stays at
one configured lot; paper sizing stays at its premium budget. This validates decisions on the modeled
path, not real fills, slippage or tick-level timing. It stops after the runner confirms the day is banked.

## Rollout after approval

1. Commit only this implementation's files/hunks, then push. Preserve other sessions' edits.
2. Outside 09:15–15:30 IST, re-check every affected PA file against the saved baseline. Upload through the
   PA files API, merging only these changes if PA has drifted, and verify the uploaded content.
3. Reload `labs-mvkumar01.pythonanywhere.com`; restart Proposer task **274885** and verify its boot log
   reports `abi=proposer-dt25-live-v11`. Leave NIFTY alpha task **253170** alone.
4. The paper tolerance change also requires refreshing the paper process and rebuilding the NIFTY
   paper backfill. Identify that process and show the exact restart/backfill action for approval first;
   this plan does not authorize restarting an unidentified task.
5. Vipin switches the intended Angel connection while flat and selects DRY_RUN. Collect at least three
   sessions of paper-vs-DRY_RUN evidence before Vipin arms LIVE himself, initially at one lot.

PA `config/telegram.json` is absent: entry/cancel/step-down notifications are unavailable. No Telegram
messages were sent during implementation. Account sharing with other strategies still needs checking
before Vipin switches the Angel connection.
