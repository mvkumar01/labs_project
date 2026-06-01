"""
live/ — parallel, import-isolated real-money auto-trading stack.

HARD ISOLATION (enforced by the §11 CI grep gate):
  * `live/*` NEVER imports `labs.engine.paper_executor` or
    `labs.engine.strategy_runner` (or anything under `labs.engine.*` /
    `labs.services.*`). It reuses ONLY neutral infra:
        storage.live_db, auth.session_manager, config.labs_config.
  * A broker order SDK (`kiteconnect`, `SmartApi`) may be imported ONLY
    under `live/brokers/`.
  * `place_order` does not appear anywhere under `labs/`.

DRY-RUN ONLY (Phase 0): every broker adapter's `place_order` raises
NotImplementedError until a deliberate Phase-1 enablement flips
`_LIVE_ORDERS_ENABLED`. No real order can fire in this build.
"""
