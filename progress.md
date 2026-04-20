# Labs Project — Build Progress

## Current Status: Phase 3 (post-launch fixes) complete

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

**Entry:** `rsi_lt`, `rsi_gt`, `ema_gt`, `ema_lt`, `sma_gt`, `sma_lt`, `adx_diplus_gt_diminus`, `adx_diminus_gt_diplus`

**Gate:** `spot_gt_sma`, `spot_lt_sma`, `spot_above_sma_by`, `spot_below_sma_by`, `ema_gt`, `ema_lt`, `sma_gt`, `sma_lt`

**Exit:** `spot_gain_gte`, `spot_loss_gte`, `ltp_gain_gte`, `ltp_loss_gte`, `spot_gt_sma`, `spot_lt_sma`, `rsi_gt`, `rsi_lt`, `ema_gt`, `ema_lt`, `sma_gt`, `sma_lt`, `sma_gt_spot`, `sma_lt_spot`

**Stoploss:** `spot_loss_gte`, `ltp_loss_gte`, `spot_gain_gte`, `ltp_gain_gte`, `spot_gt_sma`, `spot_lt_sma`, `rsi_gt`, `rsi_lt`, `sma_gt`, `sma_lt`

---

## What's Deployed on PA

| Component | Status |
|---|---|
| Web app | ✅ Live — nifty-multi-mvkumar01.pythonanywhere.com |
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
