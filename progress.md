# Labs Project — Build Progress

## Status: Phase 2 complete and deployed

---

## Phase 1 — Initial Build  ✅ Complete

**Goal:** Standalone paper-trading lab, fully independent of alphaIMB.

### Delivered

| Component | File(s) | Notes |
|---|---|---|
| Zerodha auth | `auth/generate_token.py`, `auth/session_manager.py` | TOTP login; walks up to 5 redirect hops to extract request_token from localhost callback |
| Market data collector | `collector/run_collector.py`, `spot_collector.py`, `options_collector.py`, `instruments.py` | 60s loop, spot + options chain (500-symbol batches) |
| SQLite schema | `storage/db.py` | 7 tables: bots, bot_params, lab_bot_legs, positions, trades, signals, daily_summaries; WAL mode |
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
- `RuntimeError: Could not extract request_token` — Zerodha redirect chain is two hops; fixed by looping up to 5 redirects
- `ModuleNotFoundError: No module named 'config'` on PA — fixed by adding `sys.path.insert(0, project_root)` in `storage/db.py`
- PA path corruption in curl calls — fixed with `MSYS_NO_PATHCONV=1`
- Duplicate always-on task (241267 + 241272) — deleted 241267
- WSGI upload exit code 26 — uploaded from Windows temp dir instead of Git Bash `/tmp`

---

## Phase 2 — Multi-Leg Strategy Builder  ✅ Complete

**Goal:** Replace single-strategy model with a flexible per-leg condition builder where each bot can have up to 4 independent legs (C1/C2 = CE, P1/P2 = PE), each with dynamic entry/gate/exit/stoploss conditions configurable from the UI.

**Commit:** `f74e8c0` — *feat: add multi-leg strategy builder with dynamic condition UI*

### Files changed

| File | Change |
|---|---|
| `storage/db.py` | Added `lab_bot_legs` table; safe `leg_code` column migration for existing `positions` rows |
| `labs/engine/resampler.py` | Replaced `to_5min` with `get_resampled_data(df_1min, tf, now)` supporting 1m/5m/10m/15m; `to_5min` kept as alias |
| `labs/engine/indicator_engine.py` | Added ADX/DI+/DI− (Wilder's smoothing); added single-value helpers: `rsi_value`, `ema_value`, `sma_value`, `adx_values` |
| `labs/engine/condition_evaluator.py` | **NEW.** `TFCache` (per-tick resample cache); `evaluate_condition()` with `match` dispatch; `evaluate_entry()` (AND/OR); `evaluate_gates()` (AND); `evaluate_exits()` (first match) |
| `labs/engine/strategy_runner.py` | Added `process_bot_with_legs()` and `_process_leg()`; `process_bot()` routes to leg-based or classic path based on presence of `lab_bot_legs` rows |
| `labs/services/bot_service.py` | Added `LEG_CODES`, `save_legs()` (upsert via ON CONFLICT), `get_legs()`; `clone_bot()` now copies legs |
| `labs/ui/routes.py` | Added `_parse_legs(form)` helper; `new_bot` and `edit_bot` POST handlers call `save_legs()`; added `/api/<bot_id>/legs` endpoint |
| `templates/bot_form.html` | Full rewrite: leg toggle pills (C1/C2/P1/P2), 4 leg cards each with 4 condition sections (A: Entry with AND/OR, B: Gates, C: Exit, D: Stoploss); hidden JSON inputs per section; seeds `EXISTING_LEGS` for JS |
| `static/labs.js` | Added `CONDITION_DEFS`, `toggleLegCard()`, `addRow()`, `deleteRow()`, `updateConditionParams()`, `serializeAllLegs()`, `initConditionBuilder()` (populates from `EXISTING_LEGS` on edit) |
| `static/labs.css` | Added styles for leg cards, toggle pills, CE/PE colour coding, condition rows, param inputs, timeframe selects |

### Condition types supported

**Entry conditions (indicator, per timeframe):**
`rsi_lt`, `rsi_gt`, `ema_gt`, `ema_lt`, `adx_diplus_gt_diminus`, `adx_diminus_gt_diplus`

**Entry gates (spot vs SMA, per timeframe):**
`spot_gt_sma`, `spot_lt_sma`, `spot_above_sma_by`, `spot_below_sma_by`, `ema_gt`, `ema_lt`

**Exit / Stoploss (spot or LTP P&L, or indicator):**
`spot_gain_gte`, `spot_loss_gte`, `ltp_gain_gte`, `ltp_loss_gte`, `rsi_gt`, `rsi_lt`, `ema_gt`, `ema_lt`, `sma_gt_spot`, `sma_lt_spot`

Supported timeframes: **1m, 5m, 10m, 15m** (all resampled from 1-min live data on demand via `TFCache`).

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

- **Backtesting:** No historical backtest runner yet. All evaluation is forward paper-trading.
- **Bot cloning UI:** Clone works via POST form on the detail page but no dedicated UI flow.
- **Leg-level position sizing:** Currently inherits bot-level lot size; no per-leg qty control.
- **Re-entry logic:** `allow_reentry` flag is read but not fully implemented in the leg path.
- **Condition types to add if needed:** `adx_gt` (ADX strength threshold), `atr_gt` / `atr_lt` (volatility gate), `time_of_day` gate.
- **Signal logging:** Classic path logs signals; leg-based path does not yet write to `signals` table.
