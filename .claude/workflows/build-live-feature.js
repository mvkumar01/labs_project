export const meta = {
  name: 'build-live-feature',
  description: 'Build the live-trading feature on the labs shell (Angel+Zerodha), reusing Bot A engine. DRY-RUN ONLY — place_order guarded; no real orders possible. Harden spec -> build -> verify. NO commit/push/deploy.',
  phases: [
    { title: 'Spec', detail: 'fold operator refinements into a build-ready hardened spec (the contract)' },
    { title: 'Build', detail: 'sequential layers: DB -> engine -> web/UI -> launcher/CI gate' },
    { title: 'Verify', detail: 'isolation + dry-run-guard + grep-gate + render checks' },
  ],
}

const LABS = 'C:/Users/vipin/labs_project'
const BOT = 'C:/Users/vipin/Nifty_Bots_Python'

// ───────────────────────────────────────────────────────────────────
phase('Spec')

const spec = await agent(
  `Harden ${LABS}/BOTS_LIVE_FEATURE_SPEC.md into a BUILD-READY contract. Read: the existing spec, ${LABS}/CLAUDE.md (HARD RULE #1: paper-only — paper_executor.py must never call kite.place_order; the live module must be import-isolated from it), and the Bot A engine to REUSE (${BOT}/bots/bot_a/execution.py, signal.py, runner.py).

Fold in ALL of these operator-mandated requirements and define CONCRETE interfaces for each (so build agents have an unambiguous contract):
1. 3-mode state machine DISARMED -> DRY_RUN -> LIVE_ARMED, default DISARMED (persisted in live_config).
2. Web UI NEVER places orders — only the always-on live runner places/exits. Routes mutate DB/config only.
3. Broker-API boundary: ONLY live/brokers/* may call external broker order SDKs. No order-API call in labs/*, live_routes.py, live_service.py, live_runner.py.
4. Startup reconciliation, block-on-mismatch: runner compares DB position vs actual broker position before any signal; mismatch -> block new trades + warn on /live/status.
5. Idempotency key format: conn_id:trade_date:strategy_version:bar_timestamp:action:side:entry_rule:intent_seq (ledger via INSERT OR IGNORE).
6. Brokers: Angel One + Zerodha ONLY (Angel primary = account isolation from Bot A's Zerodha). Drop XTS/Bull Force.
7. Explicit armed flag + 6 pre-trade gates, ALL must pass before any live order: mode==LIVE_ARMED, kill_switch==0, broker connected, account-isolation passed, daily-loss not breached, lots<=2.
Plus: encrypted-at-rest creds (Fernet, key from PA env var, stored under already-gitignored storage/state/, TOTP/PIN write-only never echoed); phased rollout Phase0 dry-run -> Phase1 1-lot -> Phase2 2-lot ceiling.

8. MULTI-USER (generic, N users) — PRIMARY NEW REQUIREMENT; it SUPERSEDES any single-user/single-account assumption in the existing spec. Rewrite the affected sections, do not merely append.
   - Generic N-user system: any number of users register and log in independently. NOT a fixed/hard-coded pair. Replace the old single shared-passcode gate.
   - Per-user identity: a live_users table (user_id PK, username UNIQUE, passcode_hash via bcrypt/argon2, created_at). Auth gate verifies via hmac.compare_digest; session carries user_id; app.secret_key=secrets.token_hex(32) from env. register/login/logout routes. Fails closed.
   - Per-user broker connections: each user connects their OWN broker independently and simultaneously (e.g. user A on Angel One, user B on Zerodha, at the same time). conn_id is scoped per user (derive from user_id + broker). live_broker_connections gains a NOT NULL user_id column.
   - Per-user STATE ISOLATION (the crux): mode (DISARMED/DRY_RUN/LIVE_ARMED), kill_switch, lots, armed flag, daily_loss, and reconcile-block status are ALL per-user/per-connection, NEVER global. One user arming/killing/configuring MUST NOT affect another. Redesign live_config from a global key-value store into per-user/per-conn scoping (composite PK (user_id, key) or a user_id column). live_orders, live_idempotency_ledger, and any live_trades/live_trade_state/live_day_pnl all carry user_id/conn_id and are always queried scoped to the owner.
   - Idempotency key stays user-scoped (conn_id already encodes the user) so two users' identical signals never collide.
   - Runner: live_runner iterates over ALL active user connections, each evaluated independently with its OWN mode, 6 gates, reconcile, daily-loss cutoff, kill-switch, EOD square-off, and trade state. A per-connection runner-owner claim prevents double-processing.
   - Account-isolation gate (gate 4) extended: each user's broker account must be isolated from Bot A's Zerodha account AND from every other user's connected account (no two users may share a broker account).
   - UI scoping: every /live route and the dashboard/status/configure pages show and mutate ONLY the logged-in user's own connection, config, and orders. A user can never see or control another user's state.

Define interfaces for: live_* DB tables (incl. live_users + user_id scoping on every live_* table); the live/ package modules + function signatures; Flask routes (incl. register/login/logout + per-user scoping); the broker adapter ABC; the per-user 3-mode machine; the per-user 6 gates; the user-scoped idempotency key; the DRY-RUN guard (place_order raises NotImplementedError until a separate Phase-1 enablement).

Write the updated ${LABS}/BOTS_LIVE_FEATURE_SPEC.md. Return a concise summary + the component interface list. Do NOT write feature code in this phase.`,
  { label: 'spec:harden', phase: 'Spec' }
)

// ───────────────────────────────────────────────────────────────────
phase('Build')

const db = await agent(
  `Per the hardened ${LABS}/BOTS_LIVE_FEATURE_SPEC.md, create ${LABS}/storage/live_db.py with init_live_db() that creates the live_* tables via CREATE TABLE IF NOT EXISTS: live_users (user_id PK, username UNIQUE, passcode_hash, created_at), live_broker_connections (with a NOT NULL user_id column), live_orders, live_config, and live_idempotency_ledger. MULTI-USER: per the spec §8, every live_* table except live_users carries a user_id (and conn_id where applicable) for per-user isolation; live_config is per-user/per-conn (composite PK (user_id, key) — NOT a single global key-value store) so mode/kill_switch/lots/armed/daily_loss are scoped per user. NO foreign key into the paper 'bots' table, reuse get_conn() from storage.db, and apply the PRAGMA table_info() safe-migration pattern from ${LABS}/storage/db.py. Do NOT edit the paper schema in storage/db.py. Return the file path + the table DDL.`,
  { label: 'build:db', phase: 'Build' }
)

const engine = await agent(
  `Per the spec and ${LABS}/storage/live_db.py (already created): ${db}

Build the IMPORT-ISOLATED live/ package in ${LABS}/live/. Files:
MULTI-USER (spec §8): all state is per-user/per-connection. Every service/executor/runner function takes a user_id (or conn_id that encodes it) and scopes its DB reads/writes to that owner. No global mode/kill/lots/armed/daily_loss — one user's actions must never affect another.
- live_service.py — DB/config CRUD for live_* tables, ALWAYS scoped by user_id + a user-management API (create_user, verify_user, get_user) + encrypted cred storage (Fernet, key from env var LABS_CRED_KEY, ciphertext under storage/state/; TOTP/PIN write-only), creds stored per user. Reads/writes per-user live_config (mode, kill_switch, lots, armed, daily_loss keyed by (user_id, key)).
- brokers/base.py — adapter ABC: connect(), place_order(), get_positions(), square_off().
- brokers/angel.py, brokers/zerodha.py — concrete adapters; the ONLY files permitted to touch a broker order SDK. CRITICAL DRY-RUN GUARD: place_order() must raise NotImplementedError("LIVE_ARMED not enabled — Phase 1 gated") so NO real order can fire in this build.
- live_executor.py — the single order chokepoint, per-user: calls brokers/* only; evaluates the calling user's own 3-mode + 6 gates; routes DRY_RUN to a logged-intent path (no broker call); user-scoped idempotency key.
- live_runner.py — always-on loop that ITERATES over ALL active user connections, each evaluated independently with its OWN: startup reconciliation (DB vs broker position, block-on-mismatch), 3-mode machine (DISARMED/DRY_RUN/LIVE_ARMED, default DISARMED), armed flag + 6 pre-trade gates, daily-loss cutoff, kill-switch poll, EOD forced square-off, idempotency ledger (INSERT OR IGNORE), and a per-connection runner-owner claim to prevent double-processing.

REUSE Bot A patterns adapted into live/ (read ${BOT}/bots/bot_a/{execution.py,signal.py,runner.py}): the evaluate() signal contract, _resolve_itm_option, restart-reconcile, trade-state persistence, per-tier counters, atomic trade-marker writer.

HARD CONSTRAINT: live/* must NEVER 'import labs.engine.paper_executor' or 'import labs.engine.strategy_runner'. Reuse only neutral infra: storage.db.get_conn, storage.live_db, auth.session_manager, config.labs_config. Return the file list.`,
  { label: 'build:engine', phase: 'Build' }
)

const web = await agent(
  `Per the spec and the live/ package (already built): ${engine}

Build the web layer in ${LABS}:
- labs/ui/live_routes.py — Blueprint live_bp at /live with routes: / (dashboard), /connect (broker cred intake), /configure (lots + bot select), /dryrun, /kill, /arm, /status. EVERY route mutates DB/config ONLY — NONE may call a broker order API.
- Register live_bp in ${LABS}/app.py (additive: import + register_blueprint, next to the existing labs_bp lines; do not disturb labs_bp).
- templates/live.html + templates/live_configure.html matching the broker-connect UI images: Connect Broker landing -> broker selector (Zerodha Kite Connect / Angel One SmartAPI) -> credential forms (Zerodha: API Key + API Secret; Angel One: API Key + Client Code + PIN + TOTP Secret); Configure page: number of lots (show "NIFTY 1 lot = 65 qty"), select bot (1 variant), and a clear mode + armed-status banner (DISARMED/DRY_RUN/LIVE_ARMED). static/live.css, static/live.js (filenames live.* so they never overwrite labs.*).
- Passcode auth: a before_request gate (allow only /login + static; check session flag; compare submitted passcode to a bcrypt/argon2 hash from PA env var via hmac.compare_digest; set app.secret_key = secrets.token_hex(32) loaded from env, never committed). TOTP/PIN fields are write-only — never echoed back in any GET/JSON.
Return the file list.`,
  { label: 'build:web', phase: 'Build' }
)

const launcher = await agent(
  `Per the spec and the web layer (already built): ${web}

Finalize in ${LABS}:
- pa_live_runner.py — top-level launcher mirroring pa_strategy_runner.py (sys.path fix + runpy.run_module("live.live_runner", run_name="__main__")), for its own PA always-on task.
- A CI isolation gate test (tests/test_live_isolation.py or similar) that FAILS if: 'place_order' appears anywhere under labs/; OR a broker order SDK call appears in live_routes.py / live_service.py / live_runner.py (allowed only under live/brokers/*); OR 'import live' appears in labs/engine/paper_executor.py.
- Update ${LABS}/.gitignore for storage/state/creds_enc.json and any new secret/token artifacts.
Return the file list.`,
  { label: 'build:launcher', phase: 'Build' }
)

// ───────────────────────────────────────────────────────────────────
phase('Verify')

const verify = await agent(
  `Verify the DRY-RUN build in ${LABS} (read-only checks + local test runs; make NO edits, NO commit, NO push, NO PA deploy). Layers built: ${launcher}

Run and report PASS/FAIL for each:
1. Schema: 'python storage/db.py' (paper) still clean; init_live_db() creates live_* tables without error.
2. Import/render: the Flask app imports without error; /live and /live/configure render (import-check or app test client).
3. Broker-API boundary (grep): ZERO 'place_order' (or Angel SmartAPI order calls) under labs/; broker order SDK calls appear ONLY under live/brokers/*; none in labs/*, live_routes.py, live_service.py, live_runner.py.
4. Paper isolation (grep): live/* never imports labs.engine.paper_executor or labs.engine.strategy_runner; paper_executor.py never imports live.
5. DRY-RUN guard: confirm brokers/{angel,zerodha}.place_order raises NotImplementedError (no real order possible in this build).
6. DRY_RUN mode produces logged intents, not broker calls; default mode is DISARMED.
7. Secrets: no secret committed/staged; storage/state gitignored; TOTP/PIN not echoed in any GET.
8. The CI isolation test passes.

Report a PASS/FAIL table + the full feature diff ('git status --short' in ${LABS}, and a file inventory of everything created). Explicitly confirm: this build CANNOT place a real order (guard raises), and nothing was committed/pushed/deployed.`,
  { label: 'verify:dry-run-build', phase: 'Verify' }
)

return {
  spec,
  build: { db, engine, web, launcher },
  verify,
  note: 'DRY-RUN-only build. place_order is guarded (raises). No commit/push/deploy performed. Operator review required before any commit and before any Phase-1 live enablement.',
}
