# BOTS LIVE — Multi-User Real-Money Auto-Trading Feature (BUILD-READY CONTRACT)

**Status:** BUILD-READY CONTRACT — interfaces frozen. Build agents implement to these exact signatures.
This phase is **spec only**; no feature code, no commit, no deploy.
**What it is:** A generic, **N-user** real-money auto-trading platform layered onto the labs PA Flask app (`labs-mvkumar01.pythonanywhere.com`). Any number of users register, log in, connect their **own** broker account (Angel One or Zerodha), and operate it in **complete isolation** from every other user. It reuses the proven Bot A execution engine (`Nifty_Bots_Python/bots/bot_a/`), adapted — not rewritten — behind a broker adapter, with a per-user 3-mode safety machine and 6 pre-trade gates.
**Goal:** catch real-deployment bugs (slippage, tech failures, broker API quirks, execution edge-cases) at 1–2 lots, with Angel One as the primary broker (account isolation from Bot A's Zerodha book).

---

## HARD CONSTRAINTS (override everything below)

1. **Paper-only Rule #1 preserved.** `labs/engine/paper_executor.py` must NEVER call `kite.place_order()`. The live stack is a *parallel, import-isolated* package: `live/*` must never `import labs.engine.*`, and `labs/*` must never `import live...`. A `grep place_order` over `labs/` stays at **zero hits**.
2. **The Web UI never places orders.** Only the always-on `live_runner` places/exits. All Flask routes mutate DB/config only.
3. **Broker-SDK boundary.** Only `live/brokers/*` may import/call an external broker order SDK. No order-API call may appear in `labs/*`, `live_routes.py`, `live_service.py`, or `live_runner.py`.
4. **DRY-RUN by default; LIVE is multi-gated, per user.** Each user's default 3-mode state is `DISARMED`. `place_order` in every adapter raises `NotImplementedError` until a deliberate Phase-1 enablement (§13).
5. **MULTI-USER by design.** This is a generic N-user system, NOT a single-operator / single-account / single-passcode tool. **All trading state is per-user / per-connection — never global.** One user arming, killing, configuring, or hitting a daily-loss halt MUST NOT affect any other user.

> Authored 2026-05-30. Based on cross-repo audits + Bot A engine read (`execution.py`, `signal.py`, `runner.py`).

---

## 1. ARCHITECTURE

### 1.1 Design thesis

A **separate `live/` package** inside `labs_project` reuses the proven Bot A engine behind a **new `/live` Flask blueprint** with its own templates, its own `live_*` DB tables, and its own PA always-on runner. The paper engine is untouched and order-free. Real-money safety is enforced **per user** by: a 3-mode state machine (DISARMED → DRY_RUN → LIVE_ARMED), 6 pre-trade gates that ALL must pass before any live order, startup broker reconciliation that blocks on mismatch, a user-scoped idempotency ledger, per-user identity/auth, encrypted-at-rest creds, and a hard 2-lot ceiling reached only by phased rollout. Brokers are **Angel One (primary) + Zerodha only**. XTS / Bull Force are dropped.

### 1.2 Module / file layout

```
C:/Users/vipin/labs_project/
├── app.py                      # +N lines: env secret_key, register live_bp + auth gate (additive)
├── pa_live_runner.py           # NEW PA always-on launcher (mirrors pa_strategy_runner.py)
│
├── live/                       # NEW package — the entire real-money stack
│   ├── __init__.py
│   ├── live_runner.py          # always-on poll loop over ALL active user connections; ONLY order-placing owner. No broker SDK import.
│   ├── live_service.py         # DB access for live_* tables (user/conn-scoped CRUD + config). NO broker calls.
│   ├── live_state.py           # per-user/per-conn 3-mode state machine — pure.
│   ├── gates.py                # per-user/per-conn 6 pre-trade gates + GateResult. Pure checks against DB/adapter.
│   ├── rails.py                # user-scoped idempotency key/ledger, daily-loss, kill switch, EOD watchdog, per-conn runner-owner claim
│   ├── reconcile.py            # per-conn startup DB-vs-broker reconciliation (block-on-mismatch)
│   ├── crypto.py               # Fernet envelope encryption (encrypt/decrypt in-memory only)
│   ├── auth_gate.py            # per-user session auth before_request + register/login/logout + throttle/CSRF
│   ├── brokers/                # ★ ONLY package allowed to import a broker order SDK ★
│   │   ├── __init__.py
│   │   ├── base.py             # BrokerAdapter ABC, OrderResult, Position
│   │   ├── angel_adapter.py    # SmartApi.SmartConnect — PRIMARY
│   │   └── zerodha_adapter.py  # kiteconnect.KiteConnect — secondary (Bot-A-account-blocked)
│   └── engine/
│       ├── __init__.py
│       ├── signal_engine.py    # ADAPTED copy of bots/bot_a/signal.py (pure logic; no broker, no DB)
│       └── order_manager.py    # ADAPTED copy of bots/bot_a/execution.py, broker-abstracted + per-conn
│
├── storage/
│   ├── db.py                   # UNTOUCHED (reuse get_conn only)
│   ├── live_db.py              # NEW: init_live_db() — creates live_* tables only
│   └── state/                  # gitignored — Fernet key NEVER here; only non-secret state markers
│
├── labs/ui/
│   ├── routes.py               # UNTOUCHED (labs_bp, /labs)
│   └── live_routes.py          # NEW: live_bp (/live). Per-user scoped. Mutates DB/config only. NO broker calls.
│
├── templates/
│   ├── live_login.html         # username + passcode login
│   ├── live_register.html      # username + passcode registration
│   ├── live_connect.html       # broker selector (Angel default, Zerodha)
│   ├── live_credentials.html   # per-broker cred form (write-only)
│   ├── live_configure.html     # lots + bot variant + daily-loss + mode controls (this user only)
│   └── live_status.html        # mode banner, position, PnL, kill switch, reconciliation warning (this user only)
│
└── static/
    ├── live.css
    └── live.js
```

### 1.3 Flask wiring (additive — `app.py`)

```python
import os, secrets
from labs.ui.live_routes import live_bp
from live.auth_gate import register_auth_gate

app.secret_key = os.environ.get("LABS_SECRET_KEY") or secrets.token_hex(32)  # 32-byte hex from PA env
register_auth_gate(app)          # per-user session before_request (covers /live blueprint)
app.register_blueprint(live_bp)  # url_prefix="/live"
```

`live_bp = Blueprint("live", __name__, url_prefix="/live")`. `live_routes.py` imports only `live.live_service`, `live.live_state`, `live.gates`, `live.crypto`, `live.auth_gate` — **never** `labs.services.*`, `labs.engine.*`, or any broker SDK.

### 1.4 Mechanically-enforced isolation

1. `live/*` imports only neutral infra: `storage.db.get_conn`, `storage.live_db`, `config.labs_config`, `auth.session_manager` (read-only Zerodha kite provider). **Never** `labs.engine.paper_executor` / `labs.engine.strategy_runner`.
2. `paper_executor.py` never `import live...`.
3. **Broker-SDK boundary:** `import kiteconnect` / `from SmartApi import ...` may appear **only** under `live/brokers/`. CI grep gate (§12) fails the build on any violation.
4. Schema split: paper tables vs `live_*`, no cross-FK.
5. Process split: paper → `pa_strategy_runner.py`; live → `pa_live_runner.py`. Independent restart, logs, kill switch.
6. Blueprint/URL split: `/labs` vs `/live`.
7. **User-scope isolation (multi-user crux):** every `live_*` table carries `user_id` (and `conn_id` where a connection exists); every `live_service` read/write is scoped to a `user_id`; every `/live` route reads `user_id` from the session and only ever touches that user's rows. No `live_service` function exposes a cross-user query path.

---

## 2. IDENTITY MODEL — `conn_id`, `user_id`, scoping rules

The whole feature is scoped along two keys:

- **`user_id`** — opaque stable id (e.g. `uuid4().hex`) assigned at registration. Carried in the Flask session after login. Every `live_*` row except `live_users` carries it.
- **`conn_id`** — a per-user broker-connection id, **derived deterministically** as `f"{user_id}:{broker}"` (broker ∈ `{"angel","zerodha"}`). One user may hold up to one connection per broker (so at most Angel + Zerodha). Because `conn_id` embeds `user_id`, two different users on the same broker never collide, and the idempotency key (§9) is automatically user-scoped.

**Scoping invariants (build agents MUST honor all):**
- Every `live_service` getter/setter and every `gates`/`rails`/`reconcile`/`live_state` function takes an explicit `user_id` (and `conn_id` where a connection is implied). There is **no** global getter.
- `live_runner` iterates connections; each iteration carries a single `(user_id, conn_id)` and never reads another user's rows.
- Routes derive `user_id = session["user_id"]`; a user can never pass another user's `user_id`/`conn_id` (routes ignore client-supplied ids and re-derive from session).

---

## 3. DB TABLES — `storage/live_db.py`

`init_live_db(conn=None)` runs one `executescript` of `CREATE TABLE IF NOT EXISTS`, reusing `get_conn()` (WAL, `check_same_thread=False`). Uses the `PRAGMA table_info` ADD-COLUMN guard for future migrations. All tables `live_`-prefixed, share `labs.db`, never FK to paper tables. **Every table except `live_users` carries `user_id`; per-connection tables also carry `conn_id`.**

| Table | Purpose | Columns |
|---|---|---|
| `live_users` | one row per registered user | `user_id TEXT PK, username TEXT UNIQUE NOT NULL, passcode_hash TEXT NOT NULL, created_at TEXT NOT NULL` |
| `live_broker_connections` | one row per (user, broker) connection (NO secrets) | `conn_id TEXT PK, user_id TEXT NOT NULL, broker TEXT NOT NULL, account_label TEXT, account_ref TEXT, status TEXT, connected_at TEXT, created_at TEXT, updated_at TEXT` — `UNIQUE(account_ref)` (global account-isolation), `UNIQUE(user_id, broker)` |
| `live_credentials_enc` | Fernet ciphertext blobs, keyed per connection | `conn_id TEXT PK, user_id TEXT NOT NULL, broker TEXT NOT NULL, ciphertext BLOB NOT NULL, created_at TEXT, updated_at TEXT` |
| `live_config` | **per-user/per-conn** runtime config (3-mode state, kill switch, lots, daily-loss cap, runner owner…) | `user_id TEXT NOT NULL, conn_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT, updated_at TEXT, PRIMARY KEY (user_id, conn_id, key)` |
| `live_orders` | **idempotency ledger** — every order attempt | `idem_key TEXT PK, user_id TEXT NOT NULL, conn_id TEXT NOT NULL, broker_order_id TEXT, trade_date TEXT, strategy_version TEXT, bar_timestamp TEXT, action TEXT, side TEXT, entry_rule TEXT, intent_seq INTEGER, symbol TEXT, qty INTEGER, order_type TEXT, limit_price REAL, status TEXT, dry_run INTEGER, avg_fill_price REAL, placed_at TEXT, filled_at TEXT, created_at TEXT` |
| `live_idempotency_ledger` | *(alias view — `live_orders` IS the ledger; no separate table. Listed here only to map the operator's requirement name to the implemented table.)* | — |
| `live_trades` | round-trip trades for PnL/audit | `trade_id TEXT PK, user_id TEXT NOT NULL, conn_id TEXT NOT NULL, side TEXT, symbol TEXT, entry_price REAL, exit_price REAL, qty INTEGER, pnl REAL, entry_time TEXT, exit_time TEXT, reason TEXT, dry_run INTEGER` |
| `live_trade_state` | restart-safe single-row position state per connection (mirrors Bot A trade_state) | `conn_id TEXT PK, user_id TEXT NOT NULL, position TEXT, side TEXT, symbol TEXT, entry_spot REAL, entry_time TEXT, entry_price REAL, virtual INTEGER, peak_pnl REAL, entry_rule TEXT, max_alpha_seen REAL, entry_grace_until TEXT, daily_trades_date TEXT, daily_trades_by_tier TEXT, updated_at TEXT` |
| `live_day_pnl` | per-IST-date realized PnL per connection (daily-loss source of truth) | `trade_date TEXT, user_id TEXT NOT NULL, conn_id TEXT NOT NULL, realized_pnl REAL, trade_count INTEGER, halted INTEGER, PRIMARY KEY (trade_date, conn_id)` |

**`live_config` canonical keys** (string values; helpers in `live_service` cast). **All keys are per-(user_id, conn_id) — never global:**

| key | type | default | meaning |
|---|---|---|---|
| `mode` | enum | `DISARMED` | this connection's 3-mode state (§4). Persisted; default DISARMED. |
| `kill_switch` | int 0/1 | `0` | this connection's pollable hard halt |
| `lots` | int 1–2 | `1` | phase-capped (§13), per connection |
| `daily_loss_cap` | float ₹ | `3000` | this connection's rupee stop |
| `bot_variant` | str | `bot_a_v28` | engine variant |
| `armed` | int 0/1 | `0` | explicit armed flag — set 1 only by a successful `arm_live` after gates pass; cleared by `disarm`/kill |
| `strategy_version` | str | `bot_a_v28` | feeds idempotency key |
| `intent_seq` | int | `0` | monotonic per-(conn, date) intent counter |
| `reconcile_blocked` | int 0/1 | `0` | set by startup reconcile mismatch (§8); blocks new entries |
| `runner_owner` | str | `""` | PA task id + heartbeat ISO, **per conn_id** (single-flight) |

---

## 4. PER-USER 3-MODE STATE MACHINE — `live/live_state.py`

Three modes, **persisted in `live_config[(user_id, conn_id, 'mode')]`, default `DISARMED`**. Every function is scoped to `(user_id, conn_id)`; **one connection's mode change never touches another's.**

```
DISARMED ──arm_dry_run()──▶ DRY_RUN ──arm_live()──▶ LIVE_ARMED
   ▲                           │                          │
   └────disarm()───────────────┴──────disarm()────────────┘
```

| Mode | Runner behavior (for THAT connection only) |
|---|---|
| `DISARMED` | runner idle for this conn; evaluates nothing; places nothing. Default at install and after any `disarm()`. |
| `DRY_RUN` | full pipeline runs (signal → gates → simulated order → state → PnL → EOD → kill), `dry_run=1` rows written, **zero broker order calls**. |
| `LIVE_ARMED` | real orders *permitted* for this conn — but still gated by all 6 gates (§6) every order. |

```python
# live/live_state.py
from enum import Enum

class Mode(str, Enum):
    DISARMED   = "DISARMED"
    DRY_RUN    = "DRY_RUN"
    LIVE_ARMED = "LIVE_ARMED"

_VALID = {
    Mode.DISARMED:   {Mode.DRY_RUN},                  # arm_dry_run
    Mode.DRY_RUN:    {Mode.LIVE_ARMED, Mode.DISARMED},
    Mode.LIVE_ARMED: {Mode.DISARMED},                 # only disarm; re-arm via DRY_RUN
}

class InvalidTransition(Exception): ...

def get_mode(user_id: str, conn_id: str, conn=None) -> Mode: ...        # DISARMED if unset
def can_transition(current: Mode, target: Mode) -> bool:
    return target in _VALID.get(current, set())
def set_mode(user_id: str, conn_id: str, target: Mode, conn=None) -> Mode:
    ...   # validates via can_transition, else raises InvalidTransition; writes live_config
def arm_dry_run(user_id: str, conn_id: str, conn=None) -> Mode: ...     # DISARMED -> DRY_RUN
def arm_live(user_id: str, conn_id: str, conn=None) -> Mode: ...        # DRY_RUN -> LIVE_ARMED; sets armed=1
def disarm(user_id: str, conn_id: str, conn=None) -> Mode: ...          # any -> DISARMED; sets armed=0
```

Invariant: there is **no DISARMED → LIVE_ARMED** edge. Going live always passes through DRY_RUN. `arm_live` also sets `live_config[...,'armed']=1`; `disarm` (and any kill) sets it back to `0`.

---

## 5. EXECUTION REUSE — adapt Bot A, do not rewrite

Bot A (`Nifty_Bots_Python/bots/bot_a/`) is the canonical proven engine. We **port and broker-abstract** it into `live/engine/`. Strategy/signal logic is copied near-verbatim (v2.8-aligned). The order-placement surface is wrapped behind `BrokerAdapter`. **`order_manager` is instantiated once per active connection** so its trade-state and counters are per-conn (DB-backed, not JSON files).

### 5.1 Reuse map (component → action)

| Bot A component (`bots/bot_a/…`) | Action | Notes |
|---|---|---|
| `signal.py` whole `AlphaSignalEngine` (`evaluate:325`, `pick_alpha:176`, `_enter:533`, Rule3, v711, v28 flip) + `v711_drift_update:65` | **REUSE near-verbatim** → `live/engine/signal_engine.py`. Pure, no broker, no DB writes. | Signal contract unchanged (§5.2). One engine instance per conn. |
| `execution.py` `OrderManager` (`__init__:330`, `handle_signal:652`, `_enter:735`, `_exit_all:846`, `_resolve_itm_option:1097`) | **ADAPT** → `live/engine/order_manager.py`. Constructor now `OrderManager(signal_engine, broker, user_id, conn_id)`. | Replace `self.kite` with `self.broker` (a `BrokerAdapter`). Replace `self.kite.place_order(...)` (`:763`, `:907`) with `rails.place_idempotent(...)` → `self.broker.place_order/exit_all`. Keep ITM resolution (`ITM_DISTANCE=200`), LIMIT-at-LTP, qty step-down retry, virtual fallback. |
| trade_state file I/O (`_load_trade_state:450`, `_save_trade_state:463`, `_reset_trade_state:471`) | **REPLACE** JSON-file persistence with `live_service` reads/writes to `live_trade_state` keyed by `conn_id`. | No `state/trade_state_botA.json`; DB-backed, restart-safe, per-conn. |
| `_get_open_position:567`, `get_position_context:580`, `_reconcile_state_with_broker:483` | **ADAPT** → broker-abstracted via `adapter.get_position()`. | Mid-day restart reconciliation preserved; §8 hardens it to block-on-mismatch. |
| `load_access_token:115` / `__init__` kite construction | **REPLACE** → broker session via the per-conn adapter (`adapter.connect()` from decrypted creds). Zerodha via `auth.session_manager.get_kite()` only inside `zerodha_adapter`. | session_manager is shared neutral infra; used ONLY inside `live/brokers/`. |
| per-tier daily counters (`_roll_daily_counter_if_new_day:621`, `_increment_daily_trade_counter:642`) | **REUSE** → persisted in `live_trade_state.daily_trades_by_tier` (JSON column), per conn. | Restart-safe. |
| market reads (`_get_nifty_spot:947`, `_get_option_ltp:973`, quote/OI `_batch_pe_oi`, `_find_ce_wall`, `read_vix:1156`) | **ADAPT** → `adapter.get_spot()`, `adapter.get_ltp()`, `adapter.quote()`. | All market reads go through the adapter. |
| exit checks (`check_spot_exit:1319`, `check_alpha_stall:1486`, `check_wall_rejection:1403`, `check_v711_drift_protective:1167`, `check_v28_pc250_gap_up_spot_exit:1260`, `check_d2_trigger:1781`) | **REUSE** → call `self.broker.exit_all(...)` (via `rails.place_idempotent`) instead of kite. | Logic verbatim; only broker surface swapped. |
| `_write_trade_marker:1036` (hardcoded alphaIMB path) | **DROP** → write `live_trades` DB rows; `live_status.html` reads DB. | No cross-repo file writes. |
| `runner.py` `main:78` 2s poll, `get_latest_alpha:28` | **ADAPT** → `live/live_runner.py` (§7) — now iterates ALL active connections. Alpha feed read-only. | Runner never computes alpha. |
| `MAX_TRADES_PER_DAY:29`, `MAX_TRADES_PER_TIER:40`, `EOD_EXIT_TIME:54`, `ITM_DISTANCE:56`, `LOT_SIZE:25`, `ZERODHA_TOTAL_LOTS:26` | **REUSE** → module constants in `order_manager.py`; lots clamped to that conn's `live_config['lots']` (≤2). | |

### 5.2 Signal contract (unchanged from Bot A)

`signal = {"action": "ENTER"|"EXIT"|"HOLD", "side": "CALL"|"PUT"|None, "reason": str, "rule": str|None}` plus optional v2.8 flip keys (`v28_pc250_gap_up_flipped`, `exit_mode`, `tp_pts`, `sl_pts`). Consumed by `order_manager.handle_signal(signal)`. Pure; no broker, no DB.

### 5.3 Broker adapter ABC — `live/brokers/base.py`

★ The only place in the repo allowed to import a broker order SDK. ★ One adapter instance per active connection.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

@dataclass
class OrderResult:
    broker_order_id: Optional[str]   # None on DRY-RUN / failure
    status: str                      # "PLACED" | "REJECTED" | "DRY_RUN" | "FAILED"
    avg_fill_price: Optional[float]
    raw: Optional[dict]              # broker payload (NEVER contains creds)

@dataclass
class Position:
    symbol: Optional[str]
    qty: int                         # signed; 0 == flat
    side: Optional[str]              # "CALL" | "PUT" | None

class BrokerAdapter(ABC):
    broker_name: str                 # "angel" | "zerodha"

    def __init__(self, *, user_id: str, conn_id: str, creds: dict): ...
        # creds is the in-memory decrypted dict (crypto.decrypt). NEVER stored, logged, or echoed.

    @abstractmethod
    def connect(self) -> None: ...                   # establish session from in-memory creds
    @abstractmethod
    def is_connected(self) -> bool: ...              # cheap auth ping for gate 3
    @abstractmethod
    def account_ref(self) -> str: ...                # stable account id for isolation (§6 gate 4)
    @abstractmethod
    def get_spot(self) -> float: ...                 # NIFTY index LTP
    @abstractmethod
    def get_ltp(self, symbol: str) -> float: ...
    @abstractmethod
    def quote(self, symbols: list[str]) -> dict: ... # for OI walls
    @abstractmethod
    def get_position(self) -> Position: ...          # current broker NFO MIS position
    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> dict: ...

    # ── THE GUARDED CALLS ──────────────────────────────────────────────
    @abstractmethod
    def place_order(self, *, side: str, symbol: str, qty: int,
                    price: float, idempotency_key: str) -> OrderResult:
        """MUST raise NotImplementedError until Phase-1 enablement (§13).
        Concrete adapters implement the real broker call ONLY behind the
        _LIVE_ORDERS_ENABLED guard. DRY_RUN never reaches the real branch."""
        ...

    @abstractmethod
    def exit_all(self, *, symbol: str, qty: int, reason: str,
                 idempotency_key: str) -> OrderResult: ...   # same guard
```

Concrete adapters:
- **`angel_adapter.py` (PRIMARY)** — wraps `SmartApi.SmartConnect`. `connect()` = `generateSession(client_code, pin, totp)` from decrypted creds. `account_ref()` = client_code. Maps logical ops to Angel `placeOrder` (NFO, INTRADAY, LIMIT @ LTP).
- **`zerodha_adapter.py`** — wraps `kiteconnect.KiteConnect`. Session from the user's own stored encrypted token (NOT Bot A's). `account_ref()` = api_key/user_id. Mirrors Bot A surface (VARIETY_REGULAR / EXCHANGE_NFO / PRODUCT_MIS / ORDER_TYPE_LIMIT @ LTP). **Blocked by gate 4 if its `account_ref()` equals Bot A's live account OR any other user's connected `account_ref`.**

**Fill verification (gap closed vs Bot A):** `place_order` returns `OrderResult.broker_order_id`; `order_manager` polls `get_order_status` for fill/avg-price; PnL uses actual fill price. An unfilled exit LIMIT escalates to MARKET after a timeout.

### 5.4 DRY-RUN / NotImplementedError guard

Every adapter top:

```python
# live/brokers/angel_adapter.py  (and zerodha_adapter.py identically)
_LIVE_ORDERS_ENABLED = False   # ← Phase-1 enablement flips this True (deliberate, reviewed commit)

def place_order(self, *, side, symbol, qty, price, idempotency_key) -> OrderResult:
    if not _LIVE_ORDERS_ENABLED:
        raise NotImplementedError(
            "Live order placement is disabled (Phase-0 dry-run). "
            "Enable only via the Phase-1 enablement commit after a clean dry-run session."
        )
    # ...real broker call below this line, reached only in LIVE_ARMED after all 6 gates pass...
```

In DRY_RUN, `rails.place_idempotent` short-circuits to a simulated `OrderResult(status="DRY_RUN", avg_fill_price=price)` and writes a `dry_run=1` ledger row — `place_order` is **never** invoked. So in Phase 0 the real branch is unreachable two ways: (a) mode is DRY_RUN, and (b) the guard raises anyway. Phase 1 flips `_LIVE_ORDERS_ENABLED` AND requires `LIVE_ARMED` + 6 gates.

---

## 6. SIX PER-USER PRE-TRADE GATES — `live/gates.py`

ALL six must pass before any live order, evaluated **for the specific `(user_id, conn_id)`**. Evaluated by `order_manager` immediately before each `place_order`, and surfaced on `/live/status` and `POST /live/arm_live`. One user's gate failure never blocks another user.

```python
from dataclasses import dataclass

@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str

def gate_mode_armed(user_id, conn_id, conn=None) -> GateResult:
    """1. live_config[(user_id,conn_id,'mode')] == LIVE_ARMED AND armed==1."""
def gate_kill_switch_clear(user_id, conn_id, conn=None) -> GateResult:
    """2. live_config[(user_id,conn_id,'kill_switch')] == 0."""
def gate_broker_connected(adapter, user_id, conn_id, conn=None) -> GateResult:
    """3. adapter.is_connected() is True (session live; cheap auth ping ok)."""
def gate_account_isolation(adapter, user_id, conn_id, conn=None) -> GateResult:
    """4. adapter.account_ref() != BOT_A_ZERODHA_ACCOUNT_REF
        AND account_ref is not claimed by any OTHER user's connection
        (enforced by UNIQUE(account_ref) + a live_service lookup).
        Angel passes the Bot-A check trivially (different broker)."""
def gate_daily_loss_ok(adapter, user_id, conn_id, conn=None) -> GateResult:
    """5. live_day_pnl.realized_pnl > -live_config['daily_loss_cap'] AND halted==0
        for today's IST date / this conn."""
def gate_lots_within_cap(user_id, conn_id, conn=None) -> GateResult:
    """6. 1 <= live_config['lots'] <= LOTS_HARD_CAP (==2) AND <= current phase cap (§13)."""

def evaluate_all(adapter, user_id, conn_id, conn=None) -> list[GateResult]: ...
def all_passed(results: list[GateResult]) -> bool: ...
```

`BOT_A_ZERODHA_ACCOUNT_REF` is a configured constant (Bot A's live Zerodha api_key/user_id). Gate 4 hard-blocks arming the feature onto the Bot-A book **or onto an account already connected by another user**. `order_manager` calls `evaluate_all`; if `not all_passed`, it logs the failing gates and **does not** place an entry. Existing open positions may still EXIT — exits bypass entry gates 5/6 but still honor mode≠DISARMED and kill switch for that conn.

---

## 7. RUNNER — `live/live_runner.py` (iterates ALL active connections)

```python
def run() -> None:
    """PA always-on entry. Boot:
       1. init_live_db()
       2. loop forever (POLL_INTERVAL ~2s, market-hours aware):
          for (user_id, conn_id) in live_service.active_connections():
              if not rails.claim_runner_owner(user_id, conn_id, task_id): continue  # single-flight
              process_connection(user_id, conn_id)
       Each connection is evaluated INDEPENDENTLY with its own mode, gates,
       reconcile, daily-loss, kill switch, EOD square-off, idempotency, trade
       state. An exception in one connection is caught, logged, and never
       aborts the loop for other connections."""

def process_connection(user_id: str, conn_id: str) -> None:
    """Per-connection cycle (mirrors Bot A runner.main loop body):
       - mode = live_state.get_mode(user_id, conn_id); if DISARMED -> return
       - on first claim per boot: reconcile.reconcile_on_startup(adapter, user_id, conn_id)
       - if rails.is_killed(...) -> return
       - if eod_watchdog hits and position open -> exit_all and return
       - read alpha bar (read-only shared feed) -> signal_engine.evaluate(...)
       - order_manager.handle_signal(signal)  (gates enforced inside before any place_order)
       DRY_RUN executes the full path with dry_run=1; LIVE_ARMED places real orders
       only after all 6 gates pass and reconcile_blocked==0."""

def get_latest_alpha() -> dict | None:
    """Read-only latest alpha bar from the shared alpha relay (adapted from
    Bot A runner.get_latest_alpha). Never computes alpha; never writes."""
```

`live_service.active_connections()` returns `[(user_id, conn_id), …]` for every connection whose `mode != DISARMED`. The per-conn `runner_owner` claim (PA task id + heartbeat ISO in `live_config`) prevents two runner processes double-processing the same connection.

---

## 8. STARTUP RECONCILIATION (block-on-mismatch) — `live/reconcile.py`

Per connection, on first claim each boot, **before any signal is evaluated**, compare DB state vs actual broker position. On mismatch, block new trades for THAT conn and warn on `/live/status`.

```python
from dataclasses import dataclass

@dataclass
class ReconcileResult:
    ok: bool
    db_symbol: str | None
    db_qty: int
    broker_symbol: str | None
    broker_qty: int
    message: str            # human-readable mismatch description for the status banner

def reconcile_on_startup(adapter, user_id: str, conn_id: str, conn=None) -> ReconcileResult:
    """Compare live_trade_state (this conn) vs adapter.get_position().
    Agreement rules:
      - both flat (db position NONE & broker qty 0)            -> ok
      - both OPEN, same symbol & same |qty|                    -> ok (adopt broker truth)
      - any disagreement (orphan broker pos, db OPEN broker flat,
        symbol/qty mismatch)                                   -> ok=False
    On ok=False: set live_config[(user_id,conn_id,'reconcile_blocked')]='1' and
    write `message`; runner refuses NEW entries for this conn until cleared.
    EXIT of a known position remains allowed."""
```

`process_connection` gates the entry path on this conn's `reconcile_blocked`. The block clears only when a subsequent reconcile agrees (e.g. after manual square-off). `/live/status` (scoped to the session user's conn) surfaces `reconcile_ok` + `reconcile_warning`.

---

## 9. USER-SCOPED IDEMPOTENCY — `live/rails.py`

**Key format (operator-mandated) — `conn_id` already encodes `user_id`, so keys are inherently user-scoped and two users' identical signals never collide:**

```
idem_key = ":".join([
    conn_id, trade_date, strategy_version, bar_timestamp,
    action, side, entry_rule, str(intent_seq)
])
# conn_id == "<user_id>:<broker>", e.g.
# "a1b2c3:angel:2026-05-30:bot_a_v28:2026-05-30T10:25:00+05:30:ENTER:PUT:rule1:3"
```

```python
def build_idem_key(*, conn_id, trade_date, strategy_version, bar_timestamp,
                   action, side, entry_rule, intent_seq) -> str: ...

def place_idempotent(adapter, *, user_id, conn_id, idem_key, side, symbol, qty,
                     price, action, dry_run: bool, conn=None) -> OrderResult:
    """1. INSERT OR IGNORE a live_orders row (user_id, conn_id, idem_key, status='PENDING').
    2. If the row already existed with a non-PENDING status -> SKIP the broker
       call, return the recorded OrderResult (defends against web double-click,
       PA restart re-fire, same-bar re-entry).
    3. Else: if dry_run -> synthetic OrderResult(status='DRY_RUN', avg=price);
             else -> adapter.place_order(...) (gate- & guard-protected).
    4. UPDATE the ledger row with broker_order_id/status/avg_fill_price.
    Pass idem_key as the broker order `tag` where supported."""

def check_daily_loss(user_id, conn_id, conn=None) -> bool: ...   # True if within cap & not halted
def is_killed(user_id, conn_id, conn=None) -> bool: ...
def eod_watchdog(now_ist) -> bool: ...                           # independent EOD square-off trigger
def claim_runner_owner(user_id, conn_id, task_id, conn=None) -> bool: ...  # per-conn single-flight + heartbeat
def next_intent_seq(user_id, conn_id, trade_date, conn=None) -> int: ...   # monotonic, reset per IST date
```

`entry_rule` is `"none"` when absent (e.g. EXIT). `bar_timestamp` is the alpha-bar ISO timestamp that produced the intent. `intent_seq` is the monotonic per-(conn, date) `live_config['intent_seq']`, bumped per committed intent and reset per IST date. `eod_watchdog` runs each poll independently of the signal cycle (hardens Bot A `EOD_EXIT_TIME=15:25`).

---

## 10. AUTH & MULTI-USER IDENTITY — `live/auth_gate.py` + `live_service`

Replaces any single shared-passcode gate with **per-user accounts**. Fails closed.

```python
# live/auth_gate.py
def register_auth_gate(app) -> None:
    """Install @app.before_request: allow /live/login, /live/register, and
    static; otherwise require a valid session['user_id'] (redirect to
    /live/login). Fails closed — no user_id => no access."""

def current_user_id() -> str | None:
    """session.get('user_id'). Routes use this; never trust a client-supplied id."""

def login_throttled(username: str) -> bool:
    """True if this username/IP exceeded 5 attempts/min (brute-force blunt)."""
```

```python
# live/live_service.py (identity portion)
def create_user(username: str, passcode: str, conn=None) -> str:
    """Register: reject duplicate username (UNIQUE). Hash passcode with
    bcrypt/argon2. Insert live_users. Return new user_id (uuid4 hex)."""

def verify_user(username: str, passcode: str, conn=None) -> str | None:
    """Fetch passcode_hash by username; verify with the KDF's checkpw and
    compare the boolean via hmac.compare_digest. Return user_id on success,
    else None. Constant-time-ish; never reveals whether username exists."""

def get_user(user_id: str, conn=None) -> dict | None: ...
```

- `app.secret_key = os.environ['LABS_SECRET_KEY']` (`secrets.token_hex(32)` from env). Cookies `Secure`+`HttpOnly`+`SameSite=Lax`, 30-min idle timeout.
- Passcodes hashed with bcrypt (or argon2); verification boolean wrapped in `hmac.compare_digest`.
- CSRF token on every POST (register, login, cred form, configure, arm_*, kill, resume).
- Login throttle (5/min). Optional IP allowlist as a second layer.

---

## 11. WEB UI & ROUTES — `labs/ui/live_routes.py` (per-user scoped)

Mirrors the pramanaa.ai connect flow on the labs domain. **Routes mutate DB/config only — they never place or exit orders, never import a broker SDK.** Every authenticated route derives `user_id = current_user_id()` and `conn_id` from the session's selected broker; it shows/mutates **only that user's** rows. Going live = a config flag the runner observes.

| Method + Route | Auth | Action (DB/config only, scoped to session user) |
|---|---|---|
| `GET /live/register` | exempt | registration form |
| `POST /live/register` | exempt | `live_service.create_user(username, passcode)`; on success set `session['user_id']` → `/live/connect`; on dup username re-render with error. CSRF + throttle. |
| `GET /live/login` | exempt | login form |
| `POST /live/login` | exempt | `uid = verify_user(...)`; if uid set `session['user_id']=uid` → `/live/`; else re-render error. Throttled 5/min. |
| `POST /live/logout` | required | clear session → `/live/login` |
| `GET /live/` | required | `live_status.html` if this user has a connected+configured conn, else `→ /live/connect` |
| `GET /live/connect` | required | `live_connect.html` — Angel (default) / Zerodha selector |
| `POST /live/connect` | required | store selected broker in session; derive `conn_id=f"{user_id}:{broker}"` → `/live/credentials/<broker>` |
| `GET /live/credentials/<broker>` | required | `live_credentials.html` (write-only fields; show `•••• set`) |
| `POST /live/credentials/<broker>` | required | validate → `crypto.encrypt` → write `live_credentials_enc` (this conn); upsert `live_broker_connections` (with `user_id`). **Never echoes secrets.** |
| `GET /live/zerodha/callback` | required | Kite OAuth callback (request_token → `generate_session`); host-pinned to labs domain; store encrypted token for THIS user's zerodha conn |
| `GET /live/configure` | required | `live_configure.html` — lots, variant, daily-loss for this conn |
| `POST /live/configure` | required | persist lots (re-clamp 1–2 to phase cap), variant, daily_loss to this conn's `live_config` |
| `POST /live/arm_dry_run` | required | `live_state.arm_dry_run(user_id, conn_id)` (DISARMED→DRY_RUN). Config mutation only. |
| `POST /live/arm_live` | required | `gates.evaluate_all(adapter, user_id, conn_id)`; if all pass → `live_state.arm_live(user_id, conn_id)`; else 409 + failing-gate list. Confirm-modal client-side. |
| `POST /live/disarm` | required | `live_state.disarm(user_id, conn_id)` (→DISARMED; armed=0) |
| `POST /live/kill` | required | set this conn's `live_config['kill_switch']=1`, `armed=0` |
| `POST /live/resume` | required | clear this conn's kill switch |
| `GET /live/status` | required | JSON scoped to session user/conn: `mode`, `kill_switch`, `position`, `today_pnl`, `daily_loss_cap`, `reconcile_ok`, `reconcile_warning`, `last_orders[]`, `gates` (pass/fail per gate) |

All POST routes require the auth gate (§10) + a CSRF token. **No route accepts a `user_id`/`conn_id` from the client** — both are re-derived from the session, so a user can never read or mutate another user's state.

**Status page (`live_status.html`)** prominently shows (for the logged-in user only): current **mode banner** (DISARMED/DRY_RUN/LIVE_ARMED), red **KILL SWITCH**, a **reconciliation-mismatch warning** banner when `reconcile_ok=false` (§8), position, live PnL vs daily-loss cap, and the recent `live_orders` table (dry-run rows clearly tagged).

---

## 12. CRED SECURITY + ACCOUNT ISOLATION

### 12.1 Crypto — `live/crypto.py`

```python
def encrypt(plaintext: dict) -> bytes:   # Fernet(LABS_CRED_KEY).encrypt(json.dumps(...))
def decrypt(ciphertext: bytes) -> dict:  # in-memory only; never logged/written/echoed
```

- Master key `LABS_CRED_KEY` from PA env var; never committed.
- Ciphertext stored in `live_credentials_enc.ciphertext` (BLOB), keyed per `conn_id`, in gitignored `labs.db`. Non-secret state markers may live under gitignored `storage/state/`.
- **TOTP secret + PIN + passcode are write-only** — never returned by any GET/JSON; UI shows `•••• set`.
- **Never-log:** never `log`/`print` any cred dict, blob, TOTP, PIN, passcode, or access_token. Exception handlers log field *names* only, never values.
- `.gitignore` additions (before any cred file is created): `storage/state/creds_enc.json`, `storage/state/live_*.json`, `config/*creds*.json`, `config/*token*.json`, `*.secret`, `live/**/secrets.*`. Ship `*.example` only.
- **Precondition (blocks Phase 2):** rotate + history-purge already-leaked secrets (Zerodha api_secret/password/TOTP in tracked `config/zerodha_creds.json`; Angel pin/TOTP in Nifty_Bots history). Not part of the build but must complete before any real lot.

### 12.2 Account isolation (no double-trade vs Bot A or other users)

**Threat:** Bot A is already live on **Zerodha** trading NIFTY; this feature must not place a second NIFTY position against that book — nor may two users share one account.

1. **Angel One primary = different broker account** → physically separate book; nothing to double.
2. **Gate 4 (`gate_account_isolation`)** hard-blocks if a selected account's `account_ref()` equals `BOT_A_ZERODHA_ACCOUNT_REF` **or** is already bound to another user's connection. `UNIQUE(account_ref)` on `live_broker_connections` enforces the latter at the DB layer.
3. **One-engine-per-account:** `live_broker_connections.UNIQUE(account_ref)` plus per-conn `armed`/`mode` keep at most one engine per account.
4. **Process/feed independence:** runner reads only the shared *alpha feed* (read-only); never Bot A's trade-state files / `bot.db`.

**CI grep gate (build-time guard):** fail the build if
- `place_order` appears anywhere under `labs/`, OR
- `import kiteconnect` / `from SmartApi` appears outside `live/brokers/`, OR
- `import live` appears in `paper_executor.py`.

---

## 13. PHASED ROLLOUT (per connection)

1. **Phase 0 — DRY_RUN.** `_LIVE_ORDERS_ENABLED=False`. A user: register → login → Connect (Angel) → Configure (1 lot) → `arm_dry_run`. Full session validates connect→configure→signal→gates→simulated order→exit→EOD→kill→reconciliation→idempotency, **zero broker calls**. **Gate to Phase 1:** clean dry-run session + secrets rotated/purged.
2. **Phase 1 — 1 lot live.** Deliberate enablement commit flips `_LIVE_ORDERS_ENABLED=True`; `lots=1` (NIFTY qty 65); `arm_live` (passes 6 gates). Watch slippage, partial fills, session drops, EOD reliability, idempotency. **Gate to Phase 2:** N clean live sessions, no exec/idempotency anomalies.
3. **Phase 2 — 2 lots live.** `lots=2` (qty 130 — the hard ceiling, `LOTS_HARD_CAP=2`). No further scale-up.

---

## 14. DEPLOYMENT — labs PA domain

- **Kite redirect URL:** `https://labs-mvkumar01.pythonanywhere.com/live/zerodha/callback` (never accept redirect host from a query param). Angel needs no OAuth.
- **`pa_live_runner.py`** mirrors `pa_strategy_runner.py`: `sys.path.insert` then `runpy.run_module("live.live_runner", "__main__")`. `live_runner.run()` calls `init_live_db()`, then loops over all active connections (per-conn claim + reconcile + market-hour poll).
- Register as its **own** PA always-on task (separate process/restart/logs from paper runner + collector).
- **Env vars:** `LABS_SECRET_KEY`, `LABS_CRED_KEY`. (No global `LIVE_PASSCODE_HASH` — passcodes are per-user in `live_users`.)
- **Deploy:** `git stash && git pull origin main`; reload web app; create/start the live always-on task; verify in DRY-RUN.
- Paper runner + collector tasks: **untouched**.

---

## 15. COMPONENT INTERFACE INDEX (build agents implement these)

| Component | File | Key symbols |
|---|---|---|
| DB schema (+ multi-user) | `storage/live_db.py` | `init_live_db`; tables `live_users`, `live_broker_connections`, `live_credentials_enc`, `live_config`, `live_orders` (ledger), `live_trades`, `live_trade_state`, `live_day_pnl` — all `user_id`-scoped |
| Service / DB access | `live/live_service.py` | `create_user`, `verify_user`, `get_user`, `active_connections`, per-(user_id,conn_id) config getters/setters, `live_*` CRUD |
| 3-mode machine (per user) | `live/live_state.py` | `Mode`, `get_mode`, `can_transition`, `set_mode`, `arm_dry_run`, `arm_live`, `disarm`, `InvalidTransition` — all `(user_id, conn_id)`-scoped |
| 6 gates (per user) | `live/gates.py` | `GateResult`, `gate_mode_armed`, `gate_kill_switch_clear`, `gate_broker_connected`, `gate_account_isolation`, `gate_daily_loss_ok`, `gate_lots_within_cap`, `evaluate_all`, `all_passed` |
| Reconciliation (per conn) | `live/reconcile.py` | `ReconcileResult`, `reconcile_on_startup(adapter, user_id, conn_id)` |
| Idempotency + rails | `live/rails.py` | `build_idem_key`, `place_idempotent`, `check_daily_loss`, `is_killed`, `eod_watchdog`, `claim_runner_owner`, `next_intent_seq` |
| Broker ABC | `live/brokers/base.py` | `BrokerAdapter`, `OrderResult`, `Position` |
| Angel adapter | `live/brokers/angel_adapter.py` | `AngelAdapter(BrokerAdapter)`, `_LIVE_ORDERS_ENABLED` guard |
| Zerodha adapter | `live/brokers/zerodha_adapter.py` | `ZerodhaAdapter(BrokerAdapter)`, `_LIVE_ORDERS_ENABLED` guard |
| Signal engine | `live/engine/signal_engine.py` | `AlphaSignalEngine` (verbatim from Bot A), `v711_drift_update` |
| Order manager | `live/engine/order_manager.py` | `OrderManager(signal_engine, broker, user_id, conn_id)`, `handle_signal`, `_enter`, `_exit_all`, exit checks; DB-backed trade_state |
| Runner (multi-conn) | `live/live_runner.py` | `run`, `process_connection`, `get_latest_alpha` |
| Crypto | `live/crypto.py` | `encrypt`, `decrypt` |
| Auth (per user) | `live/auth_gate.py` | `register_auth_gate`, `current_user_id`, `login_throttled` |
| Routes (per user) | `labs/ui/live_routes.py` | `live_bp` + routes in §11 (register/login/logout + per-user scoped, DB/config only) |
| Launcher | `pa_live_runner.py` | PA always-on entry |

---

## Appendix — source references

- Bot A engine: `Nifty_Bots_Python/bots/bot_a/execution.py` (`OrderManager.__init__:330`, `_enter:735`, `_exit_all:846`, `handle_signal:652`, `_reconcile_state_with_broker:483`, `get_position_context:580`, `_get_open_position:567`, `_resolve_itm_option:1097`, `check_spot_exit:1319`, `check_wall_rejection:1403`, `check_v711_drift_protective:1167`, `check_v28_pc250_gap_up_spot_exit:1260`, `check_d2_trigger:1781`, place_order calls `:763`/`:907`, EOD `:54`, caps `:25-52`), `signal.py` (`AlphaSignalEngine.evaluate:325`, `pick_alpha:176`, `_enter:533`, `v711_drift_update:65`), `runner.py` (`main:78`, `get_latest_alpha:28`, 2s poll `:14`).
- labs shell: `app.py` (secret_key, register_blueprint), `storage/db.py` (`get_conn`, ADD-COLUMN guard), `auth/session_manager.py` (`get_kite` — used only inside `live/brokers/zerodha_adapter.py`), `labs/engine/paper_executor.py` (Hard Rule #1 — order-free).
