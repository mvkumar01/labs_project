# Four-Way Contract P&L Comparison — Implementation Plan

## Objective

Extend `https://labs-mvkumar01.pythonanywhere.com/labs/live` to price the
same Alpha v2.11 signals and timestamps using four option-contract variants:

1. ATM, this week (`nearest_weekly`, strike offset `0`)
2. ITM 200, this week (`nearest_weekly`, strike offset `200`) — current primary
3. ATM, next week (`next_weekly`, strike offset `0`)
4. ITM 200, next week (`next_weekly`, strike offset `200`)

All four variants must use identical strategy entries/exits. Only expiry and
strike selection may differ. P&L is option LTP P&L for one lot after charges.

## Important Existing State

- Current primary tracker: nearest-weekly ITM 200.
- Corrected June 1–19 primary ITM-200 net: `-Rs2,980.30`.
- A read-only in-memory replay of current signals using nearest-weekly ATM:
  `-Rs2,766.32`.
- The older ATM snapshot (`+Rs898.17`) is not comparable because it contains a
  stale pre-fix June 19 replay.
- `storage/db.py` intentionally uses `journal_mode=DELETE` and
  `busy_timeout=5000`. **Do not restore WAL mode.** WAL previously corrupted
  the PythonAnywhere shared DB.
- Production DB was recovered on 2026-06-21. It currently passes
  `PRAGMA integrity_check`, preserves 2,446 trades and 34,433 signals, and has
  archived corrupt originals under `storage/labs.db.corrupt_*` on PA.
- Local June 1–18 shared-store files each contain exactly two valid expiries.
  June 19 PA coverage must be checked before deployment.

## Non-Goals

- Do not change Alpha, range selection, strategy rules, entries, or exits.
- Do not change the real-order live runner's contract selection.
- Do not modify captured market-data CSVs.
- Do not replace the existing `paper_strategy_daily` or
  `paper_strategy_trades` tables; keep them as the nearest-week ITM-200 primary
  view for backward compatibility.

## Files to Change

- `labs/engine/paper_strategy_tracker.py`
- `labs/ui/routes.py`
- `templates/live_strategy.html`
- `tests/test_paper_strategy_tracker_pricing.py`
- Add focused comparison-persistence tests if useful, for example
  `tests/test_paper_contract_comparison.py`

## Variant Definitions

Define one canonical ordered mapping in `paper_strategy_tracker.py`:

```python
CONTRACT_VARIANTS = {
    "near_atm": {
        "label": "ATM — This week",
        "expiry_mode": "nearest_weekly",
        "strike_offset": 0,
    },
    "near_itm200": {
        "label": "ITM 200 — This week",
        "expiry_mode": "nearest_weekly",
        "strike_offset": 200,
    },
    "next_atm": {
        "label": "ATM — Next week",
        "expiry_mode": "next_weekly",
        "strike_offset": 0,
    },
    "next_itm200": {
        "label": "ITM 200 — Next week",
        "expiry_mode": "next_weekly",
        "strike_offset": 200,
    },
}
PRIMARY_VARIANT = "near_itm200"
```

Use `market_data.expiry.select_expiry_code`; do not reimplement expiry parsing.
“This week” means the nearest non-expired captured expiry. “Next week” means
the second non-expired captured expiry.

## New Persistence Tables

Create these lazily inside `_ensure_tables()`:

```sql
CREATE TABLE IF NOT EXISTS paper_contract_daily (
    trade_date       TEXT,
    variant          TEXT,
    expiry_mode      TEXT,
    expiry_code      TEXT,
    strike_offset    INTEGER,
    status           TEXT,
    n_trades         INTEGER,
    gross_rs         REAL,
    charges_rs       REAL,
    net_rs           REAL,
    error             TEXT,
    strategy_version TEXT,
    updated_at       TEXT,
    PRIMARY KEY (trade_date, variant)
);

CREATE TABLE IF NOT EXISTS paper_contract_trades (
    trade_date    TEXT,
    seq           INTEGER,
    variant       TEXT,
    expiry_mode   TEXT,
    expiry_code   TEXT,
    side          TEXT,
    strike        INTEGER,
    entry_ts      TEXT,
    exit_ts       TEXT,
    entry_spot    REAL,
    exit_spot     REAL,
    entry_prem    REAL,
    exit_prem     REAL,
    gross_rs      REAL,
    charges_rs    REAL,
    net_rs        REAL,
    entry_rule    TEXT,
    exit_reason   TEXT,
    PRIMARY KEY (trade_date, seq, variant)
);
```

Status values:

- `priced`: all exact entry/exit quotes were available.
- `no_trade`: strategy produced no trades; net is zero.
- `unavailable`: expiry or an exact quote was missing; net must be `NULL`, not
  zero, and `error` must explain why.
- `open`: current session has a holding mark rather than final EOD exit.

## Pricing Refactor

### 1. Read the option CSV once per day

Replace the nearest-only `_premium_lookup()` with a function that builds both
expiry books in one CSV read. Suggested return shape:

```python
{
    "nearest_weekly": {
        "expiry_code": "26623",
        "prices": {(timestamp_iso, strike, option_type): ltp},
    },
    "next_weekly": {
        "expiry_code": "26JUN",
        "prices": {...},
    },
}
```

Required rules:

- Preserve exact 5-minute marks only: `timestamp.minute % 5 == 0`.
- Never use `resample().last()` or bucket relabelling.
- Select expiry using the `expiry` column and `select_expiry_code()`.
- Fail closed if the requested expiry is absent.
- Keep the same selected expiry from entry through exit.

### 2. Parameterize strike pricing

Change `_price_trade()` to accept `strike_offset`, `expiry_code`, and
`expiry_mode`.

```python
atm = _r50(entry_spot)
strike = atm - strike_offset if side == "CALL" else atm + strike_offset
```

Thus offset `0` is ATM; offset `200` is ITM 200.

### 3. Simulate once, price four times

In `run_day()`:

1. Build Alpha and call `champion_sim.simulate()` once.
2. Build the two expiry price books once.
3. Price the same `sim_trades` for all four variants.
4. Save `near_itm200` into the existing legacy tables.
5. Save all four variants into the new comparison tables.

Do not allow a missing next-week quote to prevent the primary nearest-week
ITM-200 row from saving. Mark only that comparison variant `unavailable`.
The primary variant should retain the existing strict missing-quote behaviour.

Apply `holding`/`eod` exit labelling consistently across all variants.

## UI Route

In `labs/ui/routes.py::live_strategy()`:

1. Keep existing queries for the primary tracker.
2. Query `paper_contract_daily` for the same date range.
3. Build per-variant totals:
   - net P&L after charges
   - charges
   - priced days
   - unavailable days
   - latest expiry code
4. Pivot daily results into:

```python
comparison_by_date[trade_date][variant] = net_rs_or_none
```

5. Query `paper_contract_trades` for the latest session if a latest-contract
   comparison table is desired.

The route must tolerate missing comparison tables during rollout and show a
clear “comparison not backfilled yet” message rather than returning HTTP 500.

## UI Template

In `templates/live_strategy.html`:

- Rename the existing headline card to `Primary — ITM 200, This week`.
- Add a `Contract P&L comparison` section with four cards showing cumulative
  net P&L after charges.
- Each card should show the latest selected expiry code and unavailable-day
  count.
- Add a compact daily comparison table:

| Date | ATM This Week | ITM 200 This Week | ATM Next Week | ITM 200 Next Week |

- Show unavailable values as `—`, never `₹0`.
- Colour positive green and negative red.
- Explain that all four columns use identical signals/timestamps and differ
  only by strike/expiry.
- Continue displaying spot-point P&L separately; never label it option P&L.

## Tests

Update `tests/test_paper_strategy_tracker_pricing.py` to cover:

1. Nearest expiry exact-mark lookup.
2. Next expiry exact-mark lookup.
3. A 09:24 quote is never relabelled to 09:20.
4. CALL ATM strike = rounded ATM.
5. PUT ATM strike = rounded ATM.
6. CALL ITM 200 = ATM - 200.
7. PUT ITM 200 = ATM + 200.
8. Missing exact entry/exit quote raises or marks only that variant unavailable.
9. The same expiry code is persisted for entry and exit.

Add persistence tests using an in-memory SQLite connection:

- Four daily rows are written per trade date.
- Four trade rows are written per strategy trade.
- `near_itm200` agrees with the legacy primary table.
- Re-running a date is idempotent.
- No-trade days create four zero-net `no_trade` daily rows.

Run at minimum:

```bash
python3 -m pytest \
  tests/test_db_journal_mode.py \
  tests/test_paper_strategy_tracker_pricing.py \
  tests/test_expiry_and_session_safety.py \
  tests/test_paper_contract_comparison.py -q
```

## Local Historical Validation

Before commit, run an in-memory backfill for June 1–18 so the production DB is
not touched. Assert:

- 13 dates × 4 daily comparison rows.
- Every trade variant uses the expected strike offset.
- `near_itm200` total matches the current primary total through June 18
  (approximately `+Rs631.29`; use exact DB output as authority).
- All next-week variants are priced or explicitly reported unavailable.

## Git

Commit only intended source/tests. Suggested commit:

```text
feat: compare weekly ATM and ITM paper PnL
```

Push `main` only after tests pass.

## PythonAnywhere Deployment

The normal path is Local -> GitHub `main` -> PA checkout -> backfill -> reload.
The user may need to open a fresh PA Bash console because dormant consoles
return API `412` or expired consoles return `404`.

1. Confirm production DB before mutation:

```sql
PRAGMA journal_mode;   -- must be delete
PRAGMA integrity_check; -- must be ok
```

2. Verify June 19 contains both `nearest_weekly` and `next_weekly` expiries.
   Stop if next-week coverage is missing; do not synthesize premiums.

3. Take a consistent SQLite backup using `Connection.backup()` and verify the
   backup with `PRAGMA integrity_check`.

4. Pull the approved commit with `git pull --ff-only origin main`.

5. Run the targeted tests on PA.

6. Stop only the paper tracker during historical backfill to avoid concurrent
   rewrites. The strategy runner need not be stopped if it is not involved in
   these new tables, but keeping the maintenance window outside market hours is
   preferred.

7. Backfill June 1–18:

```bash
python3 -m labs.engine.paper_backfill labs/engine/june_champion_ranges.json
```

8. Backfill June 19 with the recovered verified range:

```python
{
    "lower": 23800,
    "upper": 24600,
    "bucket": "PC400",
    "direction": "DOWN",
    "vix": 13.24,
    "pc400_v210_biggap": False,
    "skip": False,
}
```

9. Assert:

- 14 dates × 4 comparison daily rows = 56 rows.
- Each priced strategy trade has four comparison trade rows.
- No variant has the wrong strike offset.
- `near_itm200` still equals the legacy primary totals.
- `PRAGMA journal_mode` remains `delete`.
- `PRAGMA integrity_check` remains `ok`.

10. Restart the paper tracker and reload the web app.

11. Wait at least 15 seconds, repeat the DB assertions, then verify the public
    page returns HTTP 200 and renders all four labels and totals.

## Acceptance Criteria

- `/labs/live` visibly reports all four requested P&L variants.
- All variants use identical Alpha trades and exact entry/exit marks.
- ATM/ITM and current-/next-week definitions are explicit on the page.
- Missing next-week data appears as unavailable, never as zero.
- Existing primary ITM-200 output remains backward-compatible.
- Tests pass locally and on PA.
- Production DB remains `journal_mode=delete` with `integrity_check=ok` after
  task restart.
