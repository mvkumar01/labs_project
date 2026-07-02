# CLAUDE.md — Labs Project

## 1. What This Is

**Labs** is a standalone research/prototype trading platform for NIFTY/BANKNIFTY/SENSEX options, independent of the `alphaIMB` project. It now hosts **two surfaces**:

- **`/labs/`** (`labs-mvkumar01.pythonanywhere.com/labs/`) — **paper trading**. Execution is simulated with real market data; `paper_executor.py` never places real orders.
- **`/live/`** (`labs-mvkumar01.pythonanywhere.com/live/`) — **REAL-MONEY live trading**. Research-shortlisted candidate strategies are tested with **real money** through live brokers (Angel One, Zerodha) in small size. This is deliberate: some deployment bugs surface only in live (e.g. the recent Angel One issues — order/auth/position quirks — were diagnosable and fixable only against the real broker, not in paper). The `live/` package places real orders; `/labs/` does not.

Bot A (the `Nifty_Bots_Python` prototype) is **disabled** — live testing now runs through the labs `/live/` UI.

**Separation from alphaIMB:**
- Own Zerodha credentials (`config/zerodha_creds.json`)
- Own SQLite database (`storage/labs.db`)
- Own PA web app: `labs-mvkumar01.pythonanywhere.com`
- Own always-on tasks: Collector (run_collector.py) + Runner (strategy_runner.py)

---

## 2. Directory Structure

```
labs_project/
├── app.py                       Flask entry point — /labs blueprint, port 5001
├── eod_maintenance.py           EOD: daily_summary rows + CSV archiving
│
├── auth/
│   ├── session_manager.py       KiteConnect singleton (loads token from JSON)
│   └── generate_token.py        TOTP login → zerodha_token.json (PA scheduled 08:55 IST)
│
├── collector/
│   ├── run_collector.py         Market-hours loop (60s) — spot + futures + options
│   ├── spot_collector.py        Spot index 1-min OHLCV → data/live/ daily CSV
│   ├── futures_collector.py     Nearest-expiry futures 1-min OHLCV → data/live/ daily CSV (separate file from spot)
│   ├── options_collector.py     Option chain LTP → shared_market_data/live/ (canonical shared store)
│   ├── instruments.py           Option symbol builder (spot ± 10%, nearest 2 expiries) + get_current_future()
│   └── kite_bars.py             Shared "completed 1-min candle" fetch, used by spot_collector + futures_collector
│
├── config/
│   ├── labs_config.py           Constants: UNDERLYINGS, market hours, intervals, dirs, shared paths
│   ├── zerodha_creds.json       API key, secret, user_id, password, totp_key  [tracked]
│   └── zerodha_token.json       Access token (regenerated daily)  [gitignored]
│
├── data/
│   ├── live/                    Spot + futures 1-min OHLCV CSVs  [NEVER MODIFY]
│   │                            {date}_{UNDERLYING}_spot_1min.csv / _futures_1min.csv
│   └── archive/                 EOD .tar.gz archives    [gitignored]
│
│   NOTE: Options data is no longer in data/live/. It is written to the shared
│   store at ~/shared_market_data/live/YYYY-MM-DD/{UNDERLYING}_options_1min.csv
│
├── labs/
│   ├── engine/
│   │   ├── condition_evaluator.py  TFCache + per-condition evaluators (entry/gate/exit/SL)
│   │   ├── data_loader.py          CSV → DataFrame for spot + options
│   │   ├── indicator_engine.py     RSI, EMA, SMA, ADX/DI+/DI−, ATR; single-value helpers
│   │   ├── paper_executor.py       open_position / close_position → DB; charges computation
│   │   ├── position_manager.py     Exit checks: LTP SL/target, spot SL/target, indicator
│   │   ├── resampler.py            get_resampled_data(df_1min, tf, now) → 1m/5m/10m/15m
│   │   └── strategy_runner.py      Main 60s loop: leg-based or classic path per bot
│   │
│   ├── strategies/                 Classic (pre-leg) strategy registry
│   │   ├── base.py                 Strategy ABC
│   │   ├── registry.py             name → class
│   │   ├── rsi_sma.py
│   │   ├── rsi_ema_sma.py
│   │   ├── ema_crossover.py
│   │   └── trend_pullback.py
│   │
│   ├── services/
│   │   ├── bot_service.py          CRUD for bots + legs (save_legs, get_legs, clone_bot, delete_bot)
│   │   └── metrics_service.py      P&L, trade log, signal log, equity curve, stats
│   │
│   └── ui/
│       └── routes.py               Flask blueprint: /labs/* + /labs/api/* endpoints
│
├── storage/
│   ├── db.py                    Schema init + get_conn() — WAL mode, Row factory
│   └── labs.db                  SQLite database
│
├── static/
│   ├── labs.css                 Styles: dashboard, bot cards, leg builder, condition rows
│   └── labs.js                  Equity chart, trade log, condition builder JS
│
├── templates/
│   ├── labs.html                Dashboard (bot summary grid)
│   ├── bot_form.html            Create/edit bot — leg cards + dynamic condition builder
│   └── bot_detail.html          Bot detail — stats, equity chart, trade log, signal log
│
└── logs/                        Runner + collector logs (gitignored)
```

---

## 3. Database Schema

Seven tables in `storage/labs.db`:

| Table | Purpose |
|---|---|
| `bots` | Core bot identity (name, underlying, strategy_type, status) |
| `bot_params` | All tunable params (RSI periods, EMA, session times, expiry mode…) |
| `lab_bot_legs` | Per-leg JSON condition sets for multi-leg bots |
| `positions` | Open paper positions (entry_ltp, entry_spot, symbol, side, leg_code) |
| `trades` | Completed trades (entry+exit LTP, pnl_pts, pnl_rs, charges, net_pnl_rs, exit_reason) |
| `signals` | Every evaluated signal (whether acted on or skipped) |
| `daily_summary` | Per-bot daily P&L rollup (written by eod_maintenance.py) |

**Important:** The table is `daily_summary` (singular), not `daily_summaries`.

### trades key columns
```
pnl_pts     — option points: exit_ltp − entry_ltp
pnl_rs      — gross rupees: pnl_pts × lot_size × qty
charges     — brokerage + taxes (see formula below)
net_pnl_rs  — pnl_rs − charges  ← used in all P&L metrics and display
```

### Charges formula
```
Q        = lot_size × qty
Turnover = (entry_ltp + exit_ltp) × Q
Charges  = 40 + 0.00053 × Turnover + 0.001 × (exit_ltp × Q)
```

### Lot sizes (as of Apr 2026)
| Underlying | Lot size | Strike step |
|---|---|---|
| NIFTY | 65 | 50 |
| BANKNIFTY | 15 | 100 |
| SENSEX | 20 | 100 |

### lab_bot_legs schema
```sql
CREATE TABLE lab_bot_legs (
    id                       TEXT PRIMARY KEY,
    bot_id                   TEXT NOT NULL REFERENCES bots(bot_id),
    leg_code                 TEXT NOT NULL,   -- C1, C2, P1, P2
    is_enabled               INTEGER NOT NULL DEFAULT 1,
    entry_logic              TEXT NOT NULL DEFAULT 'AND',
    entry_conditions_json    TEXT NOT NULL DEFAULT '[]',
    entry_gates_json         TEXT NOT NULL DEFAULT '[]',
    exit_conditions_json     TEXT NOT NULL DEFAULT '[]',
    stoploss_conditions_json TEXT NOT NULL DEFAULT '[]',
    created_at               TEXT NOT NULL,
    UNIQUE(bot_id, leg_code)
);
```

### Safe migrations in init_db()
Each startup checks for missing columns and adds them — safe to call repeatedly:
- `positions.leg_code` (TEXT)
- `trades.charges` (REAL DEFAULT 0)
- `trades.net_pnl_rs` (REAL DEFAULT 0)

---

## 4. Bot Architecture

### Two execution paths

**Leg-based path** (activated when `lab_bot_legs` rows exist for a bot):
- Each bot has up to 4 independent legs: `C1`, `C2` (CE/call legs), `P1`, `P2` (PE/put legs)
- Each leg has 4 condition sections (stored as JSON arrays):
  - **Entry Conditions** — AND or OR logic
  - **Entry Gates** — all must pass (AND-only)
  - **Exit Conditions** — first match wins (checked at 5-min bar close only)
  - **Stoploss Conditions** — checked every tick, first match wins
- Evaluated by `condition_evaluator.py`

**Classic path** (no legs configured):
- Uses Strategy class from `labs/strategies/registry.py`
- Selects entry/exit via `strategy.entry_signal(df_5min)` / `check_exit()`

### Condition types

| Section | Supported types |
|---|---|
| Entry | `rsi_lt`, `rsi_gt`, `ema_gt`, `ema_lt`, `sma_gt`, `sma_lt`, `adx_diplus_gt_diminus`, `adx_diminus_gt_diplus` |
| Gate | `spot_gt_sma`, `spot_lt_sma`, `spot_above_sma_by`, `spot_below_sma_by`, `ema_gt`, `ema_lt`, `sma_gt`, `sma_lt` |
| Exit | `spot_gain_gte`, `spot_loss_gte`, `ltp_gain_gte`, `ltp_loss_gte`, `spot_gt_sma`, `spot_lt_sma`, `rsi_gt`, `rsi_lt`, `ema_gt`, `ema_lt`, `sma_gt`, `sma_lt`, `sma_gt_spot`, `sma_lt_spot` |
| Stoploss | `spot_loss_gte`, `ltp_loss_gte`, `spot_gain_gte`, `ltp_gain_gte`, `spot_gt_sma`, `spot_lt_sma`, `rsi_gt`, `rsi_lt`, `sma_gt`, `sma_lt` |

- `ema_gt` / `ema_lt` — EMA(period_a) vs EMA(period_b)
- `sma_gt` / `sma_lt` — SMA(period_a) vs SMA(period_b) (labelled "MA" in the UI)
- `spot_gt_sma` / `spot_lt_sma` — current spot vs SMA(period)

Every condition JSON: `{"type": "rsi_lt", "timeframe": "5m", "params": {"period": 3, "value": 30}, "enabled": true}`

Supported timeframes: `1m`, `5m`, `10m`, `15m` — resampled from 1-min live data via `TFCache`.

**To add a new condition type:** add a `case` to `condition_evaluator.py` AND an entry to `CONDITION_DEFS` in `static/labs.js`.

### Contract selection
Bot-level settings: `expiry_mode` (nearest_weekly / next_weekly / monthly), `strike_mode` (atm / itm_N / otm_N), `strike_offset_pts`, `hold_same_contract`.

### Bot status lifecycle
- `paused` → can be edited, activated, cloned, or deleted
- `active` → monitored by strategy_runner; cannot be edited or deleted
- `archived` → not monitored; visible on dashboard but cannot change status; can be deleted

---

## 5. PythonAnywhere Setup

| Task | Type | Command |
|---|---|---|
| Token generation | Scheduled (08:55 IST) | `python3 auth/generate_token.py` |
| Data collector | Always-on (ID: 241272) | `python3 pa_run_collector.py` |
| Strategy runner | Always-on (ID: 241278) | `python3 pa_strategy_runner.py` |
| EOD maintenance | Scheduled (15:40 IST) | `python3 eod_maintenance.py` |
| EOD recovery | Manual one-time | `python3 eod_recovery.py` |

**Web app:** `labs-mvkumar01.pythonanywhere.com`
**WSGI file:** `/var/www/nifty-multi-mvkumar01_pythonanywhere_com_wsgi.py`

**Deploy after any push:**
```bash
cd ~/labs_project && git stash && git pull origin main
# Then reload web app from PA dashboard
```

Never use plain `git pull` on PA — runtime-generated files (token, db) block the merge.

---

Recent production fixes:
- `auth/session_manager.py` auto-reloads `zerodha_token.json` when it changes, so long-running tasks can pick up refreshed tokens without a restart.
- `labs/engine/strategy_runner.py` keeps running outside market hours, performs EOD square-off after cutoff, and logs warmup status for SMA50-based gates.
- `labs/engine/data_loader.py` supports prior-session 1-minute warmup backfill for SMA50 readiness from market open.
- `labs/engine/resampler.py` uses broker-style left-labeled 5-minute resampling for completed-bar evaluation.

## 6. Shared Market-Data Store

Options data is written to a directory **outside both projects** so both Labs and AlphaIMB can consume a single feed.

```
/home/mvkumar01/
├── labs_project/       ← writes spot CSVs here (data/live/)
├── alphaIMB/           ← reads OI data from shared store
└── shared_market_data/
    ├── live/
    │   └── YYYY-MM-DD/
    │       ├── NIFTY_options_1min.csv
    │       ├── BANKNIFTY_options_1min.csv
    │       └── SENSEX_options_1min.csv
    └── archive/
        └── YYYY-MM-DD/
            └── *.parquet.zst  (ZSTD level 3; legacy .parquet.gz fallback)
```

**Canonical schema** (`shared_market_data/live/.../`):
```
timestamp, underlying, tradingsymbol, strike, option_type, expiry,
ltp, bid, ask, oi, volume, spot
```

**Config paths** (`labs_config.py`):
```python
SHARED_MARKET_DIR  = BASE_DIR.parent / "shared_market_data"
SHARED_LIVE_DIR    = SHARED_MARKET_DIR / "live"
SHARED_ARCHIVE_DIR = SHARED_MARKET_DIR / "archive"
```

**`data_loader.load_options_1min()`** reads from `SHARED_LIVE_DIR / trade_date / f"{underlying}_options_1min.csv"`.

**`latest_ltp(options_df, symbol)`** filters by `options_df["tradingsymbol"] == symbol` (column was previously named `symbol`).

**First-time setup on PA:** `mkdir -p /home/mvkumar01/shared_market_data/live`

---

## 7. Coding Conventions

- `pathlib.Path` everywhere — no `os.path`
- `BASE_DIR = Path(__file__).resolve().parent` at top of each script; `LIVE_DIR`, `LOG_DIR` from `labs_config.py`
- All DB functions accept `conn=None`; open+close their own connection when None
- Never re-implement indicator logic inline — use helpers from `indicator_engine.py` (`rsi_value`, `ema_value`, `sma_value`, `adx_values`)
- Never re-implement resampling inline — use `get_resampled_data()` from `resampler.py`
- All P&L metrics use `net_pnl_rs` — never sum `pnl_rs` directly in queries
- New condition types: add to `condition_evaluator.py` `match` block AND to `CONDITION_DEFS` in `labs.js`

---

## 8. Hard Rules

1. **Paper path never places real orders** — `paper_executor.py` (the `/labs/` paper surface) must never call `kite.place_order()`. **Real orders are placed only by the `/live/` package** (`live/`, real-money brokers Angel One / Zerodha) — keep the two surfaces isolated; never route paper through live or vice-versa.
2. **Never modify `data/live/`** — spot CSVs, source of truth.
3. **Never modify `shared_market_data/live/`** — canonical options store, written only by the collector.
4. **Do not run `run_collector.py` or `strategy_runner.py` locally** — PA-only.
5. **`zerodha_creds.json` is tracked** (private repo). Never commit to a public repo.
6. **`zerodha_token.json` is gitignored** — regenerated daily by PA.
7. **Table is `daily_summary`** (singular) — not `daily_summaries`. Use exact name in all queries.
8. **Gitignoring a secret file = re-provision it on PA.** Whenever a file is removed from tracking / added to `.gitignore` for security, a PA `git pull` will DELETE its working copy. ALWAYS flag this prominently (not buried in prose) and create a TODO to recreate the file on PA. Applies to `config/zerodha_creds.json` and any future secret/credential file.

---

## 9. Common Commands

```bash
# Local dev — run Flask dashboard
python app.py
# → http://localhost:5001/labs

# Initialize or migrate DB (safe to re-run)
python storage/db.py

# On PA after code push
cd ~/labs_project && git stash && git pull origin main
# Then reload web app from PA dashboard
```

---

## 9. GitHub

Repo: `https://github.com/mvkumar01/labs_project` (private)

Commit format: `feat:` / `fix:` / `refactor:` / `docs:` prefix.

Gitignored: `data/live/`, `data/archive/`, `logs/`, `storage/labs.db`, `config/zerodha_token.json`, `*.pyc`, `__pycache__/`

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
