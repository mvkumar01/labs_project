# Graph Report - labs_project  (2026-07-08)

## Corpus Check
- 153 files · ~132,604 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1707 nodes · 3426 edges · 112 communities (96 shown, 16 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 27 edges (avg confidence: 0.54)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `6d1cebf7`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 76|Community 76]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 80|Community 80]]
- [[_COMMUNITY_Community 81|Community 81]]
- [[_COMMUNITY_Community 82|Community 82]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 84|Community 84]]
- [[_COMMUNITY_Community 85|Community 85]]
- [[_COMMUNITY_Community 86|Community 86]]
- [[_COMMUNITY_Community 87|Community 87]]
- [[_COMMUNITY_Community 88|Community 88]]
- [[_COMMUNITY_Community 89|Community 89]]
- [[_COMMUNITY_Community 90|Community 90]]
- [[_COMMUNITY_Community 91|Community 91]]
- [[_COMMUNITY_Community 92|Community 92]]
- [[_COMMUNITY_Community 93|Community 93]]
- [[_COMMUNITY_Community 94|Community 94]]
- [[_COMMUNITY_Community 95|Community 95]]
- [[_COMMUNITY_Community 96|Community 96]]
- [[_COMMUNITY_Community 97|Community 97]]
- [[_COMMUNITY_Community 98|Community 98]]
- [[_COMMUNITY_Community 99|Community 99]]
- [[_COMMUNITY_Community 100|Community 100]]
- [[_COMMUNITY_Community 101|Community 101]]
- [[_COMMUNITY_Community 102|Community 102]]
- [[_COMMUNITY_Community 113|Community 113]]
- [[_COMMUNITY_Community 114|Community 114]]
- [[_COMMUNITY_Community 116|Community 116]]
- [[_COMMUNITY_Community 117|Community 117]]
- [[_COMMUNITY_Community 118|Community 118]]
- [[_COMMUNITY_Community 119|Community 119]]
- [[_COMMUNITY_Community 121|Community 121]]
- [[_COMMUNITY_Community 122|Community 122]]

## God Nodes (most connected - your core abstractions)
1. `get_conn()` - 60 edges
2. `AngelAdapter` - 37 edges
3. `get_live_conn()` - 35 edges
4. `process_connection()` - 30 edges
5. `AlphaSignalEngine` - 27 edges
6. `load_options_frame()` - 26 edges
7. `OrderResult` - 23 edges
8. `select_expiry_code()` - 22 edges
9. `ZerodhaAdapter` - 21 edges
10. `run_backtest()` - 20 edges

## Surprising Connections (you probably didn't know these)
- `ExpirySelectionTests` --uses--> `MarketSessionClosed`  [INFERRED]
  tests/test_expiry_and_session_safety.py → collector/spot_collector.py
- `SessionSafetyTests` --uses--> `MarketSessionClosed`  [INFERRED]
  tests/test_expiry_and_session_safety.py → collector/spot_collector.py
- `_Smart` --uses--> `AngelAdapter`  [INFERRED]
  tests/test_angel_read_cache.py → live/brokers/angel.py
- `test_angel_market_data_surface_is_disabled()` --calls--> `AngelAdapter`  [EXTRACTED]
  tests/test_market_data_order_routing.py → live/brokers/angel.py
- `test_order_proxy_fails_closed_when_static_route_missing()` --calls--> `order_proxy()`  [EXTRACTED]
  tests/test_market_data_order_routing.py → live/proxy.py

## Import Cycles
- None detected.

## Communities (112 total, 16 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (62): _at_5min_close(), _best_source(), _build_params(), _cached_option_source_quality(), _can_enter(), _charges(), _close_backtest_position(), DataSource (+54 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (58): Labs Flask application entry point., _account_ref_from_creds(), _adapter_for(), arm(), arm_dry_run(), configure(), connect(), _connection_status_for_refresh() (+50 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (72): get_backtest_bots(), _ensure_tables(), _leg_strike(), _mark_day_unavailable(), pending_dates(), price_basket_trade(), Connection, _quote() (+64 more)

### Community 3 - "Community 3"
Cohesion: 0.09
Nodes (31): ABC, _check_token(), evaluate_exit_rules(), evaluate_rules(), Any, DataFrame, Abstract Strategy base class. All strategies implement entry_signal() and exit_s, Subclasses receive a completed 5-min bar DataFrame (with indicator columns) (+23 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (45): Enum, Exception, all_passed(), arm_dry_run(), arm_live(), _blocked_exit(), build_idem_key(), can_transition() (+37 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (27): AlphaSignalEngine, _denom_guard_test(), _exit_test(), _first(), _may14_pc400_multi_entry_test(), _pc50_crossover_at_25(), Drive the engine through a clean crossover so _enter fires.         alpha_direc, Tier-specific crossover threshold (also used for alpha SL on PC50). (+19 more)

### Community 6 - "Community 6"
Cohesion: 0.31
Nodes (9): collect_options(), _ensure_header(), _parse_symbol(), datetime, Path, Fetches option chain quotes for an underlying and appends rows to the shared 1-m, Extract (strike, option_type, expiry_str) from a Kite tradingsymbol.     Example, Builds instrument list, fetches quotes in batches, appends rows to shared CSV. (+1 more)

### Community 7 - "Community 7"
Cohesion: 0.08
Nodes (27): _after_minute_close(), _bar_minutes_from_midnight(), d2_nearest_below_wall(), exact_mark_cutoff(), find_wall_above(), _local_naive_timestamp(), OHLC, pc400_dn_put_should_skip() (+19 more)

### Community 8 - "Community 8"
Cohesion: 0.25
Nodes (8): _bar_timestamp_now(), _log_spot_sample(), market_session_available(), _now_ist(), datetime, Allow processing only from weekday market open onward.      Post-cutoff proces, Signal/idempotency bucket. Use the current completed minute in IST so a     res, Forward-capture every per-poll spot sample to logs/spot2s_DATE.csv so     tick-

### Community 9 - "Community 9"
Cohesion: 0.12
Nodes (34): SENSEX (BSE) long-option round-trip charges. qty = lots * 20.      Mirrors rou, sensex_round_trip_charges(), backfill(), Guarded historical backfill for the SENSEX-own Alpha paper tracker.  Every reque, _alpha_base_dir(), build_alpha_bars(), compute_abs_alpha(), _ensure_tables() (+26 more)

### Community 10 - "Community 10"
Cohesion: 0.10
Nodes (20): addRow(), _buildParamInputs(), CONDITION_DEFS, _groupSkippedReasons(), initBacktestPage(), LEG_CODES, loadBacktestRanges(), loadMoreSignals() (+12 more)

### Community 11 - "Community 11"
Cohesion: 0.11
Nodes (27): calc_net_option_pnl(), calc_option_charges(), conn_id_for(), get_book_role(), get_config(), get_config_float(), get_config_int(), get_daily_loss_cap() (+19 more)

### Community 12 - "Community 12"
Cohesion: 0.13
Nodes (19): current_net(), merge_desired(), OrderIntent, plan_orders(), Multi-source order manager — net N strategy books onto ONE broker account.  Alph, One source's open long position. qty > 0 (we only buy options)., Per-symbol long quantity summed across sources. ledger: source -> SourcePos|None, broker_net (symbol -> qty) must equal the ledger's per-symbol sum.     Returns ( (+11 more)

### Community 13 - "Community 13"
Cohesion: 0.17
Nodes (25): _book(), _mark(), _recovery_inputs(), _replay(), _segment(), test_entry_spot_stop_then_confirmed_recovery_reenters_same_side(), test_overlay_default_off_preserves_v211_single_segment(), test_require_all_quotes_preserves_existing_v212_rows() (+17 more)

### Community 14 - "Community 14"
Cohesion: 0.15
Nodes (26): DatetimeIndex, _compute_alpha_series(), _expiry_code_to_date(), hybrid_alpha_bars(), latest_spot_1min(), _load_baseline(), _load_live_data(), _market_schedule() (+18 more)

### Community 15 - "Community 15"
Cohesion: 0.13
Nodes (22): _previous_trading_days(), alpha_source(), ContextInputError, day_context(), pc400_in_carve_out(), prev_close(), previous_session_close(), Build the inputs champion_sim needs, from the shared store + alphaIMB data.  Sha (+14 more)

### Community 16 - "Community 16"
Cohesion: 0.08
Nodes (24): For /graphify add and --watch, For /graphify query, For the commit hook and native CLAUDE.md integration, For --update and --cluster-only, /graphify, Honesty Rules, Interpreter guard for subcommands, Part A - Structural extraction for code files (+16 more)

### Community 17 - "Community 17"
Cohesion: 0.08
Nodes (24): For /graphify add and --watch, For /graphify query, For the commit hook and native CLAUDE.md integration, For --update and --cluster-only, /graphify, Honesty Rules, Interpreter guard for subcommands, Part A - Structural extraction for code files (+16 more)

### Community 18 - "Community 18"
Cohesion: 0.15
Nodes (27): r2_signal(), r2_vix_tp_exit(), R2 exit decision: VIX-scaled spot take-profit, NO stop-loss.      side: "call" |, R2 alpha-shell entry/exit (NOT the tiered engine).      Convention matches the m, check_daily_loss(), claim_runner_owner(), _engine_for(), evaluate_signal() (+19 more)

### Community 19 - "Community 19"
Cohesion: 0.08
Nodes (11): BrokerAdapter, MUST raise NotImplementedError until Phase-1 enablement (spec §5.4)., Square off `qty` of `symbol`. Same guard as place_order in Phase 0.          `, Abstract broker surface. Concrete adapters live in this package only.      One, Establish a session from the in-memory creds (held in-memory only)., Cheap auth ping for gate 3 (broker_connected)., Stable account identifier for the isolation gate (spec §6 gate 4)., Available cash/margin snapshot for display. Never returns secrets. (+3 more)

### Community 20 - "Community 20"
Cohesion: 0.09
Nodes (20): 1. What This Is, 2. Directory Structure, 3. Database Schema, 4. Bot Architecture, 5. PythonAnywhere Setup, 6. Shared Market-Data Store, 7. Coding Conventions, 8. Hard Rules (+12 more)

### Community 21 - "Community 21"
Cohesion: 0.11
Nodes (31): account_ref_claimed_by_other(), active_connections(), clear_source_pos(), get_connection(), get_ledger(), get_order_ledger(), get_selected_broker(), get_user() (+23 more)

### Community 22 - "Community 22"
Cohesion: 0.09
Nodes (21): Architecture, Bug fixed during this phase, Bug fixes, Cross-project changes (alphaIMB repo), Current Status: Phase 5 (shared market-data store migration) complete, Delivered, Features added, Files changed (+13 more)

### Community 23 - "Community 23"
Cohesion: 0.10
Nodes (19): 1. What This Is, 2. Directory Structure, 3. Database Schema, 4. Bot Architecture, 5. PythonAnywhere Setup, 6. Coding Conventions, 7. Hard Rules, 8. Common Commands (+11 more)

### Community 24 - "Community 24"
Cohesion: 0.29
Nodes (5): _Kite, _patch_kite(), Legacy/strike-selection fast spot uses Kite data, never Angel execution.  Canoni, test_kite_spot_none_on_failure(), test_kite_spot_parsed()

### Community 25 - "Community 25"
Cohesion: 0.24
Nodes (16): load_options_frame(), option_source_candidates(), DataFrame, Path, Return supported sources in deterministic priority order., Resolve the live CSV or archived Parquet for one session.      Missing data is, Load one session from CSV or archived Parquet without changing rows.      ``co, resolve_options_source() (+8 more)

### Community 26 - "Community 26"
Cohesion: 0.60
Nodes (4): _in_session(), main(), datetime, Always-on launcher for the LIVE paper strategy tracker.  Re-runs the daily paper

### Community 28 - "Community 28"
Cohesion: 0.11
Nodes (4): AngelAdapter, generateSession(client_code, pin, totp) from decrypted creds.          Login is, Call fn(), retrying with exponential backoff while `retryable(exc)`         is T, All nonzero NIFTY net legs: {tradingsymbol: signed_qty}.

### Community 29 - "Community 29"
Cohesion: 0.23
Nodes (15): _marketable_limit(), _order_accepted(), _order_applied(), Nudge a LIMIT price across the spread: BUY pays up, SELL gives up, both     by, Entry-recording gate (D2) — broader than _order_applied.      A live entry mus, Regression tests for the 2026-07-07 late/untracked entry-fill incident.  A LIM, The incident order: broker accepted it (has an id) but it is still     'open' —, Dry-run must behave exactly like the old gate (byte-identical replays). (+7 more)

### Community 30 - "Community 30"
Cohesion: 0.11
Nodes (18): 1. Read the option CSV once per day, 2. Parameterize strike pricing, 3. Simulate once, price four times, Acceptance Criteria, Files to Change, Four-Way Contract P&L Comparison — Implementation Plan, Git, Important Existing State (+10 more)

### Community 31 - "Community 31"
Cohesion: 0.23
Nodes (10): backfill(), Guarded atomic backfill for both inverted SENSEX paper books., _ensure_tables(), _invert_trades(), Connection, SENSEX-own Alpha paper book with option execution side inverted.  Alpha calculat, run_day(), _save() (+2 more)

### Community 32 - "Community 32"
Cohesion: 0.10
Nodes (27): addCell(), applyConnectionStatus(), applyFunds(), applyModeBanner(), applyOpenMtm(), applyTradeHistory(), fmtMoney(), fmtPrice() (+19 more)

### Community 33 - "Community 33"
Cohesion: 0.05
Nodes (84): get_kite(), Loads the stored Zerodha token and returns an authenticated KiteConnect instance, Return a cached, authenticated KiteConnect instance., Force re-load of token (call after token refresh)., reset(), One-time recovery script to force-close all open paper positions.  Run on Python, run(), evaluate_condition() (+76 more)

### Community 34 - "Community 34"
Cohesion: 0.50
Nodes (4): latest_hybrid_alpha(), Return the latest COMPLETED locked hybrid alpha bar, or None for no-trade., get_latest_alpha(), Read-only latest locked hybrid alpha bar from shared market data.

### Community 35 - "Community 35"
Cohesion: 0.50
Nodes (4): create_user(), _hash_passcode(), Hash with bcrypt if available, else PBKDF2-HMAC-SHA256 (stdlib).     Returns an, Register a user. Rejects duplicate username (UNIQUE). Returns user_id.     Rais

### Community 36 - "Community 36"
Cohesion: 0.50
Nodes (3): Canonical reader for Labs' shared options market-data store.  Recent sessions, Raised when a requested shared-market session cannot be loaded., SharedMarketDataError

### Community 37 - "Community 37"
Cohesion: 0.19
Nodes (15): _alpha_in_range(), build_dynamic_range_series(), determine_dynamic_range_from_delta(), DataFrame, Signed-delta alpha across [lower, upper]. Returns (d_pe, d_ce, alpha)., A new (lower, upper) is only adopted when the SAME pair is seen     for 2 consec, Limit the centre shift between consecutive bars to <=100 strike pts.     Width i, For every 5-min bucket in snapshot_df, detect a dynamic Gemini     range from po (+7 more)

### Community 40 - "Community 40"
Cohesion: 0.13
Nodes (14): A1. Install the Angel SDK on PA (this is the current failure), A2. Outbound connectivity / static IP (only if needed), A3. Credentials correctness (common mistake), A4. Verify end-to-end on PA (read-only diagnostic, operator-run), A5. Runner auto-reconnect (so it survives daily token expiry), A. ANGEL ONE — auto-login (no daily manual step) — PRIMARY, do this first, B1. Fix the stale "connected" badge (real bug), B2. Add a one-click "Re-login to Kite" CTA (+6 more)

### Community 41 - "Community 41"
Cohesion: 0.25
Nodes (8): credentials_status(), _fernet(), load_credentials(), Build a Fernet from env LABS_CRED_KEY. Deferred import so the module     loads, Fernet-encrypt the cred dict and persist the ciphertext in     live_credentials, Decrypt and return THIS conn's cred dict IN MEMORY ONLY. Callers must     never, SAFE, echo-able status of which cred fields are set — values masked.     WRITE_, store_credentials()

### Community 42 - "Community 42"
Cohesion: 0.22
Nodes (8): _bars(), _config(), _quotes(), test_hard_eod_exit_at_1525(), test_inverted_crosses_and_zero_reversals(), test_missing_executable_quote_never_falls_back(), test_option_execution_is_entry_ask_and_exit_bid_only(), test_run_day_persists_separate_daily_and_trade_rows()

### Community 43 - "Community 43"
Cohesion: 0.11
Nodes (11): _angel_order_tag(), _live_orders_enabled(), Angel rejects ordertag values >= 20 chars; keep this deterministic., OrderResult, _live_orders_enabled(), Zerodha adapter (secondary) — wraps kiteconnect.KiteConnect.  ★ Permitted to imp, All nonzero NIFTY MIS net legs: {tradingsymbol: signed_qty}., Build a KiteConnect session from THIS user's own encrypted creds.          Falls (+3 more)

### Community 44 - "Community 44"
Cohesion: 0.09
Nodes (28): Pure state-transition for v7.11 drift protective stop.      Inputs are the fou, v711_drift_update(), _as_float(), _csv_ts_to_ist_naive(), evaluate_pc400_spot_trail(), _fast_spot(), get_kite_ltp(), get_kite_spot() (+20 more)

### Community 45 - "Community 45"
Cohesion: 0.14
Nodes (13): Angel One Adapter, Broker Sessions And Credentials, Data Flow, Deployment Sequence, DRY_RUN Behavior, Files That Must Stay Private, Live Trading Architecture, Mode Machine (+5 more)

### Community 46 - "Community 46"
Cohesion: 0.38
Nodes (7): _default_trade_state(), get_trade_state(), Load this conn's trade-state row (creating a default if absent)., Atomic per-conn upsert for restart-safe position state., Reset to flat, preserving the daily per-tier counters., reset_trade_state(), save_trade_state()

### Community 47 - "Community 47"
Cohesion: 0.32
Nodes (11): _py_files(), Path, CI isolation gate — enforces the hard separation between the paper-trading engi, Sanity: confirm the SDK truly is isolated — no direct SDK import/order     call, Best-effort removal of line comments and string/docstring bodies so a     patte, _read(), _strip_comments_and_strings(), test_brokers_dir_is_the_only_sdk_holder() (+3 more)

### Community 48 - "Community 48"
Cohesion: 0.23
Nodes (9): Path, _price_trade() without explicit offset uses ITM_DISTANCE=200., All rows in a book belong to a single expiry code — entry and exit never mix., test_build_price_books_nearest_expiry_at_exact_alpha_mark(), test_build_price_books_never_relabels_off_grid_row(), test_build_price_books_next_expiry_at_exact_alpha_mark(), test_build_price_books_same_expiry_code_for_entry_and_exit(), test_price_trade_default_offset_is_itm200() (+1 more)

### Community 49 - "Community 49"
Cohesion: 0.18
Nodes (7): ensure_schema(), Idempotent live_* schema init (delegates to storage.live_db)., init_live_db(), Connection, Live-trading schema initialisation (live_* tables).  This module is part of th, Create all live_* tables if they don't exist. Call once at runner/app startup., test_replay_cursor_survives_trade_state_reset()

### Community 50 - "Community 50"
Cohesion: 0.18
Nodes (10): Account Isolation, Architecture, Bots Live Feature Spec, DRY_RUN, Gates, Hard Constraints, Live Enablement, Mode Machine (+2 more)

### Community 51 - "Community 51"
Cohesion: 0.36
Nodes (10): backfill_day(), _backup_existing(), main(), datetime, Path, Backfill spot 1-min OHLC CSVs from Kite historical_data.  Earlier spot files wer, Return up to n weekday dates ending at end_date (inclusive, IST-naive)., _spot_csv_path() (+2 more)

### Community 52 - "Community 52"
Cohesion: 0.11
Nodes (22): backfill(), Backfill the paper tracker for past days using champion ranges from a JSON.  T, Preflight every date, validate benchmarks, then publish every date.      The pre, _build_price_books(), _ensure_tables(), _insert_comparison_daily(), _premium(), _price_trade() (+14 more)

### Community 53 - "Community 53"
Cohesion: 0.13
Nodes (15): configure_outbound_proxy(), _env_first(), order_proxy_url(), Helpers for outbound proxy configuration for the live stack.  Static-IP proxy po, A live order was attempted without the mandatory static-IP route., Static-IP proxy URL reserved for ORDER PLACEMENT only.      Reads the same env v, Legacy launcher hook retained as an intentional no-op.      Data/auth/position/o, StaticOrderProxyRequired (+7 more)

### Community 54 - "Community 54"
Cohesion: 0.33
Nodes (3): r2_tp_points(), VIX-scaled take-profit distance (points). NaN/None -> high-VIX TP (safe:     wid, Unit tests for the R2 book pure logic (range + VIX-scaled TP exit).  No live dat

### Community 55 - "Community 55"
Cohesion: 0.22
Nodes (8): graphify reference: extra exports and benchmark, Step 6b - Wiki (only if --wiki flag), Step 7 - Neo4j export (only if --neo4j or --neo4j-push flag), Step 7a - FalkorDB export (only if --falkordb or --falkordb-push flag), Step 7b - SVG export (only if --svg flag), Step 7c - GraphML export (only if --graphml flag), Step 7d - MCP server (only if --mcp flag), Step 8 - Token reduction benchmark (only if total_words > 5000)

### Community 56 - "Community 56"
Cohesion: 0.22
Nodes (8): graphify reference: extra exports and benchmark, Step 6b - Wiki (only if --wiki flag), Step 7 - Neo4j export (only if --neo4j or --neo4j-push flag), Step 7a - FalkorDB export (only if --falkordb or --falkordb-push flag), Step 7b - SVG export (only if --svg flag), Step 7c - GraphML export (only if --graphml flag), Step 7d - MCP server (only if --mcp flag), Step 8 - Token reduction benchmark (only if total_words > 5000)

### Community 57 - "Community 57"
Cohesion: 0.24
Nodes (10): champion_target(), datetime, Replay-to-now decision engine for the live runner.  On every new exact-mark alph, Reconcile final position plus newly closed canonical segments.      A target-onl, Compare the replay target to the bot's actual position -> one action.      curre, Range + cell context from the backfill override or the locked hybrid     state., Replay completed bars up to `now_ist` and return the target position.      Retur, reconcile() (+2 more)

### Community 58 - "Community 58"
Cohesion: 0.44
Nodes (8): audit(), _connect(), invalid_trade_text_rows(), main(), Connection, Path, quarantine(), Semantic SQLite audit and safe quarantine for malformed trade rows.  SQLite's ``

### Community 59 - "Community 59"
Cohesion: 0.56
Nodes (8): _mark(), _priced_book(), _replay(), _signal(), test_missing_executable_quote_never_uses_ltp_fallback(), test_nifty_signal_prices_same_side_sensex_atm_ask_in_bid_out(), test_no_trade_day_persists_without_loading_sensex_book(), test_require_all_quotes_fail_closed_preserves_existing_rows()

### Community 62 - "Community 62"
Cohesion: 0.08
Nodes (34): collect_futures(), _ensure_header(), _futures_csv_path(), datetime, Path, Fetches the just-completed 1-min OHLCV candle for the nearest-expiry futures con, Fetch the just-completed 1-min OHLCV candle for the nearest-expiry future and, get_current_future() (+26 more)

### Community 64 - "Community 64"
Cohesion: 0.36
Nodes (7): main(), migrate_file(), DataFrame, Path, Migrate legacy GZIP Parquet shared-market archives to ZSTD level 3.  The desti, Write every Parquet column with Fastparquet ZSTD level 3., write_zstd_level3()

### Community 65 - "Community 65"
Cohesion: 0.61
Nodes (7): _bar(), _state(), test_non_pc400_position_does_not_use_trail(), test_pc400_call_gap_down_high_vix_trail_arms_then_fires(), test_pc400_call_gap_up_high_vix_does_not_use_trail(), test_pc400_put_gap_up_high_vix_uses_symmetric_trail(), test_trail_snapshot_recovers_peak_after_restart()

### Community 66 - "Community 66"
Cohesion: 0.14
Nodes (25): backfill(), _override_from_context(), Guarded atomic historical rebuild for the Alpha v2.12 paper ledger., Recover immutable day inputs before replacing historical v2.12 rows., _stored_context_ranges(), AlphaV212InputError, build_executable_book(), _ensure_tables() (+17 more)

### Community 67 - "Community 67"
Cohesion: 0.23
Nodes (9): Position, Broker adapter ABC + value objects (spec §5.3).  ★ This module and its concret, _conn(), FakeAdapter, _place_exit(), test_live_exit_allows_matching_long_position(), test_live_exit_blocks_when_broker_is_flat(), test_live_exit_blocks_when_broker_position_is_short() (+1 more)

### Community 68 - "Community 68"
Cohesion: 0.33
Nodes (6): _adapter(), Angel adapter short-TTL read cache (rate-limit defense, 2026-07-07).  Repeated p, _Smart, test_cache_expires_after_ttl(), test_invalidate_after_order_forces_fresh_read(), test_repeated_reads_collapse_to_one_call()

### Community 69 - "Community 69"
Cohesion: 0.14
Nodes (26): Build v2.11 trades once, before any option-contract pricing.      Cross-index pa, Historical replay inputs are missing or incomplete.      This is distinct from a, replay_champion_signals(), ReplayInputError, backfill(), Guarded atomic backfill for NIFTY v2.11 signals on SENSEX ATM options., _ensure_tables(), Connection (+18 more)

### Community 70 - "Community 70"
Cohesion: 0.25
Nodes (8): add_day_pnl(), get_day_pnl(), Public wrapper for UI routes that need the live trading date., Accumulate realized PnL into the bucket matching the trade's mode.      `reali, Completed live_trades for one user's selected broker connection.      Date fil, set_day_halted(), _today_ist_iso(), trade_history()

### Community 71 - "Community 71"
Cohesion: 0.20
Nodes (7): Indian NIFTY-options round-trip charges (discount-broker model).  Used by the, Return the charge breakdown + total ₹ for one long-option round trip., round_trip_charges(), Basket replay: v2.11 signal segments re-priced as multi-leg structures.  Leg pri, Adding a basket must re-open already-replayed days so refresh fills the     new, test_day_replayed_under_old_basket_set_is_pending_again(), test_long_synthetic_pricing()

### Community 72 - "Community 72"
Cohesion: 0.33
Nodes (5): For /graphify explain, For /graphify path, graphify reference: query, path, explain, Step 0 — Constrained query expansion (REQUIRED before traversal), Step 1 — Traversal

### Community 73 - "Community 73"
Cohesion: 0.33
Nodes (5): For /graphify explain, For /graphify path, graphify reference: query, path, explain, Step 0 — Constrained query expansion (REQUIRED before traversal), Step 1 — Traversal

### Community 75 - "Community 75"
Cohesion: 0.33
Nodes (6): _build_adapter(), _connect_adapter(), _ensure_connected_adapter(), Construct (not connect) a broker adapter for this connection. Creds are     dec, Connect/reconnect one adapter and persist the real broker status., Return a connected adapter, reconnecting stale cached sessions.

### Community 76 - "Community 76"
Cohesion: 0.40
Nodes (4): June 2026 executable option replay notes, NIFTY executable replay, Question, SENSEX execution of NIFTY v2.11 signals

### Community 77 - "Community 77"
Cohesion: 0.27
Nodes (10): archive_shared_market(), archive_today(), purge_old_files(), purge_old_shared_market(), Path, Labs EOD maintenance script. Run at 15:40 IST daily (PA scheduled task).  1., Converts each {UNDERLYING}_options_1min.csv in SHARED_LIVE_DIR/<trade_date>/, Remove shared live date directories older than keep_days. (+2 more)

### Community 80 - "Community 80"
Cohesion: 0.60
Nodes (4): Connection, _seed_conn(), test_missing_locked_state_preserves_existing_result(), test_missing_replay_input_preserves_existing_result()

### Community 81 - "Community 81"
Cohesion: 0.50
Nodes (3): For /graphify add, For --watch, graphify reference: add a URL and watch a folder

### Community 82 - "Community 82"
Cohesion: 0.50
Nodes (3): For git commit hook, For native CLAUDE.md integration, graphify reference: commit hook and native CLAUDE.md integration

### Community 83 - "Community 83"
Cohesion: 0.50
Nodes (3): For --cluster-only, For --update (incremental re-extraction), graphify reference: incremental update and cluster-only

### Community 84 - "Community 84"
Cohesion: 0.50
Nodes (3): For /graphify add, For --watch, graphify reference: add a URL and watch a folder

### Community 85 - "Community 85"
Cohesion: 0.50
Nodes (3): For git commit hook, For native CLAUDE.md integration, graphify reference: commit hook and native CLAUDE.md integration

### Community 86 - "Community 86"
Cohesion: 0.50
Nodes (3): For --cluster-only, For --update (incremental re-extraction), graphify reference: incremental update and cluster-only

### Community 87 - "Community 87"
Cohesion: 0.22
Nodes (13): _is_rate_limited(), _is_transient_read(), Angel One adapter (PRIMARY broker) — wraps SmartApi.SmartConnect.  ★ Permitted t, Angel gateway throttle. The request was REJECTED before processing, so     nothi, Retryable for idempotent READS only: throttle or a garbled JSON body     (Angel, _adapter(), Angel bounded backoff/retry on throttling (rate-limit defense, 2026-07-07).  Rea, Order path uses _is_rate_limited: a parse-only error must NOT retry, so a     po (+5 more)

### Community 113 - "Community 113"
Cohesion: 0.47
Nodes (4): Render smoke test for the Baskets tab: the template must render with empty state, _render(), test_baskets_tab_renders_empty_state(), test_baskets_tab_renders_populated()

### Community 114 - "Community 114"
Cohesion: 0.36
Nodes (8): _Adapter, _patch(), Funds-aware strike fallback: ITM200 first; step 50 pts cheaper until the premium, test_funds_read_failure_proceeds_at_itm200(), test_itm200_when_funds_suffice(), test_never_deeper_otm_than_100(), test_nothing_affordable_skips_entry(), test_steps_down_to_affordable_strike()

### Community 116 - "Community 116"
Cohesion: 0.33
Nodes (6): _labs_spot_ohlc(), latest_completed_ohlc_minute(), ohlc_by_minute(), Timestamp key for the newest completed labs one-minute spot candle.      The liv, {'HH:MM': (open,high,low,close)} for the trade_date.      SOURCE PRIORITY (labs, 1-min index spot OHLC from the labs collector store — the single source.      Re

### Community 117 - "Community 117"
Cohesion: 0.12
Nodes (22): build_option_symbols(), _get_instruments(), get_strike_range(), Builds the list of option instrument tokens to fetch for a given underlying + sp, Return a list of Kite instrument symbols (strings) for all CE+PE within the, _round_strike(), _resolve_contract_from_options(), Paper trading executor. Writes fills to SQLite — no real orders are sent. (+14 more)

### Community 118 - "Community 118"
Cohesion: 0.33
Nodes (6): dtime, eod_watchdog(), _om_enabled_sources(), _om_ledger_to_sourcepos(), _process_om(), Independent EOD square-off trigger — True once at/after the cutoff.     Runs ev

### Community 119 - "Community 119"
Cohesion: 0.50
Nodes (5): build_sim_inputs(), _gemini_adf(), DataFrame, Gemini-c2 (confirm2) dynamic-range alpha frame — faithful port of     v79_v281_i, (snapshot_df, adf, ce_map, pe_map) for champion_sim from the alpha_hybrid     pi

### Community 121 - "Community 121"
Cohesion: 0.33
Nodes (4): _ist_date_of(), PA always-on entry. Boots the live schema, then loops forever (or     `max_cycl, IST calendar date (YYYY-MM-DD) of an ISO timestamp, or None., run()

## Knowledge Gaps
- **188 isolated node(s):** `meta`, `CONDITION_DEFS`, `TIMEFRAMES`, `LEG_CODES`, `SECTION_KEY` (+183 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **16 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `get_conn()` connect `Community 2` to `Community 33`, `Community 66`, `Community 69`, `Community 9`, `Community 77`, `Community 52`, `Community 117`, `Community 31`?**
  _High betweenness centrality (0.061) - this node is a cross-community bridge._
- **Why does `AngelAdapter` connect `Community 28` to `Community 1`, `Community 67`, `Community 68`, `Community 43`, `Community 44`, `Community 19`, `Community 53`, `Community 87`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._
- **Why does `BrokerAdapter` connect `Community 19` to `Community 3`, `Community 67`, `Community 43`, `Community 87`, `Community 28`?**
  _High betweenness centrality (0.022) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `AngelAdapter` (e.g. with `BrokerAdapter` and `OrderResult`) actually correct?**
  _`AngelAdapter` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `meta`, `Labs Flask application entry point.`, `Loads the stored Zerodha token and returns an authenticated KiteConnect instance` to the rest of the system?**
  _540 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.07145501666049611 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.0625 - nodes in this community are weakly interconnected._