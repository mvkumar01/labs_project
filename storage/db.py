"""
SQLite connection + schema initialisation.
All tables use CREATE TABLE IF NOT EXISTS — safe to call on every startup.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.labs_config import DB_PATH


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all tables if they don't exist. Call once at app/runner startup."""
    conn = get_conn()
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS bots (
            bot_id        TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            underlying    TEXT NOT NULL,
            strategy_type TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'paused',
            signal_tf     TEXT NOT NULL DEFAULT '5min',
            manage_tf     TEXT NOT NULL DEFAULT '1min',
            cloned_from   TEXT,
            version       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_params (
            bot_id              TEXT PRIMARY KEY REFERENCES bots(bot_id),
            rsi_period          INTEGER NOT NULL DEFAULT 3,
            ema_fast_period     INTEGER NOT NULL DEFAULT 9,
            ema_slow_period     INTEGER NOT NULL DEFAULT 13,
            sma_period          INTEGER NOT NULL DEFAULT 50,
            rsi_oversold        REAL NOT NULL DEFAULT 25.0,
            rsi_overbought      REAL NOT NULL DEFAULT 75.0,
            entry_rules_json    TEXT NOT NULL DEFAULT '[]',
            exit_rules_json     TEXT NOT NULL DEFAULT '[]',
            spot_target_pts     REAL,
            spot_sl_pts         REAL,
            ltp_target_pts      REAL,
            ltp_sl_pts          REAL,
            max_trades_per_day  INTEGER NOT NULL DEFAULT 3,
            allow_reentry       INTEGER NOT NULL DEFAULT 0,
            session_start       TEXT NOT NULL DEFAULT '09:20',
            session_end         TEXT NOT NULL DEFAULT '15:15',
            expiry_mode         TEXT NOT NULL DEFAULT 'nearest_weekly',
            strike_mode         TEXT NOT NULL DEFAULT 'atm',
            strike_offset_pts   INTEGER NOT NULL DEFAULT 0,
            hold_same_contract  INTEGER NOT NULL DEFAULT 1,
            updated_at          TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS positions (
            position_id  TEXT PRIMARY KEY,
            bot_id       TEXT NOT NULL REFERENCES bots(bot_id),
            trade_date   TEXT NOT NULL,
            underlying   TEXT NOT NULL,
            side         TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            strike       INTEGER NOT NULL,
            expiry       TEXT NOT NULL,
            entry_time   TEXT NOT NULL,
            entry_ltp    REAL NOT NULL,
            entry_spot   REAL NOT NULL,
            lot_size     INTEGER NOT NULL,
            qty          INTEGER NOT NULL DEFAULT 1,
            status       TEXT NOT NULL DEFAULT 'open'
        );

        CREATE TABLE IF NOT EXISTS trades (
            trade_id        TEXT PRIMARY KEY,
            bot_id          TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            underlying      TEXT NOT NULL,
            side            TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            strike          INTEGER NOT NULL,
            expiry          TEXT NOT NULL,
            entry_time      TEXT NOT NULL,
            exit_time       TEXT NOT NULL,
            entry_ltp       REAL NOT NULL,
            exit_ltp        REAL NOT NULL,
            entry_spot      REAL NOT NULL,
            exit_spot       REAL NOT NULL,
            lot_size        INTEGER NOT NULL,
            qty             INTEGER NOT NULL,
            pnl_pts         REAL NOT NULL,
            pnl_rs          REAL NOT NULL,
            charges         REAL NOT NULL DEFAULT 0,
            net_pnl_rs      REAL NOT NULL DEFAULT 0,
            exit_reason     TEXT NOT NULL,
            holding_mins    INTEGER NOT NULL,
            signal_at_entry REAL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS signals (
            signal_id   TEXT PRIMARY KEY,
            bot_id      TEXT NOT NULL,
            ts          TEXT NOT NULL,
            underlying  TEXT NOT NULL,
            bar_close   REAL NOT NULL,
            rsi         REAL,
            ema_fast    REAL,
            ema_slow    REAL,
            sma         REAL,
            signal_type TEXT NOT NULL,
            acted       INTEGER NOT NULL DEFAULT 0,
            skip_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_summary (
            summary_id      TEXT PRIMARY KEY,
            bot_id          TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            underlying      TEXT NOT NULL,
            total_trades    INTEGER NOT NULL DEFAULT 0,
            winning_trades  INTEGER NOT NULL DEFAULT 0,
            losing_trades   INTEGER NOT NULL DEFAULT 0,
            pnl_pts         REAL NOT NULL DEFAULT 0,
            pnl_rs          REAL NOT NULL DEFAULT 0,
            max_drawdown_rs REAL,
            avg_hold_mins   REAL,
            UNIQUE(bot_id, trade_date)
        );

        CREATE TABLE IF NOT EXISTS lab_bot_legs (
            id                      TEXT PRIMARY KEY,
            bot_id                  TEXT NOT NULL REFERENCES bots(bot_id),
            leg_code                TEXT NOT NULL,
            is_enabled              INTEGER NOT NULL DEFAULT 1,
            entry_logic             TEXT NOT NULL DEFAULT 'AND',
            entry_conditions_json   TEXT NOT NULL DEFAULT '[]',
            entry_gates_json        TEXT NOT NULL DEFAULT '[]',
            exit_conditions_json    TEXT NOT NULL DEFAULT '[]',
            stoploss_conditions_json TEXT NOT NULL DEFAULT '[]',
            created_at              TEXT NOT NULL,
            UNIQUE(bot_id, leg_code)
        );
        """)
    # Safe migrations
    pos_cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
    if "leg_code" not in pos_cols:
        conn.execute("ALTER TABLE positions ADD COLUMN leg_code TEXT")
        conn.commit()

    trade_cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    if "charges" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN charges REAL NOT NULL DEFAULT 0")
        conn.commit()
    if "net_pnl_rs" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN net_pnl_rs REAL NOT NULL DEFAULT 0")
        conn.commit()

    conn.close()
    print(f"[labs-db] Schema ready: {DB_PATH}")


if __name__ == "__main__":
    init_db()
