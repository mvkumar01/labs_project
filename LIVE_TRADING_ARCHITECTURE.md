# Live Trading Architecture

This document describes the Labs live-trading stack as implemented for Phase 1
dry-run validation. It is intentionally separate from the paper-trading Labs
engine.

## Safety Model

The live stack is designed as a parallel package under `live/`.

- Flask routes under `/live` mutate user configuration only.
- The web app never places broker orders.
- Only the PythonAnywhere always-on live runner may create order intents.
- Only files under `live/brokers/` may call broker SDK order APIs.
- `labs/engine/*` remains paper-only and must never call broker order APIs.
- Real order placement remains disabled unless the final reviewed live switch is enabled.

The current Phase 1 implementation supports real signal generation and full
DRY_RUN order intent simulation. The final real-money switch is still off:

```python
_LIVE_ORDERS_ENABLED = False
```

Both Angel and Zerodha also require `LIVE_ORDERS_ENABLED=1`, so a missing env
var cannot accidentally enable real orders.

## Runtime Components

| Component | Path | Responsibility |
|---|---|---|
| Web app | `app.py`, `labs/ui/live_routes.py` | Register/login, broker setup, config, arm/disarm/kill controls |
| Live DB | `storage/live_db.py` | Dedicated rollback-journal SQLite DB for `live_*` tables |
| Live service | `live/live_service.py` | User-scoped CRUD, encrypted credentials, config, state, orders, trades |
| Live runner | `live/live_runner.py` | Always-on polling loop, signal evaluation, DRY_RUN/live order routing |
| Alpha reader | `live/engine/alpha_hybrid.py` | Reads locked hybrid range and shared OI store, computes latest alpha |
| Signal engine | `live/engine/signal_engine.py` | Hybrid Alpha signal engine, pure broker-free trading logic |
| Executor rails | `live/live_executor.py` | Mode machine, gates, idempotency, order ledger, fill refresh |
| Broker adapters | `live/brokers/angel.py`, `live/brokers/zerodha.py` | Broker sessions, quotes, positions, guarded order calls |
| PA launcher | `pa_live_runner.py` | Loads private env and starts `live.live_runner` |

## Data Flow

1. Labs collector writes one-minute market data into the shared store:
   `~/shared_market_data/live/<date>/NIFTY_options_1min.csv`.
2. alphaIMB locks the daily hybrid range in:
   `~/alphaIMB/config/hybrid_range_state.json`.
3. `live/engine/alpha_hybrid.py` reads both sources and computes the latest
   locked hybrid alpha bar using broker-style 5-minute buckets.
4. `live/live_runner.py` feeds each new alpha bar into a per-connection
   `AlphaSignalEngine`.
5. The signal engine emits `ENTER`, `EXIT`, or `HOLD`.
6. The runner resolves an ITM NIFTY option symbol and routes the intent through
   `live/live_executor.py`.
7. In DRY_RUN, the executor writes a simulated intent to `live_orders` and does
   not call the broker order method.
8. In LIVE_ARMED, after the final live switch is enabled, the executor re-checks
   gates and then calls the broker adapter.

## Mode Machine

Each user/broker connection has an independent mode stored in `live_config`.

```text
DISARMED -> DRY_RUN -> LIVE_ARMED
LIVE_ARMED -> DISARMED
DRY_RUN -> DISARMED
```

There is no direct `DISARMED -> LIVE_ARMED` transition. The operator must pass
through DRY_RUN first.

## Pre-Trade Gates

Before any real order can leave the process, all applicable gates must pass for
that specific `(user_id, conn_id)`.

- `mode_armed`: connection is `LIVE_ARMED` and `armed=1`.
- `kill_switch_clear`: kill switch is off.
- `broker_connected`: broker adapter reports a live authenticated session.
- `account_isolation`: account is not already claimed by another live connection.
- `daily_loss_ok`: realized P&L has not breached the configured loss cap.
- `lots_within_cap`: configured lots are within hard limits.

Exit orders bypass entry-only gates such as daily-loss and lots, but still
require mode, kill-switch, broker-connected, and account-isolation checks.

## DRY_RUN Behavior

DRY_RUN is meant to validate the complete trading path without placing orders.

- Real alpha bars are processed.
- Hybrid Alpha signal logic is used.
- ITM symbols and LTP are resolved.
- Order intents are inserted into `live_orders` with `dry_run=1`.
- DB trade state is updated so simulated positions can later exit.
- Broker order methods are not called.

This allows the dashboard's Recent Orders table to show what the live stack
would have traded.

## Broker Sessions And Credentials

Credentials are encrypted by `live_service.py` using `LABS_CRED_KEY`.

The web app and always-on live runner must use the same `LABS_CRED_KEY`, or the
runner cannot decrypt saved broker credentials. For PythonAnywhere, the runner
can load private env values from gitignored:

```text
config/live_env.json
```

That file must never be committed.

## Angel One Adapter

Angel is the primary Phase 1 broker.

- Login uses `SmartConnect.generateSession(client_code, pin, totp)`.
- NIFTY spot uses Angel's NSE spot token.
- Option LTP resolves the Angel `symboltoken` using the daily scrip master.
- Orders use NFO intraday LIMIT orders.
- Order status is polled after placement so the ledger can store broker fill
  status and average fill price.

The adapter still refuses real orders while `_LIVE_ORDERS_ENABLED=False`.

## Zerodha Isolation

Zerodha is supported as a secondary broker. Its account reference participates
in the same duplicate-account isolation gate as Angel One: a connection cannot
arm live if another live connection has already claimed the same account.

## Deployment Sequence

1. Deploy Phase 1 A-F code.
2. Configure `config/live_env.json` on PA with `LABS_CRED_KEY` and proxy/env values.
3. Restart only the live runner always-on task.
4. Arm DRY_RUN and verify broker status becomes `connected`.
5. Run at least one to two full DRY_RUN sessions.
6. Compare DRY_RUN intents against expected Hybrid Alpha behavior.
7. Only after review, flip the final `_LIVE_ORDERS_ENABLED=True` switch in a
   separate commit.
8. Start real trading at one lot only.

## Operational Controls

- **Arm DRY-RUN**: starts real signal processing with simulated orders.
- **Arm LIVE**: permits real order path only after gates pass and the final
  code switch is enabled.
- **Disarm**: stops live activity for that connection.
- **Kill Switch**: blocks new activity immediately.
- **Daily loss cap**: halts trading after configured realized loss.

## Files That Must Stay Private

- `config/live_env.json`
- `storage/live.db`
- `storage/state/creds_enc.json`
- broker token/session files
- any plaintext broker credentials
