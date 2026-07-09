# Live v2.12 Parity Runbook — architecture, invariants, and root-causing

**Deployed:** 2026-07-08 evening, labs `main@760ef67` (branch `parity-rework`),
uploaded file-by-file to PA + runner (always-on task **253170**) restarted +
web app reloaded. PA's *git* may still be behind (`6d1cebf`) with the new code
present as working-tree uploads — `git stash && git pull origin main`
reconciles; the pulled content is identical.

This document exists so a fresh session can root-cause a live incident without
re-deriving the design. Read this FIRST, then the code.

---

## 1. Architecture in one paragraph

Live v2.12 takes **exactly the paper tracker's trades**. Every decision comes
from the canonical **recovery-enabled champion replay**
(`champion_decider.champion_target(enable_entry_spot_recovery=True)`) run over
completed 1-min OHLC — the same engine, flags, and data the paper tracker
(`labs/engine/alpha_v212_tracker.py`) uses. The runner turns replay *events*
into broker orders via a persisted cursor (`champion_closed_count`). A **tick
overlay** (per ~2s poll) only ACCELERATES the entry-spot stop's execution when
the live Kite spot crosses the replay's anchored barrier — it never makes its
own decisions and never re-enters. "Fast out, patient back in."

**Why this design (2026-07-08 incident):** the previous hybrid ran the replay
*without* recovery for entries while a tick layer owned stops — a third
strategy whose trade stream diverged from paper by midday (fresh mid-day
anchor 24226.95 vs paper's anchored 24223.95 → a stop held 8 min past paper's
exit, −29 premium pts; plus missed trades). Any input/semantics divergence
between live and paper compounds; reconcilability beats stop latency.

## 2. Event flow per poll (~2s), `live/live_runner.py :: process_connection`

```
state/position reconcile (broker truth in LIVE)
  → TICK-STOP OVERLAY  (before the event gate; every poll)
      open + use_champion + v212_recovery
      anchor = st.entry_spot (canonical), tick = _fast_spot() (Kite→CSV)
      CALL: tick <= anchor / PUT: tick >= anchor
        → EXIT at market (reason ENTRY_SPOT_SL_TICK)
        → reset_trade_state (PRESERVES champion_* cursor fields)
        → cursor NOT advanced (the replay's own event acks while flat)
  → event clock: alpha_key = (alpha ts, alpha, latest_completed_ohlc_minute)
      unchanged key → return (dedup, alpha_seen)
  → champion_target(recovery=True, live_execution_spot=_fast_spot())
      = full-day replay; returns position/entry_spot/n_closed/
        last_closed_reason (+ UNAVAILABLE on transient input failure)
  → cursor adoption (first observation of the date)
  → reconcile_replay_event(target, side, closed_count_seen)
      n_closed > seen while OPEN → EXIT (canonical stop/close event)
      already flat → _advance_champion_cursor (ack, no order)
  → anchor self-heal: st.entry_spot ← target.entry_spot when open+same side
  → ENTER/EXIT arms → _route_order → place_idempotent (gates, idem keys)
```

## 3. Invariants (deliberate design — do NOT "fix" these)

1. **Angel is never a data source.** `AngelAdapter.get_spot/get_ltp/quote`
   raise by design. All pricing = Kite (`get_kite_ltp`), spot = Kite→1-min CSV
   (`_fast_spot`). Tests: `test_market_data_order_routing.py`.
2. **Position symbols from the Angel book are Angel-format**
   (`NIFTY14JUL2624400PE`). Kite cannot price those directly —
   `kite_symbol_for()` (live_runner) maps to the Kite tradingsymbol via the
   collector chain by (strike, type, expiry-date). If exits ever defer with
   "no Kite LTP", check this mapping FIRST (see §5.2).
3. **Static-IP proxy is fail-closed for ALL orders including exits**
   (`live/proxy.py order_proxy` raises `StaticOrderProxyRequired`;
   `gate_static_order_proxy` blocks entries too, so a position can't open
   un-exitable). Proxy URL comes from `config/live_env.json` on PA
   (`LIVE_OUTBOUND_PROXY_URL`/`QUOTAGUARDSTATIC_URL`) loaded by
   `pa_live_runner._load_private_env`.
4. **Stop valuation = detection candle's CLOSE, execution timestamp = next
   1-min mark** (production model, commit 81def99). Keeps paper history
   byte-continuous. `champion_sim.simulate` recovery block; test:
   `test_alpha_v212_tracker.py::test_entry_spot_stop_then_confirmed_recovery_reenters_same_side`.
5. **Recovery re-entry is canonical only** — a completed 1-min bar must touch
   the level AND close favourable; fill = the executable next-mark quote
   (honest fill; the anchored-level fill was proven unearnable —
   see memory `v212-honest-fill-revalidation`: 60% of the anchored edge was
   artifact). The tick overlay NEVER re-enters.
6. **The cursor is the only event memory.** `champion_closed_count` (persisted
   in `live_trade_state`, written ONLY via `_advance_champion_cursor`) tells
   the runner which canonical closed segments were already consumed.
   `champion_last_event_id` is stored but not compared (kept for telemetry).
   `reset_trade_state` PRESERVES the champion_* fields — required so a tick
   exit doesn't cause the replay's event to re-fire an EXIT.
7. **Transient replay-input failures return `UNAVAILABLE`, never None** →
   reconcile HOLDs. A None target means a genuine no-trade day (SKIP/unlocked).

## 4. Performance layer (champion_inputs)

mtime/size-keyed caches (`_LABS_OHLC_CACHE`, `_LATEST_MINUTE_CACHE`,
`_OHLC_BY_MINUTE_CACHE`, `_VERIFIED_OPEN_CACHE`, `_PREV_CLOSE_CACHE`):
unchanged files cost one stat(); the collector's per-minute write invalidates.
`_labs_spot_ohlc` is the test seam — sig computed separately (`_labs_sig`);
when the source file is absent (monkeypatched tests) memoization is skipped.
`tests/conftest.py` clears all caches around every test.

## 5. Failure modes → diagnosis → fix

Logs: PA always-on log `/var/log/alwayson-log-253170.log` (via PA files API).
DBs: `storage/live.db` (live_orders/live_trades/live_trade_state),
`storage/labs.db` (alpha_v212_daily/trades = paper canonical).

### 5.1 Live diverges from paper again (different trades/anchors)
- Compare `live_trades` vs `alpha_v212_trades` for the date; compare
  `st.entry_spot` (live_trade_state) vs paper segments' `entry_spot`.
- Grep log for `anchor synced to replay` (self-heal fired = late-data anchor
  revision — expected, benign) and `champion conn=... target=` lines: the
  target's entry_spot IS the canonical anchor.
- If sequences differ: check both books run the SAME flags — live must call
  champion_target with `enable_entry_spot_recovery=True` (v2.12 conns);
  `strategy_version` config = "v2.12", `decision_engine` = "champion_replay".

### 5.2 Exits defer / position stuck ("no Kite LTP ... order intent deferred")
- Expected only during a genuine Kite outage (accepted residual risk).
- If Kite is healthy: `kite_symbol_for` failed to map. Verify the collector
  chain CSV exists (`~/shared_market_data/live/<date>/NIFTY_options_1min.csv`)
  and contains the strike; check `market_data.expiry.expiry_code_from_symbol`
  decodes the candidate symbols. Emergency manual exit: broker app/terminal —
  then the broker-flat reconcile clears DB state automatically.

### 5.3 Orders blocked with gate failure `static_order_proxy`
- `config/live_env.json` lost its proxy URL, or env not loaded (check
  pa_live_runner boot). Restore the key + restart task 253170. This gate
  failing loudly is by design (never trade un-exitable).

### 5.4 Double exit after a tick stop (should be impossible — cursor bug class)
- Symptom: `ENTRY_SPOT_SL_TICK` exit, then a second broker EXIT attempt from
  the champion arm (blocked by `_verify_matching_long_before_exit` →
  NO_LONG_POSITION, but noisy).
- Check `_advance_champion_cursor` call sites (must be the ONLY writer) and
  that `reset_trade_state` still preserves champion_* fields.

### 5.5 Spurious flatten of a healthy position
- Grep target lines: was `position: UNAVAILABLE` (HOLD ✓) or a real FLAT?
- A FLAT with `context_error` set = new unhandled input-failure path in
  champion_target — extend the UNAVAILABLE returns.

### 5.6 Runner slow / falls behind (poll gap > ~3s in log timestamps)
- Caches missing? Grep champion_inputs caches present; check PA CPU quota.
- The replay runs once per completed 1-min candle; if it runs every poll,
  the event key broke — inspect `latest_completed_ohlc_minute` (None → key
  degenerates; check labs spot CSV exists for today).

### 5.7 Whipsaw churn (many tick stops)
- Expected behavior is bounded by canonical re-entry (bar close confirm).
- Real fills/charges telemetry: `live_trades` reason='ENTRY_SPOT_SL_TICK'.
- Tuning levers (research first! never live-first): buffer study memory
  `v212-honest-fill-revalidation`, spot2s logs `logs/spot2s_<date>.csv`.

## 6. Verification (run any evening)

```sql
-- live vs paper, per date (live.db + labs.db)
SELECT seq, side, entry_ts, exit_ts, entry_spot, exit_reason
  FROM alpha_v212_trades WHERE trade_date='<date>' ORDER BY seq;      -- paper
SELECT side, symbol, entry_time, exit_time, reason, net_pnl
  FROM live_trades WHERE dry_run=0 AND exit_time LIKE '<date>%';      -- live
-- state: SELECT * FROM live_trade_state; (anchor, cursor, champion_trade_date)
```
Segments should correspond 1:1 (live exits may lead paper's by <60s when the
tick overlay fired; reason ENTRY_SPOT_SL_TICK ↔ paper ENTRY_SPOT_SL).

## 7. Rollback

Previous build = `6d1cebf` (the hybrid — known to diverge from paper; only
roll back for a hard runtime failure, not for divergence):
`git checkout 6d1cebf -- live/ labs/engine/ storage/live_db.py` locally,
upload those files to PA, restart task 253170. Or on PA:
`git stash && git checkout 6d1cebf` (then restart). Re-deploy forward with
`git checkout main`.

## 8. Test map

- `tests/test_v212_live_parity.py` — event clock, cursor persistence/restart,
  contract entry_spot parity.
- `tests/test_alpha_v212_tracker.py` — sim-level stop valuation + confirmed
  recovery semantics (the canonical numbers test).
- `tests/test_tick_stop_overlay.py` — overlay exit + cursor preservation +
  `_fast_spot` zero-arg regression.
- `tests/test_exit_symbol_and_unavailable.py` — Angel→Kite symbol mapping,
  UNAVAILABLE-not-None.
- `tests/test_market_data_order_routing.py` — no-Angel-data + fail-closed
  proxy invariants.
- `tests/test_champion_inputs_cache.py` — cache invalidation on file change.
- Known env-only failures: `tests/test_shared_store_archive.py` (needs
  `fastparquet`, not installed locally).

## 9. History pointers

Commits: user WIP `bc6daeb`+`836dd34` → fixes `eccd0d1` → perf `3b18562` →
overlay `32767a4` → cleanup `e59b8a6` (merged `760ef67`). Review that found
the 5 bugs: session 2026-07-08 (10 findings; 4 confirmed + `_fast_spot(adapter)`
TypeError found during the build). Memory files: `live-parity-tick-overlay`,
`v212-honest-fill-revalidation`.
