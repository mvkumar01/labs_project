"""
Live-trading schema initialisation (live_* tables).

This module is part of the parallel, import-isolated `live` stack. It stores
`live_`-prefixed tables in `storage/live.db` so web-worker writes do not depend
on WAL visibility for the paper engine DB.

Uses rollback-journal mode (`journal_mode=DELETE`) for PythonAnywhere web
worker compatibility. All tables use CREATE TABLE IF NOT EXISTS — safe to call
on every startup.
Future migrations use the PRAGMA table_info() ADD-COLUMN guard (same pattern
as storage/db.py).

MULTI-USER (spec §3, §8): every live_* table EXCEPT `live_users` carries a
`user_id` for per-user isolation, and per-connection tables also carry a
`conn_id` (== "<user_id>:<broker>"). `live_config` is per-user/per-conn — a
composite-PK key/value store (PRIMARY KEY (user_id, conn_id, key)), NOT a
single global key-value store — so mode / kill_switch / lots / armed /
daily_loss are scoped per connection and one user's state never affects
another's.

`live_idempotency_ledger` is NOT a separate table: `live_orders` IS the
idempotency ledger (spec §3 / §9). The operator's "live_idempotency_ledger"
requirement maps to `live_orders`.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.labs_config import LIVE_DB_PATH


def get_live_conn() -> sqlite3.Connection:
    """Open the dedicated live DB in rollback-journal mode."""
    LIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(LIVE_DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_live_db(conn: sqlite3.Connection | None = None) -> None:
    """Create all live_* tables if they don't exist. Call once at runner/app startup.

    Accepts an optional connection (opens + closes its own when None), mirroring
    the labs DB-function convention. NO FK into the paper `bots` table; the
    paper schema in storage/db.py is never touched.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_live_conn()

    try:
        with conn:
            conn.executescript("""
            -- One row per registered user. The ONLY live_* table without user_id
            -- (user_id IS its primary key).
            CREATE TABLE IF NOT EXISTS live_users (
                user_id       TEXT PRIMARY KEY,
                username      TEXT NOT NULL UNIQUE,
                passcode_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );

            -- One row per (user, broker) connection. NO secrets here.
            -- UNIQUE(account_ref): global account-isolation (gate 4 / §12.2).
            -- UNIQUE(user_id, broker): at most one connection per broker per user.
            CREATE TABLE IF NOT EXISTS live_broker_connections (
                conn_id       TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                broker        TEXT NOT NULL,
                account_label TEXT,
                account_ref   TEXT,
                status        TEXT NOT NULL DEFAULT 'disconnected',
                funds_available REAL,
                funds_updated_at TEXT,
                funds_error   TEXT,
                connected_at  TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                UNIQUE(account_ref),
                UNIQUE(user_id, broker)
            );

            -- Fernet ciphertext blobs, keyed per connection. Write-only secrets.
            CREATE TABLE IF NOT EXISTS live_credentials_enc (
                conn_id    TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                broker     TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at TEXT,
                updated_at TEXT
            );

            -- Per-user / per-conn runtime config (3-mode state, kill switch,
            -- lots, daily-loss cap, armed, runner owner, ...). Composite PK —
            -- NOT a global key-value store. Every key is scoped to (user, conn).
            CREATE TABLE IF NOT EXISTS live_config (
                user_id    TEXT NOT NULL,
                conn_id    TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT,
                updated_at TEXT,
                PRIMARY KEY (user_id, conn_id, key)
            );

            -- Idempotency ledger — every order attempt (one row per idem_key).
            -- This table IS `live_idempotency_ledger` (no separate table).
            CREATE TABLE IF NOT EXISTS live_orders (
                idem_key         TEXT PRIMARY KEY,
                user_id          TEXT NOT NULL,
                conn_id          TEXT NOT NULL,
                broker_order_id  TEXT,
                trade_date       TEXT,
                strategy_version TEXT,
                bar_timestamp    TEXT,
                action           TEXT,
                side             TEXT,
                entry_rule       TEXT,
                intent_seq       INTEGER,
                symbol           TEXT,
                qty              INTEGER,
                order_type       TEXT,
                limit_price      REAL,
                status           TEXT,
                dry_run          INTEGER NOT NULL DEFAULT 1,
                avg_fill_price   REAL,
                placed_at        TEXT,
                filled_at        TEXT,
                created_at       TEXT
            );

            -- Round-trip trades for PnL / audit.
            CREATE TABLE IF NOT EXISTS live_trades (
                trade_id    TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                conn_id     TEXT NOT NULL,
                side        TEXT,
                symbol      TEXT,
                entry_price REAL,
                exit_price  REAL,
                qty         INTEGER,
                pnl         REAL,
                gross_pnl   REAL,
                charges_total REAL,
                charges_json TEXT,
                net_pnl     REAL,
                entry_time  TEXT,
                exit_time   TEXT,
                reason      TEXT,
                dry_run     INTEGER NOT NULL DEFAULT 1
            );

            -- Restart-safe single-row position state per connection.
            CREATE TABLE IF NOT EXISTS live_trade_state (
                conn_id              TEXT PRIMARY KEY,
                user_id              TEXT NOT NULL,
                position             TEXT,
                side                 TEXT,
                symbol               TEXT,
                entry_spot           REAL,
                entry_time           TEXT,
                entry_price          REAL,
                qty                  INTEGER,
                virtual              INTEGER,
                peak_pnl             REAL,
                entry_rule           TEXT,
                max_alpha_seen       REAL,
                entry_grace_until    TEXT,
                daily_trades_date    TEXT,
                daily_trades_by_tier TEXT,
                updated_at           TEXT
            );

            -- Per-IST-date realized PnL per connection (daily-loss source of truth).
            CREATE TABLE IF NOT EXISTS live_day_pnl (
                trade_date   TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                conn_id      TEXT NOT NULL,
                realized_pnl REAL,
                trade_count  INTEGER,
                halted       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (trade_date, conn_id)
            );
            """)

        # ── Safe migrations (PRAGMA table_info ADD-COLUMN guard) ──────────
        # Same pattern as storage/db.py: check for missing columns, add them.
        # Safe to call repeatedly. No-op today; placeholders document the
        # convention and let older DBs pick up new columns on next startup.
        def _cols(table: str) -> set[str]:
            return {r[1] for r in conn.execute(
                f"PRAGMA table_info({table})").fetchall()}

        broker_cols = _cols("live_broker_connections")
        if "funds_available" not in broker_cols:
            conn.execute(
                "ALTER TABLE live_broker_connections ADD COLUMN funds_available REAL")
            conn.commit()
        if "funds_updated_at" not in broker_cols:
            conn.execute(
                "ALTER TABLE live_broker_connections ADD COLUMN funds_updated_at TEXT")
            conn.commit()
        if "funds_error" not in broker_cols:
            conn.execute(
                "ALTER TABLE live_broker_connections ADD COLUMN funds_error TEXT")
            conn.commit()

        trade_cols = _cols("live_trades")
        for col, decl in (
            ("gross_pnl", "REAL"),
            ("charges_total", "REAL"),
            ("charges_json", "TEXT"),
            ("net_pnl", "REAL"),
        ):
            if col not in trade_cols:
                conn.execute(f"ALTER TABLE live_trades ADD COLUMN {col} {decl}")
                conn.commit()

        state_cols = _cols("live_trade_state")
        if "qty" not in state_cols:
            conn.execute("ALTER TABLE live_trade_state ADD COLUMN qty INTEGER")
            conn.commit()

        # v7.11 drift-protective-stop per-trade state (PC400 gap-DN PUT).
        # Tracked on the open position so a runner restart cannot lose the
        # confirmation/armed latches mid-trade.
        state_cols = _cols("live_trade_state")
        for col, decl in (
            ("drift_min_alpha", "REAL"),
            ("drift_confirmed", "INTEGER DEFAULT 0"),
            ("drift_armed", "INTEGER DEFAULT 0"),
            ("drift_stop", "REAL"),
        ):
            if col not in state_cols:
                conn.execute(
                    f"ALTER TABLE live_trade_state ADD COLUMN {col} {decl}")
                conn.commit()

        for _t in (
            "live_users",
            "live_broker_connections",
            "live_credentials_enc",
            "live_config",
            "live_orders",
            "live_trades",
            "live_trade_state",
            "live_day_pnl",
        ):
            _ = _cols(_t)
    finally:
        if own_conn:
            conn.close()

    print("[labs-live-db] Live schema ready")


if __name__ == "__main__":
    init_live_db()
