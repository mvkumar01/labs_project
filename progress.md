# Labs Project — Build Progress

## Current Status: Phase 5 (shared market-data store migration) complete

---

## Phase 1 — Initial Build  ✅ Complete

**Goal:** Standalone paper-trading lab, fully independent of alphaIMB.

### Delivered

| Component | File(s) | Notes |
|---|---|---|
| Zerodha auth | `auth/generate_token.py`, `auth/session_manager.py` | TOTP login; walks up to 5 redirect hops to extract request_token |
| Market data collector | `collector/run_collector.py`, `spot_collector.py`, `options_collector.py`, `instruments.py` | 60s loop, spot + options chain (500-symbol batches) |
| SQLite schema | `storage/db.py` | 7 tables: bots, bot_params, lab_bot_legs, positions, trades, signals, daily_summary; WAL mode |
| Config | `config/labs_config.py`, `zerodha_creds.json` | NIFTY/BANKNIFTY/SENSEX underlyings, market hours, dirs |
| 4 classic strategies | `labs/strategies/` | rsi_sma, rsi_ema_sma, ema_crossover, trend_pullback |
| Paper executor | `labs/engine/paper_executor.py` | open_position / close_position → DB (no real orders) |
| Position manager | `labs/engine/position_manager.py` | LTP SL/target, spot SL/target, indicator exits |
| Data loader | `labs/engine/data_loader.py` | CSV → DataFrame for spot + options |
| Strategy runner | `labs/engine/strategy_runner.py` | 60s loop, market-hours guard, per-bot routing |
| Flask web UI | `app.py`, `labs/ui/routes.py`, `templates/` | Dashboard, create/edit/detail, AJAX APIs |
| Styles + JS | `static/labs.css`, `static/labs.js` | Equity chart, trade log, signal log |
| EOD maintenance | `eod_maintenance.py` | Daily summaries + CSV archiving |
| PA deployment | Always-on tasks 241272 + 241278; scheduled token task | Web app: nifty-multi-mvkumar01.pythonanywhere.com |

### Key bugs fixed during Phase 1
- `RuntimeError: Could not extract request_token` — Zerodha has a two-hop redirect chain; fixed by looping up to 5 redirects
- `ModuleNotFoundError: No module named 'config'` on PA — fixed by adding `sys.path.insert(0, project_root)` in `storage/db.py`
- PA path corruption in curl calls — fixed with `MSYS_NO_PATHCONV=1`
- Duplicate always-on task (241267 + 241272) — deleted 241267
- WSGI upload exit code 26 — uploaded from Windows temp dir instead of Git Bash `/tmp`

---

## Phase 2 — Multi-Leg Strategy Builder  ✅ Complete

**Goal:** Replace single-strategy model with a flexible per-leg condition builder — up to 4 independent legs (C1/C2 = CE, P1/P2 = PE), each with dynamic entry/gate/exit/stoploss conditions configurable from the UI.

**Commit:** `f74e8c0`

### Files changed

| File | Change |
|---|---|
| `storage/db.py` | Added `lab_bot_legs` table; safe `leg_code` column migration for `positions` |
| `labs/engine/resampler.py` | `get_resampled_data(df_1min, tf, now)` for 1m/5m/10m/15m; `to_5min` kept as alias |
| `labs/engine/indicator_engine.py` | ADX/DI+/DI− (Wilder's smoothing); single-value helpers: `rsi_value`, `ema_value`, `sma_value`, `adx_values` |
| `labs/engine/condition_evaluator.py` | **NEW.** `TFCache`; `evaluate_condition()` match dispatch; `evaluate_entry()` (AND/OR); `evaluate_gates()` (AND); `evaluate_exits()` (first match) |
| `labs/engine/strategy_runner.py` | `process_bot_with_legs()` + `_process_leg()`; routing to leg or classic path |
| `labs/services/bot_service.py` | `LEG_CODES`, `save_legs()`, `get_legs()`; `clone_bot()` copies legs |
| `labs/ui/routes.py` | `_parse_legs(form)`; save legs on POST; `/api/<bot_id>/legs` endpoint |
| `templates/bot_form.html` | Full rewrite: leg pills, 4 leg cards × 4 sections; hidden JSON inputs; `EXISTING_LEGS` seed |
| `static/labs.js` | `CONDITION_DEFS`, `toggleLegCard()`, `addRow()`, `deleteRow()`, `updateConditionParams()`, `serializeAllLegs()`, `initConditionBuilder()` |
| `static/labs.css` | Leg cards, toggle pills, CE/PE colours, condition rows, param inputs |

---

## Phase 3 — Post-Launch Fixes & Enhancements  ✅ Complete

### Bug fixes

| Commit | Bug | Root cause | Fix |
|---|---|---|---|
| `b3d970a` | Bot detail 500 on any bot | `bot.entry_rules \| join` — key is `entry_rules_json`, not `entry_rules`; Jinja2 `UndefinedError` on iteration | Removed legacy lines; replaced with leg count display |
| `53253ff` | Bot detail 500 on zero-trade bots | `get_performance_stats` returned `{"total_trades": 0}` with no `total_pnl_rs`; template comparison crashed | Return full zero-valued dict always |
| `9ababe9` | Bot delete 500 | `delete_bot()` referenced `daily_summaries` — table is `daily_summary` (singular) | Fixed table name |

### Features added

| Commit | Feature | Detail |
|---|---|---|
| `53253ff` | MA crossover conditions | `sma_gt` / `sma_lt` (SMA(a) vs SMA(b)) added to entry, gate, exit, stoploss sections |
| `53253ff` | Spot vs SMA exit/stoploss | `spot_gt_sma` / `spot_lt_sma` added to exit and stoploss sections (were gate-only before) |
| `1079a7f` | Permanent bot delete | `delete_bot()` removes all 7 tables in one transaction; POST route + confirmation dialog |
| `c2c8426` | Charges & net P&L | Formula: `₹40 + 0.00053×Turnover + 0.001×(S×Q)`; trade log shows Gross / Charges / Net columns; all metrics use `net_pnl_rs` |
| `c2c8426` | Correct lot sizes | NIFTY→65, BANKNIFTY→15, SENSEX→20 |

### Full condition type reference (as of Apr 2026)

---

## Phase 5 — Shared Market-Data Store Migration  ✅ Complete

**Goal:** Eliminate duplicate OI collection between Labs (1-min) and AlphaIMB (5-min). Labs collector becomes the single canonical writer; AlphaIMB reads from the shared store via a compatibility reader.

**Commit:** `3637f87`

### Architecture

```
~/shared_market_data/
└── live/
    └── YYYY-MM-DD/
        └── {UNDERLYING}_options_1min.csv   ← written by Labs collector
                                             ← read by AlphaIMB analytics.py
```

### Files changed

| File | Change |
|---|---|
| `config/labs_config.py` | Added `SHARED_MARKET_DIR`, `SHARED_LIVE_DIR`, `SHARED_ARCHIVE_DIR` paths |
| `collector/options_collector.py` | Rewrites to canonical schema at shared path. Columns: `timestamp, underlying, tradingsymbol, strike, option_type, expiry, ltp, bid, ask, oi, volume, spot` |
| `labs/engine/data_loader.py` | `load_options_1min()` reads from `SHARED_LIVE_DIR`; `latest_ltp()` filters on `tradingsymbol` column |
| `eod_maintenance.py` | Added `archive_shared_market()` (CSV → parquet.gz) and `purge_old_shared_market()` |

### Bug fixed during this phase

| Bug | Root cause | Fix |
|---|---|---|
| Zero P&L on all leg-based bots | `INSERT INTO positions VALUES (...)` positional form used 14 values but table had 15 columns after `leg_code` was added via `ALTER TABLE` — raised `OperationalError` caught silently | Switched to explicit column-list INSERT in `paper_executor.py` |

### Cross-project changes (alphaIMB repo)

| File | Change |
|---|---|
| `shared_market_reader.py` | **NEW.** Reads shared 1-min CSV, filters to 5-min timestamps, maps columns to AlphaIMB format |
| `analytics.py` | Replaced raw CSV read with `get_oi_dataframe()` call |
| `codeC_live_capture.py` | Removed ~25-line OI bulk fetch block; kept meta init + NIFTY multi-chart refresh |

---

## Phase 4 — Live Stability, Warmup, and Recovery Fixes  ✅ Complete

### Operational fixes

| Commit | Fix | Detail |
|---|---|---|
| `fc2cb51` | Auto token reload | `auth/session_manager.py` now reloads `zerodha_token.json` when it changes so collector/runner can pick up a refreshed token without a restart |
| `5633d09` | Keep runner alive outside market hours | Strategy runner no longer exits when the market is closed; it sleeps and continues until market hours begin |
| `6b23497` | Keep collector alive outside market hours | Collector no longer exits on a closed-market check; it sleeps and retries instead of terminating |
| `366aafc` | SMA50 warmup/backfill | Prior-session 1-minute spot history is loaded before live evaluation so 5-minute SMA50 gates can be ready from market open |
| `6c0d95c` | EOD square-off | Runner force-closes open positions after `EOD_CUTOFF` even if entry logic/gates are not evaluated |
| `4947693` | Harden EOD close | `paper_executor.close_position()` now always writes numeric `charges` and `net_pnl_rs`, uses explicit trade inserts, and includes one-time `eod_recovery.py` |
| `7986c6f` | Detail page log paging | Trade and signal logs on the bot detail page now load in capped chunks with `Load more` controls instead of stretching the page indefinitely |

### Live behavior notes

- Runner logs now show per-bot warmup status, including live rows, historical rows loaded, completed 5m bars, and whether SMA50 is ready.
- EOD recovery can be run manually with `python3 eod_recovery.py` after a code pull to clear any leftover open positions.
- The current Labs web app URL is `labs-mvkumar01.pythonanywhere.com`.

**Entry:** `rsi_lt`, `rsi_gt`, `ema_gt`, `ema_lt`, `sma_gt`, `sma_lt`, `adx_diplus_gt_diminus`, `adx_diminus_gt_diplus`

**Gate:** `spot_gt_sma`, `spot_lt_sma`, `spot_above_sma_by`, `spot_below_sma_by`, `ema_gt`, `ema_lt`, `sma_gt`, `sma_lt`

**Exit:** `spot_gain_gte`, `spot_loss_gte`, `ltp_gain_gte`, `ltp_loss_gte`, `spot_gt_sma`, `spot_lt_sma`, `rsi_gt`, `rsi_lt`, `ema_gt`, `ema_lt`, `sma_gt`, `sma_lt`, `sma_gt_spot`, `sma_lt_spot`

**Stoploss:** `spot_loss_gte`, `ltp_loss_gte`, `spot_gain_gte`, `ltp_gain_gte`, `spot_gt_sma`, `spot_lt_sma`, `rsi_gt`, `rsi_lt`, `sma_gt`, `sma_lt`

---

## What's Deployed on PA

| Component | Status |
|---|---|
| Web app | ✅ Live — labs-mvkumar01.pythonanywhere.com |
| Collector (always-on 241272) | ✅ Running |
| Strategy runner (always-on 241278) | ✅ Running |
| Token generation (scheduled 08:55 IST) | ✅ Configured |
| EOD maintenance (scheduled 15:40 IST) | ✅ Configured |

---

## Known Limitations / Next Steps

- **Backtesting:** No historical backtest runner. All evaluation is forward paper-trading only.
- **Re-entry logic:** `allow_reentry` flag exists in schema but not fully implemented in the leg-based path.
- **Signal logging:** Classic path logs to `signals` table; leg-based path does not yet.
- **Leg-level position sizing:** All legs inherit bot-level lot size; no per-leg qty control.
- **Condition types to add if needed:** `adx_gt` (ADX strength threshold), `atr_gt`/`atr_lt` (volatility gate), `time_of_day` gate.
- **Old trades:** Existing rows have `charges=0`, `net_pnl_rs=0` — no retroactive recalculation.
