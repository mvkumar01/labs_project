"""
live_service.py — DB/config CRUD for live_* tables, ALWAYS scoped by user_id
(and conn_id where a connection is implied) + user-management API + encrypted
cred storage.

MULTI-USER (spec §2, §8): there is NO global getter/setter. Every config read/
write is keyed by the composite (user_id, conn_id, key) PK of live_config, so
one user's mode / kill_switch / lots / armed / daily_loss can NEVER affect
another. Per-connection tables (live_orders, live_trades, live_trade_state,
live_day_pnl, live_broker_connections, live_credentials_enc) all carry
user_id; reads filter on it.

Isolation (spec §1.4): imports ONLY neutral infra (storage.live_db,
config.labs_config). NEVER imports labs.engine.* / labs.services.* and NEVER
imports a broker SDK. No real orders here — this module touches the DB /
encrypted cred store only.

Credentials: Fernet envelope encryption, key from env LABS_CRED_KEY,
ciphertext persisted per conn_id (live_credentials_enc BLOB + a gitignored
mirror under storage/state/). TOTP secret / PIN / password / api_secret /
access_token are WRITE-ONLY — never returned by any read path, never logged.
"""
import hmac
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.labs_config import STATE_DIR
from storage.live_db import get_live_conn, init_live_db


# ── credential secret field names that must NEVER be echoed back ──────────
WRITE_ONLY_FIELDS = ("totp_secret", "pin", "password", "api_secret", "access_token")

# ── live_config canonical defaults (spec §3 table). All per-(user, conn). ──
CONFIG_DEFAULTS = {
    "mode": "DISARMED",
    "kill_switch": "0",
    "lots": "1",
    "daily_loss_cap": "3000",
    "bot_variant": "hybrid_alpha_v28",
    "armed": "0",
    "strategy_version": "hybrid_alpha_v28",
    "intent_seq": "0",
    "reconcile_blocked": "0",
    "reconcile_message": "",
    "runner_owner": "",
}

# Fernet ciphertext mirror file (gitignored). One blob per conn_id keyed inside.
_CRED_STORE = STATE_DIR / "creds_enc.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_ist_iso() -> str:
    # IST = UTC+5:30. Daily counters/PnL roll on the IST calendar date.
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()


def conn_id_for(user_id: str, broker: str) -> str:
    """Deterministic per-user broker-connection id (spec §2)."""
    return f"{user_id}:{broker}"


# ══════════════════════════════════════════════════════════════════════════
# USER MANAGEMENT (spec §10) — live_users
# ══════════════════════════════════════════════════════════════════════════
def _hash_passcode(passcode: str) -> str:
    """Hash with bcrypt if available, else PBKDF2-HMAC-SHA256 (stdlib).
    Returns an algorithm-tagged string so verify_user picks the right path."""
    try:
        import bcrypt
        return "bcrypt$" + bcrypt.hashpw(passcode.encode(), bcrypt.gensalt()).decode()
    except Exception:
        import hashlib
        import base64
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, 200_000)
        return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def _verify_passcode(passcode: str, stored: str) -> bool:
    try:
        if stored.startswith("bcrypt$"):
            import bcrypt
            return bcrypt.checkpw(passcode.encode(), stored[len("bcrypt$"):].encode())
        if stored.startswith("pbkdf2$"):
            import hashlib
            import base64
            _, b_salt, b_dk = stored.split("$", 2)
            salt = base64.b64decode(b_salt)
            expected = base64.b64decode(b_dk)
            dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, 200_000)
            return hmac.compare_digest(dk, expected)
    except Exception:
        return False
    return False


def create_user(username: str, passcode: str, conn: sqlite3.Connection = None) -> str:
    """Register a user. Rejects duplicate username (UNIQUE). Returns user_id.
    Raises ValueError on duplicate. Passcode hashed; never stored in clear."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        existing = conn.execute(
            "SELECT user_id FROM live_users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            raise ValueError("username_taken")
        user_id = uuid.uuid4().hex
        with conn:
            conn.execute(
                "INSERT INTO live_users (user_id, username, passcode_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, username, _hash_passcode(passcode), _now_iso()),
            )
        return user_id
    finally:
        if own:
            conn.close()


def verify_user(username: str, passcode: str, conn: sqlite3.Connection = None) -> str | None:
    """Return user_id on a valid (username, passcode), else None. Never reveals
    whether the username exists (constant-ish path)."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        row = conn.execute(
            "SELECT user_id, passcode_hash FROM live_users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            # Spend a hash anyway to blunt user-enumeration timing.
            _verify_passcode(passcode, "pbkdf2$AAAA$AAAA")
            return None
        if _verify_passcode(passcode, row["passcode_hash"]):
            return row["user_id"]
        return None
    finally:
        if own:
            conn.close()


def get_user(user_id: str, conn: sqlite3.Connection = None) -> dict | None:
    """Return a SAFE user dict (no passcode_hash) or None."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        row = conn.execute(
            "SELECT user_id, username, created_at FROM live_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# live_config — per-(user_id, conn_id) typed get/set
# ══════════════════════════════════════════════════════════════════════════
def get_config(user_id: str, conn_id: str, key: str,
               conn: sqlite3.Connection = None) -> str:
    """Return raw string value for (user_id, conn_id, key), or its default."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        row = conn.execute(
            "SELECT value FROM live_config WHERE user_id = ? AND conn_id = ? AND key = ?",
            (user_id, conn_id, key),
        ).fetchone()
        if row is not None and row["value"] is not None:
            return row["value"]
        return CONFIG_DEFAULTS.get(key, "")
    finally:
        if own:
            conn.close()


def set_config(user_id: str, conn_id: str, key: str, value,
               conn: sqlite3.Connection = None) -> None:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        with conn:
            conn.execute(
                "INSERT INTO live_config (user_id, conn_id, key, value, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, conn_id, key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (user_id, conn_id, key, str(value), _now_iso()),
            )
    finally:
        if own:
            conn.close()


def get_config_int(user_id: str, conn_id: str, key: str,
                   conn: sqlite3.Connection = None) -> int:
    try:
        return int(float(get_config(user_id, conn_id, key, conn)))
    except (TypeError, ValueError):
        return int(float(CONFIG_DEFAULTS.get(key, "0") or 0))


def get_config_float(user_id: str, conn_id: str, key: str,
                     conn: sqlite3.Connection = None) -> float:
    try:
        return float(get_config(user_id, conn_id, key, conn))
    except (TypeError, ValueError):
        return float(CONFIG_DEFAULTS.get(key, "0") or 0.0)


# Convenience accessors — all per-(user, conn) -------------------------------
def get_mode(user_id, conn_id, conn=None) -> str:
    return get_config(user_id, conn_id, "mode", conn) or "DISARMED"


def is_kill_switch_on(user_id, conn_id, conn=None) -> bool:
    return get_config_int(user_id, conn_id, "kill_switch", conn) == 1


def get_lots(user_id, conn_id, conn=None) -> int:
    return get_config_int(user_id, conn_id, "lots", conn)


def is_armed(user_id, conn_id, conn=None) -> bool:
    return get_config_int(user_id, conn_id, "armed", conn) == 1


def get_daily_loss_cap(user_id, conn_id, conn=None) -> float:
    return get_config_float(user_id, conn_id, "daily_loss_cap", conn)


# ══════════════════════════════════════════════════════════════════════════
# live_broker_connections — CRUD (per user)
# ══════════════════════════════════════════════════════════════════════════
def upsert_connection(user_id: str, conn_id: str, broker: str,
                      account_label: str = None, account_ref: str = None,
                      status: str = "disconnected",
                      conn: sqlite3.Connection = None) -> None:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        now = _now_iso()
        with conn:
            conn.execute(
                "INSERT INTO live_broker_connections "
                "(conn_id, user_id, broker, account_label, account_ref, status, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conn_id) DO UPDATE SET "
                "broker=excluded.broker, account_label=excluded.account_label, "
                "account_ref=COALESCE(excluded.account_ref, live_broker_connections.account_ref), "
                "status=excluded.status, updated_at=excluded.updated_at",
                (conn_id, user_id, broker, account_label, account_ref, status, now, now),
            )
    finally:
        if own:
            conn.close()


def set_connection_status(user_id: str, conn_id: str, status: str,
                          conn: sqlite3.Connection = None) -> None:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        now = _now_iso()
        connected_at = now if status == "connected" else None
        with conn:
            conn.execute(
                "UPDATE live_broker_connections SET status = ?, "
                "connected_at = COALESCE(?, connected_at), updated_at = ? "
                "WHERE conn_id = ? AND user_id = ?",
                (status, connected_at, now, conn_id, user_id),
            )
    finally:
        if own:
            conn.close()


def update_connection_funds(user_id: str, conn_id: str, funds_available,
                            error: str = "", conn: sqlite3.Connection = None) -> None:
    """Persist a non-secret broker funds snapshot for the live dashboard."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        now = _now_iso()
        funds = None if funds_available is None else float(funds_available)
        with conn:
            conn.execute(
                "UPDATE live_broker_connections SET funds_available = ?, "
                "funds_updated_at = ?, funds_error = ?, updated_at = ? "
                "WHERE conn_id = ? AND user_id = ?",
                (funds, now, str(error or "")[:200], now, conn_id, user_id),
            )
    finally:
        if own:
            conn.close()


def get_connection(user_id: str, conn_id: str,
                   conn: sqlite3.Connection = None) -> dict:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        row = conn.execute(
            "SELECT * FROM live_broker_connections WHERE conn_id = ? AND user_id = ?",
            (conn_id, user_id),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        if own:
            conn.close()


def list_user_connections(user_id: str, conn: sqlite3.Connection = None) -> list:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM live_broker_connections WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def account_ref_claimed_by_other(user_id: str, conn_id: str, account_ref: str,
                                 conn: sqlite3.Connection = None) -> bool:
    """True if account_ref is already bound to a DIFFERENT connection
    (gate 4 / spec §12.2). UNIQUE(account_ref) also enforces this at the DB
    layer; this is the readable lookup the gate uses."""
    if not account_ref:
        return False
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        row = conn.execute(
            "SELECT conn_id FROM live_broker_connections "
            "WHERE account_ref = ? AND conn_id != ?",
            (account_ref, conn_id),
        ).fetchone()
        return row is not None
    finally:
        if own:
            conn.close()


def active_connections(conn: sqlite3.Connection = None) -> list:
    """Return [(user_id, conn_id), …] for every connection whose mode is NOT
    DISARMED. Used by the runner to iterate ALL active users (spec §7).
    DISARMED connections have no mode row OR mode='DISARMED' — excluded."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        rows = conn.execute(
            "SELECT user_id, conn_id, value FROM live_config WHERE key = 'mode'"
        ).fetchall()
        out = []
        for r in rows:
            if (r["value"] or "DISARMED") != "DISARMED":
                out.append((r["user_id"], r["conn_id"]))
        return out
    finally:
        if own:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# live_orders — idempotency ledger CRUD (per user/conn)
# ══════════════════════════════════════════════════════════════════════════
def insert_order_ledger(idem_key: str, *, user_id: str, conn_id: str,
                        trade_date: str, strategy_version: str,
                        bar_timestamp: str, action: str, side: str,
                        entry_rule: str, intent_seq: int, symbol: str,
                        qty: int, order_type: str, limit_price: float,
                        dry_run: int, conn: sqlite3.Connection = None) -> bool:
    """INSERT OR IGNORE a PENDING ledger row. Returns True if a NEW row was
    inserted (caller may proceed); False if the key already existed (caller
    must SKIP the broker call — double-click / restart re-fire / same-bar
    re-entry defence). idem_key embeds conn_id (== user_id:broker) so keys are
    inherently user-scoped."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO live_orders "
                "(idem_key, user_id, conn_id, trade_date, strategy_version, "
                " bar_timestamp, action, side, entry_rule, intent_seq, symbol, "
                " qty, order_type, limit_price, status, dry_run, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)",
                (idem_key, user_id, conn_id, trade_date, strategy_version,
                 bar_timestamp, action, side, entry_rule, intent_seq, symbol,
                 qty, order_type, limit_price, dry_run, _now_iso()),
            )
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def get_order_ledger(idem_key: str, conn: sqlite3.Connection = None) -> dict:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        row = conn.execute(
            "SELECT * FROM live_orders WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        if own:
            conn.close()


def update_order_ledger(idem_key: str, *, status: str = None,
                        broker_order_id: str = None, avg_fill_price: float = None,
                        placed_at: str = None, filled_at: str = None,
                        conn: sqlite3.Connection = None) -> None:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        sets, params = [], []
        if status is not None:
            sets.append("status = ?"); params.append(status)
        if broker_order_id is not None:
            sets.append("broker_order_id = ?"); params.append(broker_order_id)
        if avg_fill_price is not None:
            sets.append("avg_fill_price = ?"); params.append(avg_fill_price)
        if placed_at is not None:
            sets.append("placed_at = ?"); params.append(placed_at)
        if filled_at is not None:
            sets.append("filled_at = ?"); params.append(filled_at)
        if not sets:
            return
        params.append(idem_key)
        with conn:
            conn.execute(
                f"UPDATE live_orders SET {', '.join(sets)} WHERE idem_key = ?",
                params,
            )
    finally:
        if own:
            conn.close()


def recent_orders(user_id: str, conn_id: str, limit: int = 20,
                  conn: sqlite3.Connection = None) -> list:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM live_orders WHERE user_id = ? AND conn_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, conn_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# live_trade_state — DB-backed single-row position state per connection
# ══════════════════════════════════════════════════════════════════════════
def _default_trade_state(user_id: str, conn_id: str) -> dict:
    return {
        "conn_id": conn_id,
        "user_id": user_id,
        "position": "NONE",
        "side": None,
        "symbol": None,
        "entry_spot": None,
        "entry_time": None,
        "entry_price": None,
        "virtual": 0,
        "peak_pnl": 0.0,
        "entry_rule": None,
        "max_alpha_seen": None,
        "entry_grace_until": None,
        "daily_trades_date": None,
        "daily_trades_by_tier": "{}",
        "updated_at": None,
    }


def get_trade_state(user_id: str, conn_id: str,
                    conn: sqlite3.Connection = None) -> dict:
    """Load this conn's trade-state row (creating a default if absent)."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        row = conn.execute(
            "SELECT * FROM live_trade_state WHERE conn_id = ? AND user_id = ?",
            (conn_id, user_id),
        ).fetchone()
        if row is None:
            st = _default_trade_state(user_id, conn_id)
            save_trade_state(user_id, conn_id, st, conn=conn)
            return st
        return dict(row)
    finally:
        if own:
            conn.close()


def save_trade_state(user_id: str, conn_id: str, state: dict,
                     conn: sqlite3.Connection = None) -> None:
    """Atomic per-conn upsert for restart-safe position state."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        by_tier = state.get("daily_trades_by_tier")
        if isinstance(by_tier, (dict, list)):
            by_tier = json.dumps(by_tier)
        with conn:
            conn.execute(
                "INSERT INTO live_trade_state "
                "(conn_id, user_id, position, side, symbol, entry_spot, entry_time, "
                " entry_price, virtual, peak_pnl, entry_rule, max_alpha_seen, "
                " entry_grace_until, daily_trades_date, daily_trades_by_tier, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conn_id) DO UPDATE SET "
                "position=excluded.position, side=excluded.side, symbol=excluded.symbol, "
                "entry_spot=excluded.entry_spot, entry_time=excluded.entry_time, "
                "entry_price=excluded.entry_price, virtual=excluded.virtual, "
                "peak_pnl=excluded.peak_pnl, entry_rule=excluded.entry_rule, "
                "max_alpha_seen=excluded.max_alpha_seen, "
                "entry_grace_until=excluded.entry_grace_until, "
                "daily_trades_date=excluded.daily_trades_date, "
                "daily_trades_by_tier=excluded.daily_trades_by_tier, "
                "updated_at=excluded.updated_at",
                (conn_id, user_id, state.get("position", "NONE"), state.get("side"),
                 state.get("symbol"), state.get("entry_spot"), state.get("entry_time"),
                 state.get("entry_price"), int(state.get("virtual", 0) or 0),
                 state.get("peak_pnl", 0.0), state.get("entry_rule"),
                 state.get("max_alpha_seen"), state.get("entry_grace_until"),
                 state.get("daily_trades_date"), by_tier or "{}", _now_iso()),
            )
    finally:
        if own:
            conn.close()


def reset_trade_state(user_id: str, conn_id: str,
                      conn: sqlite3.Connection = None) -> dict:
    """Reset to flat, preserving the daily per-tier counters."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        prev = get_trade_state(user_id, conn_id, conn=conn)
        st = _default_trade_state(user_id, conn_id)
        st["daily_trades_date"] = prev.get("daily_trades_date")
        st["daily_trades_by_tier"] = prev.get("daily_trades_by_tier") or "{}"
        save_trade_state(user_id, conn_id, st, conn=conn)
        return st
    finally:
        if own:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# live_day_pnl — per-IST-date realized PnL per conn (daily-loss source)
# ══════════════════════════════════════════════════════════════════════════
def get_day_pnl(user_id: str, conn_id: str, trade_date: str = None,
                conn: sqlite3.Connection = None) -> dict:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        trade_date = trade_date or _today_ist_iso()
        row = conn.execute(
            "SELECT * FROM live_day_pnl WHERE trade_date = ? AND conn_id = ?",
            (trade_date, conn_id),
        ).fetchone()
        if row is None:
            return {"trade_date": trade_date, "user_id": user_id, "conn_id": conn_id,
                    "realized_pnl": 0.0, "trade_count": 0, "halted": 0}
        return dict(row)
    finally:
        if own:
            conn.close()


def add_day_pnl(user_id: str, conn_id: str, delta_pnl: float,
                trade_date: str = None, conn: sqlite3.Connection = None) -> None:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        trade_date = trade_date or _today_ist_iso()
        with conn:
            conn.execute(
                "INSERT INTO live_day_pnl "
                "(trade_date, user_id, conn_id, realized_pnl, trade_count, halted) "
                "VALUES (?, ?, ?, ?, 1, 0) "
                "ON CONFLICT(trade_date, conn_id) DO UPDATE SET "
                "realized_pnl = live_day_pnl.realized_pnl + excluded.realized_pnl, "
                "trade_count = live_day_pnl.trade_count + 1",
                (trade_date, user_id, conn_id, delta_pnl),
            )
    finally:
        if own:
            conn.close()


def set_day_halted(user_id: str, conn_id: str, halted: int = 1,
                   trade_date: str = None, conn: sqlite3.Connection = None) -> None:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        trade_date = trade_date or _today_ist_iso()
        with conn:
            conn.execute(
                "INSERT INTO live_day_pnl "
                "(trade_date, user_id, conn_id, realized_pnl, trade_count, halted) "
                "VALUES (?, ?, ?, 0, 0, ?) "
                "ON CONFLICT(trade_date, conn_id) DO UPDATE SET halted = excluded.halted",
                (trade_date, user_id, conn_id, int(halted)),
            )
    finally:
        if own:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# live_trades — round-trip trades for PnL/audit (per user/conn)
# ══════════════════════════════════════════════════════════════════════════
def record_trade(user_id: str, conn_id: str, *, side: str, symbol: str,
                 entry_price: float, exit_price: float, qty: int, pnl: float,
                 entry_time: str, exit_time: str, reason: str, dry_run: int,
                 conn: sqlite3.Connection = None) -> str:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        trade_id = uuid.uuid4().hex
        with conn:
            conn.execute(
                "INSERT INTO live_trades "
                "(trade_id, user_id, conn_id, side, symbol, entry_price, exit_price, "
                " qty, pnl, entry_time, exit_time, reason, dry_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trade_id, user_id, conn_id, side, symbol, entry_price, exit_price,
                 qty, pnl, entry_time, exit_time, reason, int(dry_run)),
            )
        return trade_id
    finally:
        if own:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# Encrypted credential storage (Fernet) — keyed per conn_id
# ══════════════════════════════════════════════════════════════════════════
def _fernet():
    """Build a Fernet from env LABS_CRED_KEY. Deferred import so the module
    loads even when `cryptography` isn't installed (Phase-0 dry-run / CI)."""
    key = os.environ.get("LABS_CRED_KEY")
    if not key:
        raise RuntimeError(
            "LABS_CRED_KEY env var not set — cannot encrypt/decrypt live creds."
        )
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key)


def store_credentials(user_id: str, conn_id: str, broker: str, creds: dict,
                      conn: sqlite3.Connection = None) -> None:
    """Fernet-encrypt the cred dict and persist the ciphertext in
    live_credentials_enc (keyed by conn_id) AND a gitignored mirror under
    storage/state/.

    SECURITY: the plaintext `creds` dict (TOTP/PIN/password/secret/token) is
    NEVER logged or echoed. Only the ciphertext blob is written.
    """
    f = _fernet()
    token = f.encrypt(json.dumps(creds).encode())

    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        now = _now_iso()
        with conn:
            conn.execute(
                "INSERT INTO live_credentials_enc "
                "(conn_id, user_id, broker, ciphertext, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conn_id) DO UPDATE SET "
                "ciphertext=excluded.ciphertext, updated_at=excluded.updated_at",
                (conn_id, user_id, broker, token, now, now),
            )
    finally:
        if own:
            conn.close()

    # Gitignored on-disk mirror (defensive; DB is the source of truth).
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    store = {}
    if _CRED_STORE.exists():
        try:
            store = json.loads(_CRED_STORE.read_text())
        except Exception:
            store = {}
    store[conn_id] = token.decode()
    tmp = _CRED_STORE.with_suffix(_CRED_STORE.suffix + ".tmp")
    tmp.write_text(json.dumps(store))
    tmp.replace(_CRED_STORE)


def load_credentials(user_id: str, conn_id: str,
                     conn: sqlite3.Connection = None) -> dict:
    """Decrypt and return THIS conn's cred dict IN MEMORY ONLY. Callers must
    never log/echo the result. Returns {} if no blob is stored. Scoped by
    user_id so one user can never load another's creds."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        row = conn.execute(
            "SELECT ciphertext FROM live_credentials_enc WHERE conn_id = ? AND user_id = ?",
            (conn_id, user_id),
        ).fetchone()
    finally:
        if own:
            conn.close()
    if row is None or row["ciphertext"] is None:
        return {}
    token = row["ciphertext"]
    if isinstance(token, str):
        token = token.encode()
    f = _fernet()
    return json.loads(f.decrypt(token).decode())


def credentials_status(user_id: str, conn_id: str,
                       conn: sqlite3.Connection = None) -> dict:
    """SAFE, echo-able status of which cred fields are set — values masked.
    WRITE_ONLY_FIELDS are reported as set/unset only, never returned. Used by
    GET routes so the UI can show `•••• set`."""
    try:
        creds = load_credentials(user_id, conn_id, conn)
    except Exception:
        return {"_error": "decrypt_failed"}
    return {k: ("set" if creds.get(k) else "unset") for k in creds}


# ══════════════════════════════════════════════════════════════════════════
def ensure_schema(conn: sqlite3.Connection = None) -> None:
    """Idempotent live_* schema init (delegates to storage.live_db)."""
    init_live_db(conn)
