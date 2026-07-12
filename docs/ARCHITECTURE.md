# labs_project — Architecture

Real-money NIFTY/BANKNIFTY/SENSEX options trading + paper-research platform.
Companion to the research repo `alphaIMB` (which this repo **reads data from**;
see "Cross-repo"). Last reviewed 2026-07-12.

> Orientation tip: `graphify query "<question>"` (v0.9.2) is current in this repo
> and indexes ~1.8k nodes. Use it to locate code; it does **not** replace reading
> the actual lines for exact logic. Run `graphify update .` after code changes.

---

## 1. Two parallel stacks (the most important thing to know)

The repo is deliberately split into **two import-isolated stacks** that share
only neutral infra (`config/`, `storage/`, `market_data/`):

| | `live/` — real money | `labs/` — paper / research |
|---|---|---|
| Purpose | Places actual broker orders | Backtests, paper trackers, dashboards |
| DB | `storage/live.db` (`live_*` tables) | `storage/labs.db` (paper tables) |
| Isolation | imports ONLY `live.*` + neutral infra; **never** `labs.engine.*` and never a broker SDK directly | may import `labs.*`, `market_data.*` |
| Entry point | `pa_live_runner.py` → `live/live_runner.py` | `app.py` (Flask), `pa_*` loops |

**Never cross the streams.** `live/` must stay importable without pulling in the
paper engine (spec §1.4). Broker SDKs are imported lazily inside `connect()`.

---

## 2. `live/` — the real-money stack

- **`live/live_runner.py`** — always-on poll loop (2s). The **only** order-placing
  owner. Multi-user: iterates every active `(user_id, conn_id)` connection
  independently (own mode, gates, reconciliation, daily-loss, EOD square-off,
  idempotency ledger, DB trade-state). An exception in one connection never
  aborts the loop for others. Contains the **entry-spot stop overlay**
  (`_entry_spot_stop_hit`) and startup **reconcile** (DB-state vs broker truth).
- **`live/live_executor.py`** — `place_idempotent` chokepoint; every order intent
  routes through here (idempotency = `live_orders` table).
- **`live/live_service.py`** — connections, per-conn config (`live_config`),
  trade-state read/write, day-PnL.
- **`live/env_loader.py`** — loads private live env into the web app (arming was
  gate-blocked before this existed).
- **`live/proxy.py` / `live/auth_gate.py` / `live/notify.py`** — static-IP order
  proxy wrap, arming gates, Telegram.
- **`live/brokers/`** — `base.py` (Position/OrderResult), `angel.py`, `zerodha.py`
  adapters. SDK imports deferred into `connect()`.
- **`live/engine/`** — the strategy brain (shared by live + labs pricing):
  - `champion_sim.py` — the v2.11 champion replay core: alpha entries/exits +
    spot trail/SL + the **entry-spot stop/recovery** overlay (gated by
    `enable_entry_spot_recovery`). Detection on completed 1-min bar
    **low(call)/high(put)**, valued at bar **close**, executed at the **next
    mark** (nextmark).
  - `champion_v213.py` — v2.13 **coupled** engine: replays v2.11 as the
    authoritative lifecycle, then applies the entry-spot overlay **only inside**
    each v2.11 holding window (force-closed at the v2.11 exit).
  - `champion_decider.py` — `champion_target` / `reconcile_replay_event` — the
    canonical decision stream the live runner follows (cursor = `champion_closed_count`).
  - `champion_inputs.py` — builds sim inputs; sources 1-min spot OHLC from the
    labs collector store (`data/live/<date>_<SYM>_spot_1min.csv`, tar archive
    after KEEP_DAYS). mtime-keyed memoization for the per-minute cadence.
  - `signal_engine.py`, `alpha_hybrid.py`, `gemini_range.py` — alpha bars,
    hybrid range, ML range.

---

## 3. `labs/` — the paper / research stack

- **`labs/ui/routes.py`** — the `/labs` Flask dashboard (nifty/alpha_v212/
  alpha_v213/sensex tabs, baskets, backfill endpoints).
- **`labs/ui/live_routes.py`** — the `/live` control panel (configure, arm,
  monitor).
- **`labs/engine/`** — paper trackers + backtests:
  - `paper_strategy_tracker.py` — **v2.11 paper** (nifty tab). Prices at option
    **LTP** (no spread) + `charges.round_trip_charges`.
  - `alpha_v212_tracker.py` / `alpha_v213_tracker.py` — **v2.12 / v2.13 paper**.
    Price each segment **ask-in / bid-out** (`_price_segment`) + charges. Backfill
    variants: `alpha_v21{2,3}_backfill.py` (bounded, HTTP-driven).
  - `charges.py` — `round_trip_charges` (brokerage/STT/txn/SEBI/stamp/GST).
  - `sensex_*` — SENSEX variants (alpha, v2.11, inverted).
  - `backtest.py`, `basket_replay.py`, `strategy_runner.py`,
    `indicator_engine.py`, `condition_evaluator.py`, `resampler.py`,
    `data_loader.py`, `position_manager.py`, `paper_executor.py` — the generic
    bot framework + research harness.

**Pricing basis differs by strategy** (a real gotcha): v2.11 paper = **LTP**
(no spread, optimistic); v2.12/v2.13 = **ask-in/bid-out** (honest). Comparing
them head-to-head requires putting all on one basis.

---

## 4. Data flow

```
Zerodha Kite (data)                     Angel/Zerodha (execution)
      │  auth/session_manager.py               │  live/brokers/*
      ▼                                         ▼
collector/*  ──►  ~/shared_market_data/          live/live_runner.py (2s loop)
  options_collector  live/<DATE>/<SYM>_options_1min.parquet.zst   │
  spot/futures       archive/<DATE>/…  (zstd parquet, cols:       │ orders
                     ts,tradingsymbol,strike,option_type,expiry,  ▼
                     ltp,bid,ask,oi,volume,spot)          storage/live.db (live_*)
      │                                                            │
      ▼  market_data/shared_store.py (reads parquet/csv)          │
labs/engine/* trackers  ──►  storage/labs.db (paper tables)  ◄────┘ (paper mirrors live decisions)
      ▼
app.py / labs/ui  (Flask dashboards)
```

- **`market_data/shared_store.py`** — the single reader for the shared options
  store (transparent parquet/zstd + gzip/csv fallback, live→archive).
- **`market_data/expiry.py`** — `select_expiry_code` (nearest/next weekly).
- **`logs/spot2s_<DATE>.csv`** — every 2s Kite spot poll, logged by
  `live_runner._log_spot_sample` (`ts,source,spot`). Used to replay tick-vs-1min
  stop decisions offline.

---

## 5. Storage

- **`storage/live_db.py` → `storage/live.db`** (rollback-journal). `live_*`
  tables, per-`(user_id, conn_id)`:
  - `live_trade_state` — restart-safe single-row position state (position, side,
    symbol, entry_spot/price/time, recovery_*, `champion_*` replay cursor).
  - `live_trades` — round-trip trades (entry/exit price, gross/charges/net, `dry_run`).
  - `live_orders` — the idempotency ledger (one row per idem_key).
  - `live_day_pnl` — per-date realized PnL (the **daily-loss kill-switch source**;
    keep it honest — see phantom-guard gotcha).
  - `live_config`, `live_broker_connections`, `live_credentials_enc` (Fernet).
- **`storage/db.py` → `storage/labs.db`** — paper: `alpha_v21{2,3}_daily/_trades`,
  `paper_strategy_daily/_trades`, `sensex_*`, generic-bot `bots/trades/signals`.
- `config/labs_config.py` — `DB_PATH`, `LIVE_DB_PATH`, `SHARED_LIVE_DIR`,
  `STATE_DIR`, lot sizes, cutoffs. **Use these, never hardcode paths.**

---

## 6. Strategies (NIFTY)

| | v2.11 (champion) | v2.12 (decoupled) | v2.13 (coupled/additive) |
|---|---|---|---|
| Base | alpha (5-min) entries + spot trail/SL | v2.11 + entry-spot stop/recovery, runs **all day** | v2.11 risk authority + v2.12 overlay **bounded to v2.11 hold** |
| Direct adverse move | **rides it** (no entry-spot stop; trail only arms after +40) | **cuts near breakeven** fast, then recovers | cuts like v2.12 but stops churning when v2.11 exits |
| Paper pricing | LTP | ask-in/bid-out | ask-in/bid-out |

- **Alpha entries/exits are on 5-min bars; spot-based stops on 1-min bar (paper)
  or 2s tick (live).**
- **Live entry-spot stop overlay** (`live_runner._entry_spot_stop_hit`, selected
  per-connection `strategy_version`):
  - **v2.13** → fast tick stop past a **5-pt buffer** (`ENTRY_SPOT_TICK_BUFFER`).
  - **v2.12** → **minute-boundary close-check**: fire only at the first poll of a
    new minute (completed candle close, our own 2s feed — no Kite-OHLC wait),
    after the entry candle. ≈ paper cadence, ~2s latency.
  - Re-entry always stays **canonical** (candle resolution); the tick only
    accelerates the exit. Cursor is not advanced by the overlay so paper and live
    record the same canonical segment.

---

## 7. Runtime (PythonAnywhere always-on tasks)

| Task | Command | Role |
|---|---|---|
| **253170** | `pa_live_runner.py` | the real-money runner (restart after `live/` changes) |
| 242603 | `pa_run_collector.py` | shared-store collector |
| 242605 | `pa_strategy_runner.py` | generic bot runner |
| 256994 | `pa_paper_tracker_loop.py` | paper trackers loop |

Web app: `labs-mvkumar01.pythonanywhere.com` (reload after code changes).
`eod_maintenance.py` tars the day's spot CSVs into `data/archive/`.

---

## 8. Deploy flow + PA gotchas (read before deploying)

**Local → Git → PA.** On PA:
```
cd ~/labs_project
git stash --include-untracked          # <-- MUST include untracked
git merge --ff-only origin/main        # (or: git stash && git pull --ff-only origin main)
```
Then **reload the web app** and **restart the runner (task 253170)**.

- **Never deploy by direct file upload.** Direct uploads leave the file
  **untracked / ahead of HEAD**; a later `git stash` reverts them and untracked
  copies **block `git merge`** (`error: untracked working tree files would be
  overwritten`). This has silently un-deployed live code more than once. Deploy
  via git so **HEAD advances**.
- **`git stash` (plain) does not touch untracked files** — use
  `--include-untracked` or the merge is blocked.
- **PA console API needs a browser-started console** — sending input to a cold
  console returns **HTTP 412**. Open a Bash console in the browser first.
- The GitHub **PAT is embedded plaintext** in PA `.git/config` remote URL — rotate
  it periodically; prefer a credential helper.
- `storage/live.db` and `data/`/`logs/` are **gitignored** — safe to hand-edit on
  PA without git conflicts (git operations won't clobber them).

---

## 9. Key invariants / gotchas

1. **Daily-loss guard integrity.** `live_day_pnl.realized_pnl` gates the
   kill-switch. `_record_exit_result` **suppresses any exit with no captured
   `entry_price`** (≤0) — otherwise a zero-cost-basis exit fabricates P&L
   (the 2026-07-10 +₹34k phantom that blinded the guard).
2. **Reconcile on startup only.** DB trade-state is compared to broker truth when
   the runner (re)starts; a mismatch **blocks new entries** (safe). Restart the
   runner after the daily broker-token refresh so flat/stale states reconcile.
3. **Cross-repo:** this repo **reads** `alphaIMB` data (spot OHLC, ranges). Watch
   the `"NIFTY" ⊂ "BANKNIFTY"` substring trap when filtering symbols.
4. **Charges are real** (~₹75–80/round-trip). Churn (many small stop/re-entry
   cycles) is negative on both spread and charges — the v2.12/v2.13 tick rules
   exist to suppress intra-candle churn.
5. **graphify-out/ is committed and ~12 MB** — noisy in diffs; consider
   gitignoring and regenerating locally.

---

## 10. Where to start for common tasks

| Task | Start here |
|---|---|
| Live order/exit logic | `live/live_runner.py` (`process_connection`, `_entry_spot_stop_hit`, `_record_exit_result`) |
| Strategy rules (v2.11/2.12/2.13) | `live/engine/champion_sim.py`, `champion_v213.py` |
| Paper P&L / pricing | `labs/engine/alpha_v21{2,3}_tracker.py` (`_price_segment`), `paper_strategy_tracker.py`, `charges.py` |
| Dashboards / endpoints | `labs/ui/routes.py`, `labs/ui/live_routes.py`, `app.py` |
| Market data | `market_data/shared_store.py`, `collector/*`, `market_data/expiry.py` |
| Schema | `storage/live_db.py`, `storage/db.py` |
