# Bots Live Feature Spec

This document defines the standalone Labs live-trading stack after removal of
the legacy external runner dependency. The live stack is owned by this repo and
is implemented under `live/`.

## Purpose

Labs Live is a multi-user real-money trading harness for NIFTY options. Users
register, connect their own broker account, configure lots and daily loss caps,
and operate through a per-connection mode machine.

Supported brokers:

- Angel One, primary
- Zerodha, secondary

## Hard Constraints

- The paper engine under `labs/engine/` must never place real orders.
- The web UI must never place real orders.
- Broker SDK order calls may appear only under `live/brokers/`.
- Every live order intent must route through `live/live_executor.py`.
- Real order placement stays disabled until a deliberate reviewed enablement.
- All state is scoped by `(user_id, conn_id)`.
- One broker account may not be claimed by two live connections.

## Architecture

| Area | Files | Responsibility |
|---|---|---|
| Web routes | `labs/ui/live_routes.py` | Login, broker connection, config, mode controls |
| Live DB | `storage/live_db.py` | Dedicated rollback-journal SQLite DB for `live_*` tables |
| Live service | `live/live_service.py` | User-scoped CRUD, credentials, config, state, orders, trades |
| Runner | `live/live_runner.py` | Always-on polling, alpha read, signal evaluation, order intents |
| Alpha | `live/engine/alpha_hybrid.py` | Locked hybrid alpha from shared market data |
| Signal | `live/engine/signal_engine.py` | Pure Hybrid Alpha signal logic |
| Executor | `live/live_executor.py` | Mode machine, gates, idempotency, ledger, fill refresh |
| Brokers | `live/brokers/*.py` | Broker sessions, quotes, positions, guarded order calls |
| PA launcher | `pa_live_runner.py` | Loads private env and starts the runner |

## Mode Machine

```text
DISARMED -> DRY_RUN -> LIVE_ARMED
LIVE_ARMED -> DISARMED
DRY_RUN -> DISARMED
```

There is no direct `DISARMED -> LIVE_ARMED` transition.

## Gates

Before any real order can leave the process, the executor checks:

- `mode_armed`
- `kill_switch_clear`
- `broker_connected`
- `account_isolation`
- `daily_loss_ok`
- `lots_within_cap`

Exit orders bypass entry-only gates such as daily loss and lots, but still
require mode, kill-switch, broker-session, and account-isolation checks.

## Account Isolation

Each broker adapter returns a stable `account_ref`. The live service prevents
two connections from claiming the same account. The isolation gate blocks if
the selected account is already bound to another connection.

There is no special-case block for any removed external runner account.

## DRY_RUN

DRY_RUN executes the full live pipeline without calling broker order methods.

- Reads real hybrid alpha.
- Evaluates real signal logic.
- Resolves symbols and LTP.
- Writes `live_orders` with `dry_run=1`.
- Updates DB trade state so simulated entries can exit.
- Shows simulated intents in the dashboard.

## Live Enablement

The final live-money switch is intentionally separate:

```python
_LIVE_ORDERS_ENABLED = False
```

Real orders require both:

- `_LIVE_ORDERS_ENABLED = True` in the broker adapter
- `LIVE_ORDERS_ENABLED=1` in private runtime env

This dual guard prevents missing env vars from enabling orders.

## Private Runtime Files

These files must not be committed:

- `config/live_env.json`
- `storage/live.db`
- `storage/state/creds_enc.json`
- broker session/token files
- plaintext broker credentials

