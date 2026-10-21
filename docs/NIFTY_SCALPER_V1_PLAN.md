# Nifty Scalper V1 — live deployment plan (handoff)

Status: **implemented and validated; rollout approved by Vipin** (2026-10-21). Validation and rollout notes:
`docs/NIFTY_SCALPER_V1_IMPLEMENTATION.md`.
Owner: Vipin. This file is self-contained so another agent (Claude or Codex) can pick it up.

---

## 0. Rules for whoever implements this (non-negotiable)

- This is the **real-money** repo (`C:\Users\vipin\labs_project`). Ask Vipin before every commit, push,
  deploy or restart. Never deploy or restart between 09:15 and 15:30 IST.
  Clock trap: in Git Bash `TZ=Asia/Kolkata date` prints **UTC**. Use `date -u` and add 5:30.
- The working tree carries other sessions' uncommitted edits (`labs/ui/routes.py`,
  `templates/live_strategy.html`, `pa_paper_tracker_loop.py`, `live/engine/champion_sim.py`, …).
  Commit only your own hunks; never `git stash` / `git checkout --` them away.
- Deploy is the PA files API (`/api/v0/user/mvkumar01/files/path/home/mvkumar01/labs_project/...`), not git on PA.
  Before uploading a file, check that PA's copy equals the previous commit's blob (line endings
  ignored); for files other sessions also edit, upload "PA's current copy + only your lines".
  Helper scripts of earlier deploys: `%TEMP%\claude\C--Users-vipin-alphaIMB\428d76a0-…\scratchpad\`
  (`pa_files.py check|put`, `pa.py`, `deploy_nifty_v5.py` as a template). The PA key is in
  `Downloads\PA API Key.txt`; never print it.
- After an upload: reload web app `labs-mvkumar01.pythonanywhere.com`; restart always-on **274885**
  (SENSEX Proposer runner, `pa_proposer_runner.py`). Do **not** touch 253170 (NIFTY alpha runner)
  unless a change requires it. Confirm the runner's boot line shows the new ABI
  (`/var/log/alwayson-log-274885.log`, line `proposer_runner boot | ... abi=...`).
- DRY_RUN first. Vipin arms LIVE himself. No account is switched by the implementer.
- Graphify: the repo has `graphify-out/`; run `graphify query "<question>"` before broad reading.
- Full suite baseline: `python -m pytest -q -p no:cacheprovider tests` → 788 passed, 9 known
  failures (test_exit_retry ×3, market_data_order_routing, order_validity_ioc ×1 + 2 errors,
  tick_stop_double_fire, tick_stop_overlay). Anything else failing is yours.

---

## 1. What Nifty Scalper V1 is (the spec)

The paper book **"Nifty · Sensex V5 signals"** (`proposer_nifty_v5`, labs commit `30a173f`, tab
`/labs/live?tab=proposer_nifty_v5`) taken live. Its replay is the reference:
`labs/engine/proposer_nifty_tracker.py` (read its docstring first).

| Part | Rule |
|---|---|
| Signals | **Unchanged SENSEX Proposer prints** — the existing `PredictorFeed` in `live/proposer_runner.py` (gap regime at the close of the broker's 09:15 SENSEX candle; 5-class on SENSEX 1-min bars + SENSEX chain every 300 s). |
| Engine | `live/engine/proposer_engine.py`, rules of `proposer_dt35_v5` (Sensex V5): entry gate conf > 0.45, regime licence once a day, **with-the-day filter on SENSEX: 75 SENSEX points** (unscaled, SENSEX move vs SENSEX 09:15 close), opposite-print exit, 270 s wait after an exit, recovery book, entry cutoff 15:20, flat 15:25. |
| Day target | **1.5 %** of the trade's entry capital (premium × qty) — day realized + open trade. |
| Point exits | scaled by **ratio = NIFTY 09:15 open / SENSEX 09:15 open** (≈ 0.31): spot target decisive = 100 × ratio × **1.5**, neutral = 40 × ratio × 1.5 (≈ 46 / 19 NIFTY pts), tolerance 1 × ratio; **loss floor = 150 × ratio** (≈ 46 NIFTY pts against the entry spot); **no premium floor** (−99.9 %). Checked on the **NIFTY** spot. |
| Contract | NIFTY options (NFO). Nearest NIFTY weekly; **on NIFTY's own expiry day the next weekly** (holiday-shifted expiries count: 19 Oct 2026 was a Monday expiry). |
| Strike | Sentinel's rule scaled: the shallowest ITM strike at ATM ∓ 50/100/150 (ATM = spot rounded to 50) with \|delta\| ≥ 0.60 (Black-76 IV from the chain's LTP), else the highest-delta liquid one. Same function as the paper book: `proposer_nifty_tracker.pick_strike`. |
| Orders | as the SENSEX runner: entry IOC BUY priced from the ask with the chase cap; exit marketable DAY SELL; DRY_RUN books at the bid. |
| Size | **Open decision (§6, D1).** Paper = ₹12L of premium per entry. Live default proposal = the connection's configured lots × 65, capped at the NIFTY freeze (1,800 qty = 27 lots) with the existing step-down on size rejection. |

Evidence (in-sample, 1 Jul – 21 Oct 2026, PA backfill): +₹6.57L, 72/74 traded days green, worst day
−₹1.86L, max DD ₹3.53L, 90 trades. Research: alphaIMB `research/experiments/2026-09-09_proposer_v1_reverse_engineering/REBUILD.md` §40–42,
`source/nifty_adopt.py`, `source/nifty_book_parity.py` (labs replay = research, 76/76 sessions).

Known behaviour to expect live: on some days the 1.5 % day-target exit, after the spread, lands
under the engine's 90 % bank confirmation (`confirm_bank_tolerance`), so the bot keeps trading and
re-enters at about zero several times (13 Oct 2026: 11 trades). Paper charges cover it; live adds
slippage. See D3.

---

## 2. Design

### 2.1 Ownership and contract
- New strategy version **`proposer_nifty_scalper_v1`** (starts with `proposer` → owned by the Proposer
  runner 274885; `live_runner.py:201` already skips proposer strategies, so the NIFTY alpha runner
  never touches it).
- Add to `live/engine/proposer_engine.py`: `STRATEGY_VERSION_NIFTY_SCALPER_V1`, include it in
  `KNOWN_VERSIONS`; `params_for()` returns the V5 rules with `daily_target_pct=0.015`.
  Point scaling needs the day's ratio, so add `scaled_for_nifty(params, ratio)` (or a `ratio` arg)
  that applies exactly `proposer_nifty_tracker._params` — **move that function into the engine module
  and make the paper tracker import it** (one source of truth).
- `live/live_executor.py`: `PROPOSER_DECISION_ABI` → `"proposer-dt25-live-v11"` with a v11 comment.
  The Configure screen already refuses a Proposer change while the runner reports an older
  `proposer-` ABI (`labs/ui/live_routes.py::_runner_not_ready_for`), and the runner holds entries on
  versions it doesn't know (`pe.KNOWN_VERSIONS`).

### 2.2 Per-connection instrument profile (the main refactor in `live/proposer_runner.py`)
Today the runner is SENSEX-only through module constants (`UNDERLYING, SEGMENT, LOT_SIZE = "SENSEX",
"BFO", 20`, line 46). Introduce a small profile and pass it through:

```python
@dataclass(frozen=True)
class Instrument:
    underlying: str      # "SENSEX" | "NIFTY"
    segment: str         # "BFO" | "NFO"
    lot_size: int        # 20 | int(UNDERLYINGS["NIFTY"]["lot_size"]) (65)
    spot_key: str        # "BSE:SENSEX" | "NSE:NIFTY 50"
    kite_token: int      # 265 | 256265
SENSEX_I, NIFTY_I = ...
def instrument_for(strategy_version) -> Instrument  # NIFTY_I for proposer_nifty_scalper_v1
```
Places that must use the connection's profile instead of the constants:
- `_adapter()` (line ~581): `use_segment(profile.segment, profile.underlying)`; cache key must be
  the segment (it already compares `_proposer_segment`), so a connection switched between
  SENSEX and NIFTY presets re-points its adapter.
- `KiteMarket.quote()` (key `f"{segment}:{symbol}"`) and `KiteMarket.spot()` → take the profile.
  For a NIFTY scalper cycle fetch **both** spots in one `kite.ltp(["BSE:SENSEX", "NSE:NIFTY 50"])`
  call (SENSEX for the with-the-day filter / SessionBars, NIFTY for exits).
- Entry block (lines ~755–826): `freeze_capped_lots(configured, profile.lot_size, profile.underlying)`,
  `qty = lots * profile.lot_size`, telegram texts, `option_chain()` → the NIFTY chain
  (`SHARED_LIVE_DIR/<day>/NIFTY_options_1min.csv`, latest minute) and the NIFTY strike picker.
- `entry_spot` saved in trade state must be the **NIFTY** spot (the floor and spot targets compare
  against it).
- Keep `SessionBars` / `day_move` on **SENSEX** for every profile (the filter is SENSEX's); the
  PredictorFeed is shared and unchanged.

### 2.3 Expiry and strike for NIFTY
- Expiry: from the NIFTY chain's codes pick the nearest expiry ≥ today; if it **is** today, the
  next one. Use `labs/engine/expiry_strangle_tracker.expiry_date_of(code, "NIFTY")` or move it to
  `market_data/expiry.py` (it already decodes NIFTY weeklies and `26OCT`-style monthlies). The
  collector's live CSV must contain the next weekly too — verify on PA (`NIFTY_options_1min.csv`
  has several expiries; `option_chain()` today filters to the nearest — the NIFTY version must not).
- Strike: put the NIFTY picker in `live/engine/proposer_strike.py`
  (`delta_itm_symbol_nifty(chain, side, spot, today, expiry)`) returning
  `(tradingsymbol, strike, typ, info)` like `delta_itm_symbol`, and make
  `proposer_nifty_tracker.pick_strike` call it (paper = live).

### 2.4 The day's ratio
`ratio = NIFTY 09:15 candle open / SENSEX 09:15 candle open`. Live: broker 09:15 candles
(`broker_open_candle` exists for SENSEX token 265 — generalise to a token argument; NIFTY 256265),
fetched after 09:16:10 like the gap rule; fallback the collector's 09:15 bars. No entry before the
ratio is known (the first print is ~09:22, so this never binds in practice). Rebuild the engine
when the day changes: `ProposerConnection` is keyed on (strategy, day target); add the trade date
+ ratio so params are re-scaled each session (and restart-safe: recompute on boot).

### 2.5 Configure screen / labels
- `labs/ui/live_routes.py`: `STRATEGY_PRESETS["nifty_scalper_v1"] = ("proposer", "proposer_nifty_scalper_v1")`,
  `STRATEGY_LABELS["nifty_scalper_v1"] = "Nifty Scalper V1 (Sensex V5 signals on NIFTY options; 1.5% day target)"`.
  `_current_strategy_preset` already maps any proposer version present in `STRATEGY_PRESETS`.
- Lots hint on the Configure page already shows NIFTY lot / freeze sizes.
- Trades are tagged `proposer_nifty_scalper_v1` in `live_trades.strategy` (P&L filters work).

### 2.6 What stays as is (verify, don't rewrite)
Mode machine (DISARMED → DRY_RUN → LIVE_ARMED), gates, idempotent order ledger, reconcile on boot,
daily-loss cap (`lr.check_daily_loss`), kill switch, IOC entry / DAY exit, lot step-down on size
rejection, P&L via `svc.calc_net_option_pnl` (NSE charges — correct for NIFTY).

---

## 3. Tests to add (`tests/test_nifty_scalper_v1.py`)
1. `params_for("proposer_nifty_scalper_v1")` = V5 rules with 1.5 %; scaled params at ratio 0.31 equal
   the paper book's `_params("…", NIFTY_V5, 0.31)` field by field.
2. Strike picker: live `delta_itm_symbol_nifty` == paper `pick_strike` on a synthetic chain; ATM ∓50..150.
3. Expiry: nearest weekly; on NIFTY's expiry day the next weekly; Monday holiday-shifted expiry;
   monthly code `26OCT`.
4. Runner with a fake adapter + fake market (pattern: `tests/test_proposer_live.py` FakeAdapter):
   NIFTY connection gets `use_segment("NFO","NIFTY")`; entry qty = lots × 65 capped at 1,800;
   `entry_spot` = NIFTY spot; exit on NIFTY floor / NIFTY spot target; with-day filter reads SENSEX.
   A SENSEX connection in the same cycle still trades BFO (no cross-talk).
5. ABI v11; Configure: preset listed, refused on an older runner ABI and with an open position.
6. Decision parity on recorded sessions: drive `process_connection` minute by minute in DRY_RUN
   over 3–5 archived sessions (e.g. 2026-10-13, 10-16, 10-19, 10-21) using the archive files as the
   fake market, and compare entries/exits (time ±1 cycle, side, strike, exit rule) with the paper
   book's trades for those days. Differences are expected only from the paper's in-minute price
   model; investigate any side/strike/rule mismatch.

---

## 4. Rollout
1. Build on a branch-free working tree (stage only own hunks), full suite = baseline + new tests.
2. Show Vipin the diff summary; on his go: commit, push, deploy **after 15:30 IST** (files API,
   pre-check each file), reload web app, restart 274885, confirm boot line `abi=proposer-dt25-live-v11`.
3. Vipin selects **Nifty Scalper V1** on a connection's Configure page and arms **DRY_RUN**.
4. DRY_RUN for ≥ 3 sessions. Each evening compare the DRY trades (`/live` Recent Orders /
   `live_trades` with `dry_run=1`) with the paper tab's trades for the same day; write the
   comparison in REBUILD.md (new section).
5. Vipin arms LIVE at 1 lot. Watch the first live day: runner log 274885 `entry timing` lines,
   IOC cancels, fills vs the paper book.

Rollback: Configure → another preset (flat only) or DISARM; code rollback = re-upload the previous
commit's blobs and restart 274885.

---

## 5. Risks / things to check on PA
- **Collector NIFTY chain coverage**: the live CSV must include the next weekly on expiry days and
  strikes ATM ∓150; check a Tuesday's `SHARED_LIVE_DIR/<day>/NIFTY_options_1min.csv` on PA.
- **Latency**: one extra LTP read per cycle; keep it a single `ltp([...])` call. Entry IOC can be
  cancelled on fast moves (seen on SENSEX 19 Oct 09:32) — the paper book assumes fills.
- **Telegram is not configured on PA** (`config/telegram.json` missing) — no alerts for entries,
  cancels or step-downs. Tell Vipin.
- **Angel**: NFO instrument master lookup by Kite symbol (`broker_symbol`) — verify for NIFTY monthly
  (`NIFTY26OCT23000CE`) and weekly (`NIFTY26O2722650CE`-style) symbols.
- **Same broker account** running the NIFTY alpha bot on another connection would share margin and
  positions; reconcile compares only this connection's symbol/qty — confirm with Vipin which account.
- In-sample strategy; 1.5 % re-entry loops (§1); expiry-day contract switch untested live.

---

## 6. Decisions (Vipin, 2026-10-21)
- **D1 Size → configured lots** (× 65, freeze-capped at 27 lots, step-down on size rejection).
- **D2 Day target → editable**: reuse the per-connection field `proposer_day_target_pct`
  (`labs/ui/live_routes.py` V45_DAY_TARGETS / `pe.parse_day_target`, 0.5–10 %) for Nifty Scalper V1 too,
  with **1.5 % as its default** and a "1.5% (tested)" option; extend `pe.DAY_TARGET_VERSIONS` to the
  new version. Only 1.5 % has been backtested on NIFTY — the hint must say so.
- **D3 Re-entry loops → `confirm_bank_tolerance = 0.25`** for Nifty Scalper V1 and the paper book
  `proposer_nifty_v5` (bank the day when the booked exit is >= 75 % of the target; was 90 %). See §8.
- **D4 DRY_RUN connection → Angel** (`3adc…:angel`, currently LIVE on Sensex V5). Switching it is
  Vipin's action on Configure, flat only; it stops Sensex V5 on that account.

## 6b. Original open questions (answered above)
- **D1 Size**: configured lots (proposal) or a fixed rupee capital per entry like the paper book
  (₹12L → lots = capital ÷ (premium × 65))? With configured lots, rupee results differ from the
  paper book but % per trade and the day-target logic are the same.
- **D2 Day target**: fixed 1.5 %, or reuse the Sensex V4/V5 day-target field (`proposer_day_target_pct`)?
- **D3** Accept the near-zero re-entry loops on knife-edge days, or cap trades/day (changes the
  tested strategy; would need a backtest first)?
- **D4** Which connection/account goes to DRY_RUN first.

---

## 7. File map
| File | Change |
|---|---|
| `live/engine/proposer_engine.py` | version constant, KNOWN_VERSIONS, params_for (1.5 %), NIFTY scaling helper |
| `live/engine/proposer_strike.py` | `delta_itm_symbol_nifty` + NIFTY expiry choice |
| `live/proposer_runner.py` | `Instrument` profile; `_adapter`, `KiteMarket`, chain, entry block, ratio, engine rebuild per day |
| `live/live_executor.py` | ABI v11 |
| `labs/ui/live_routes.py` | preset + label |
| `labs/engine/proposer_nifty_tracker.py` | import the shared scaling/strike helpers (paper = live) |
| `tests/test_nifty_scalper_v1.py` (+ ABI asserts in `tests/test_proposer_live.py`, `tests/test_sensex_v45_preset.py`, `tests/test_proposer_v5_dt25.py`) | new / updated |
| alphaIMB `REBUILD.md` | new section with the DRY-vs-paper comparison |

---

## 8. D3 result (2026-10-21, alphaIMB REBUILD.md §43)
Every fix tried gives the same backtest (1 Jul – 16 Oct): bank-confirmation tolerance 0.25 or 0.40, no
confirmation, or no new entry once the day is up 1.0 % / 1.2 % → **+6.56L vs +6.29L, 75 trades vs 88,
97.2 % green days vs 95.8 %, max 3 trades a day vs 10**, worst day / DD unchanged.
**Decided (Vipin, 2026-10-21): `confirm_bank_tolerance = 0.25`** for Nifty Scalper V1 (keeps the check that
a badly slipped live exit does not count as banked), applied to the paper book `proposer_nifty_v5`
too so paper = live (rebuild its backfill after the change).
