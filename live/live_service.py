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
from csv import DictReader
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.labs_config import SHARED_LIVE_DIR, STATE_DIR, UNDERLYINGS
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
    # Alpha v2.10: which book this connection runs. "main" = RECO/Run-F book
    # (default, unchanged behaviour). "r2" = R2 consistency book (wall-range
    # @09:45 + VIX-scaled TP). A connection runs exactly one book; R2 must be a
    # SEPARATE connection from the main book (single-net-position reconcile).
    "book_role": "main",
    # Execution path. "single" (default) = today's one-book-per-conn flow
    # (incl. the book_role branch). "order_manager" = multi-source netting on
    # ONE account (main + r2 via the order_manager + live_source_ledger).
    "exec_mode": "single",
    # When exec_mode=order_manager: enable the R2 source alongside main.
    "om_r2_enabled": "0",
}

# Fernet ciphertext mirror file (gitignored). One blob per conn_id keyed inside.
_CRED_STORE = STATE_DIR / "creds_enc.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_ist_iso() -> str:
    # IST = UTC+5:30. Daily counters/PnL roll on the IST calendar date.
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()


def today_ist_iso() -> str:
    """Public wrapper for UI routes that need the live trading date."""
    return _today_ist_iso()


def conn_id_for(user_id: str, broker: str) -> str:
    """Deterministic per-user broker-connection id (spec §2)."""
    return f"{user_id}:{broker}"


_USER_PREF_CONN_ID = "__user__"
_SUPPORTED_BROKERS = {"angel", "zerodha"}
LIVE_UNDERLYING = "NIFTY"


def calc_option_charges(entry_price: float, exit_price: float, qty: int,
                        orders: int = 2) -> dict:
    """Charges formula supplied by the operator for live option P&L."""
    entry_price = float(entry_price or 0.0)
    exit_price = float(exit_price or 0.0)
    qty = abs(int(qty or 0))
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty
    total_turnover = buy_turnover + sell_turnover

    brokerage = 20 * orders
    stt = sell_turnover * 0.0015
    exchange_txn = total_turnover * 0.000325
    sebi = total_turnover * 0.000001
    stamp = buy_turnover * 0.00003
    gst = 0.18 * (brokerage + exchange_txn + sebi)
    total_charges = brokerage + stt + exchange_txn + sebi + stamp + gst

    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "sebi": round(sebi, 2),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "total_charges": round(total_charges, 2),
    }


def calc_net_option_pnl(entry_price: float, exit_price: float, qty: int) -> dict:
    qty = abs(int(qty or 0))
    gross_pnl = (float(exit_price or 0.0) - float(entry_price or 0.0)) * qty
    charges = calc_option_charges(entry_price, exit_price, qty)
    net_pnl = gross_pnl - charges["total_charges"]
    return {
        "gross_pnl": round(gross_pnl, 2),
        "charges": charges,
        "net_pnl": round(net_pnl, 2),
        "net_points": round(net_pnl / qty, 2) if qty else 0.0,
    }


def get_selected_broker(user_id: str, conn: sqlite3.Connection = None) -> str | None:
    """Return the user's persisted broker selection, if any.

    This is intentionally user-scoped rather than session-scoped so logout/login
    does not fall back to whichever broker row happened to be updated last.
    """
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        row = conn.execute(
            "SELECT value FROM live_config "
            "WHERE user_id = ? AND conn_id = ? AND key = 'selected_broker'",
            (user_id, _USER_PREF_CONN_ID),
        ).fetchone()
        broker = (row["value"] if row else "").strip().lower()
        return broker if broker in _SUPPORTED_BROKERS else None
    finally:
        if own:
            conn.close()


def set_selected_broker(user_id: str, broker: str,
                        conn: sqlite3.Connection = None) -> None:
    broker = (broker or "").strip().lower()
    if broker not in _SUPPORTED_BROKERS:
        return
    set_config(user_id, _USER_PREF_CONN_ID, "selected_broker", broker, conn)


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


def get_book_role(user_id, conn_id, conn=None) -> str:
    """'main' (RECO/Run-F) or 'r2' (R2 consistency book). Default 'main'."""
    role = (get_config(user_id, conn_id, "book_role", conn) or "main").strip().lower()
    return "r2" if role == "r2" else "main"


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
        # Attach realized PnL to EXIT rows. live_orders has no PnL of its own;
        # the round-trip result lives in live_trades, recorded in the same exit
        # cycle (both timestamps are _now_iso() UTC). Match on the natural key
        # (conn/symbol/side/dry_run) and pick the trade whose exit_time is
        # closest to this order's created_at (guards re-entries on one symbol).
        # ENTER rows and any unmatched EXIT get NULL. Read-only — the order
        # write path is untouched.
        rows = conn.execute(
            "SELECT o.*, ("
            "  SELECT COALESCE(t.net_pnl, t.pnl) FROM live_trades t "
            "  WHERE o.action = 'EXIT' "
            "    AND t.user_id = o.user_id AND t.conn_id = o.conn_id "
            "    AND t.symbol = o.symbol AND t.side = o.side "
            "    AND t.dry_run = o.dry_run "
            "    AND ABS(julianday(t.exit_time) - julianday(o.created_at)) < (60.0 / 86400.0) "
            "  ORDER BY ABS(julianday(t.exit_time) - julianday(o.created_at)) ASC "
            "  LIMIT 1"
            ") AS net_pnl "
            "FROM live_orders o WHERE o.user_id = ? AND o.conn_id = ? "
            "ORDER BY o.created_at DESC LIMIT ?",
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
        "qty": None,
        "virtual": 0,
        "peak_pnl": 0.0,
        "entry_rule": None,
        "max_alpha_seen": None,
        "entry_grace_until": None,
        "daily_trades_date": None,
        "daily_trades_by_tier": "{}",
        "recovery_armed": 0,
        "recovery_level": None,
        "recovery_side": None,
        "spot_stop_bar": None,
        "champion_trade_date": None,
        "champion_closed_count": 0,
        "champion_last_event_id": None,
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
                " entry_price, qty, virtual, peak_pnl, entry_rule, max_alpha_seen, "
                " entry_grace_until, daily_trades_date, daily_trades_by_tier, "
                " recovery_armed, recovery_level, recovery_side, spot_stop_bar, "
                " champion_trade_date, champion_closed_count, champion_last_event_id, "
                " updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                " ?, ?, ?, ?) "
                "ON CONFLICT(conn_id) DO UPDATE SET "
                "position=excluded.position, side=excluded.side, symbol=excluded.symbol, "
                "entry_spot=excluded.entry_spot, entry_time=excluded.entry_time, "
                "entry_price=excluded.entry_price, virtual=excluded.virtual, "
                "qty=excluded.qty, "
                "peak_pnl=excluded.peak_pnl, entry_rule=excluded.entry_rule, "
                "max_alpha_seen=excluded.max_alpha_seen, "
                "entry_grace_until=excluded.entry_grace_until, "
                "daily_trades_date=excluded.daily_trades_date, "
                "daily_trades_by_tier=excluded.daily_trades_by_tier, "
                "recovery_armed=excluded.recovery_armed, "
                "recovery_level=excluded.recovery_level, "
                "recovery_side=excluded.recovery_side, "
                "spot_stop_bar=excluded.spot_stop_bar, "
                "champion_trade_date=excluded.champion_trade_date, "
                "champion_closed_count=excluded.champion_closed_count, "
                "champion_last_event_id=excluded.champion_last_event_id, "
                "updated_at=excluded.updated_at",
                (conn_id, user_id, state.get("position", "NONE"), state.get("side"),
                 state.get("symbol"), state.get("entry_spot"), state.get("entry_time"),
                 state.get("entry_price"), state.get("qty"),
                 int(state.get("virtual", 0) or 0), state.get("peak_pnl", 0.0),
                 state.get("entry_rule"),
                 state.get("max_alpha_seen"), state.get("entry_grace_until"),
                 state.get("daily_trades_date"), by_tier or "{}",
                 int(state.get("recovery_armed", 0) or 0), state.get("recovery_level"),
                 state.get("recovery_side"), state.get("spot_stop_bar"),
                 state.get("champion_trade_date"),
                 int(state.get("champion_closed_count", 0) or 0),
                 state.get("champion_last_event_id"), _now_iso()),
            )
    finally:
        if own:
            conn.close()


def clear_v212_latches(user_id: str, conn_id: str,
                       conn: sqlite3.Connection = None) -> dict:
    """Clear v2.12-only recovery state without resetting replay cursor state."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        state = get_trade_state(user_id, conn_id, conn=conn)
        state.update({
            "recovery_armed": 0,
            "recovery_level": None,
            "recovery_side": None,
            "spot_stop_bar": None,
        })
        save_trade_state(user_id, conn_id, state, conn=conn)
        return state
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
        st["champion_trade_date"] = prev.get("champion_trade_date")
        st["champion_closed_count"] = int(
            prev.get("champion_closed_count") or 0
        )
        st["champion_last_event_id"] = prev.get("champion_last_event_id")
        save_trade_state(user_id, conn_id, st, conn=conn)
        return st
    finally:
        if own:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# live_source_ledger — multi-source order-manager per-(conn,source) positions
# ══════════════════════════════════════════════════════════════════════════
def get_ledger(user_id: str, conn_id: str,
               conn: sqlite3.Connection = None) -> dict:
    """Return {source: row-dict} for this conn's OPEN source positions
    (qty>0). Flat sources are absent. The runner maps rows -> SourcePos."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        rows = conn.execute(
            "SELECT source, symbol, side, qty, entry_price, entry_spot, "
            "entry_rule, entry_time, virtual FROM live_source_ledger "
            "WHERE user_id = ? AND conn_id = ? AND qty > 0",
            (user_id, conn_id),
        ).fetchall()
        return {r["source"]: dict(r) for r in rows}
    finally:
        if own:
            conn.close()


def set_source_pos(user_id: str, conn_id: str, source: str, *, symbol: str,
                   side: str, qty: int, entry_price: float, entry_spot=None,
                   entry_rule=None, entry_time=None, virtual: int = 0,
                   conn: sqlite3.Connection = None) -> None:
    """Upsert one source's open position."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        with conn:
            conn.execute(
                "INSERT INTO live_source_ledger "
                "(user_id, conn_id, source, symbol, side, qty, entry_price, "
                " entry_spot, entry_rule, entry_time, virtual, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id, conn_id, source) DO UPDATE SET "
                "symbol=excluded.symbol, side=excluded.side, qty=excluded.qty, "
                "entry_price=excluded.entry_price, entry_spot=excluded.entry_spot, "
                "entry_rule=excluded.entry_rule, entry_time=excluded.entry_time, "
                "virtual=excluded.virtual, updated_at=excluded.updated_at",
                (user_id, conn_id, source, symbol, side, int(qty),
                 float(entry_price or 0), entry_spot, entry_rule,
                 entry_time or _now_iso(), int(virtual or 0), _now_iso()),
            )
    finally:
        if own:
            conn.close()


def clear_source_pos(user_id: str, conn_id: str, source: str,
                     conn: sqlite3.Connection = None) -> None:
    """Flatten one source (delete its ledger row)."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        with conn:
            conn.execute(
                "DELETE FROM live_source_ledger "
                "WHERE user_id = ? AND conn_id = ? AND source = ?",
                (user_id, conn_id, source),
            )
    finally:
        if own:
            conn.close()


def get_exec_mode(user_id, conn_id, conn=None) -> str:
    """'single' (default — today's one-book-per-conn path) or 'order_manager'
    (multi-source netting on one account)."""
    m = (get_config(user_id, conn_id, "exec_mode", conn) or "single").strip().lower()
    return "order_manager" if m == "order_manager" else "single"


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
                    "realized_pnl": 0.0, "trade_count": 0,
                    "realized_pnl_dry": 0.0, "trade_count_dry": 0, "halted": 0}
        out = dict(row)
        # Older rows predate the dry/live split — surface explicit zeros.
        out.setdefault("realized_pnl_dry", 0.0)
        out.setdefault("trade_count_dry", 0)
        if out.get("realized_pnl_dry") is None:
            out["realized_pnl_dry"] = 0.0
        if out.get("trade_count_dry") is None:
            out["trade_count_dry"] = 0
        return out
    finally:
        if own:
            conn.close()


def add_day_pnl(user_id: str, conn_id: str, delta_pnl: float,
                trade_date: str = None, conn: sqlite3.Connection = None,
                dry_run: bool = False) -> None:
    """Accumulate realized PnL into the bucket matching the trade's mode.

    `realized_pnl`/`trade_count` are REAL-money only; dry-run results go to
    `realized_pnl_dry`/`trade_count_dry`. The two must never mix — display and
    the daily-loss gate both rely on this separation."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        trade_date = trade_date or _today_ist_iso()
        if dry_run:
            sql = (
                "INSERT INTO live_day_pnl "
                "(trade_date, user_id, conn_id, realized_pnl, trade_count, "
                " realized_pnl_dry, trade_count_dry, halted) "
                "VALUES (?, ?, ?, 0, 0, ?, 1, 0) "
                "ON CONFLICT(trade_date, conn_id) DO UPDATE SET "
                "realized_pnl_dry = COALESCE(live_day_pnl.realized_pnl_dry, 0) "
                "  + excluded.realized_pnl_dry, "
                "trade_count_dry = COALESCE(live_day_pnl.trade_count_dry, 0) + 1"
            )
        else:
            sql = (
                "INSERT INTO live_day_pnl "
                "(trade_date, user_id, conn_id, realized_pnl, trade_count, "
                " realized_pnl_dry, trade_count_dry, halted) "
                "VALUES (?, ?, ?, ?, 1, 0, 0, 0) "
                "ON CONFLICT(trade_date, conn_id) DO UPDATE SET "
                "realized_pnl = COALESCE(live_day_pnl.realized_pnl, 0) "
                "  + excluded.realized_pnl, "
                "trade_count = COALESCE(live_day_pnl.trade_count, 0) + 1"
            )
        with conn:
            conn.execute(sql, (trade_date, user_id, conn_id, delta_pnl))
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
                 strategy: str = None,
                 conn: sqlite3.Connection = None) -> str:
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        trade_id = uuid.uuid4().hex
        computed = calc_net_option_pnl(entry_price, exit_price, qty)
        gross_pnl = float(computed["gross_pnl"])
        charges_total = float(computed["charges"]["total_charges"])
        net_pnl = float(computed["net_pnl"])
        with conn:
            conn.execute(
                "INSERT INTO live_trades "
                "(trade_id, user_id, conn_id, side, symbol, entry_price, exit_price, "
                " qty, pnl, gross_pnl, charges_total, charges_json, net_pnl, "
                " entry_time, exit_time, reason, strategy, dry_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trade_id, user_id, conn_id, side, symbol, entry_price, exit_price,
                 qty, net_pnl, gross_pnl, charges_total,
                 json.dumps(computed["charges"], sort_keys=True), net_pnl,
                 entry_time, exit_time, reason, strategy, int(dry_run)),
            )
        return trade_id
    finally:
        if own:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# live_trades read helpers
# ══════════════════════════════════════════════════════════════════════════
def trade_history(user_id: str, conn_id: str, trade_date: str = None,
                  limit: int = 100, conn: sqlite3.Connection = None,
                  date_from: str = None, date_to: str = None) -> dict:
    """Completed live_trades for one user's selected broker connection.

    Date filtering: pass `date_from`/`date_to` (inclusive IST dates) for a
    range; `trade_date` remains as a single-day shorthand. Default = today.
    Summary PnL/count are returned SPLIT by mode (live vs dry) and never mixed
    into one number."""
    own = conn is None
    if own:
        conn = get_live_conn()
    try:
        date_from = date_from or trade_date or _today_ist_iso()
        date_to = date_to or trade_date or _today_ist_iso()
        if date_to < date_from:
            date_from, date_to = date_to, date_from
        limit = max(1, min(int(limit or 100), 500))
        date_expr = "substr(COALESCE(exit_time, entry_time, ''), 1, 10)"
        params = (user_id, conn_id, date_from, date_to)
        summary = conn.execute(
            f"SELECT "
            f"COALESCE(SUM(CASE WHEN dry_run = 0 THEN COALESCE(net_pnl, pnl) END), 0) AS live_pnl, "
            f"SUM(CASE WHEN dry_run = 0 THEN 1 ELSE 0 END) AS live_count, "
            f"COALESCE(SUM(CASE WHEN dry_run = 1 THEN COALESCE(net_pnl, pnl) END), 0) AS dry_pnl, "
            f"SUM(CASE WHEN dry_run = 1 THEN 1 ELSE 0 END) AS dry_count, "
            f"COUNT(*) AS trade_count "
            f"FROM live_trades "
            f"WHERE user_id = ? AND conn_id = ? AND {date_expr} BETWEEN ? AND ?",
            params,
        ).fetchone()
        rows = conn.execute(
            f"SELECT trade_id, side, symbol, entry_price, exit_price, qty, "
            f"COALESCE(net_pnl, pnl) AS pnl, gross_pnl, charges_total, net_pnl, "
            f"charges_json, entry_time, exit_time, reason, strategy, dry_run "
            f"FROM live_trades "
            f"WHERE user_id = ? AND conn_id = ? AND {date_expr} BETWEEN ? AND ? "
            f"ORDER BY COALESCE(exit_time, entry_time, '') DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        # Per-strategy PnL breakdown (live + dry split) so the UI can filter /
        # group realized PnL by which strategy produced each trade.
        by_strategy = conn.execute(
            f"SELECT COALESCE(strategy, '(unset)') AS strategy, "
            f"COALESCE(SUM(CASE WHEN dry_run = 0 THEN COALESCE(net_pnl, pnl) END), 0) AS live_pnl, "
            f"SUM(CASE WHEN dry_run = 0 THEN 1 ELSE 0 END) AS live_count, "
            f"COALESCE(SUM(CASE WHEN dry_run = 1 THEN COALESCE(net_pnl, pnl) END), 0) AS dry_pnl, "
            f"SUM(CASE WHEN dry_run = 1 THEN 1 ELSE 0 END) AS dry_count "
            f"FROM live_trades "
            f"WHERE user_id = ? AND conn_id = ? AND {date_expr} BETWEEN ? AND ? "
            f"GROUP BY COALESCE(strategy, '(unset)') "
            f"ORDER BY 1",
            params,
        ).fetchall()
        live_pnl = float(summary["live_pnl"] if summary else 0.0)
        dry_pnl = float(summary["dry_pnl"] if summary else 0.0)
        return {
            "trade_date": date_from if date_from == date_to else None,
            "date_from": date_from,
            "date_to": date_to,
            # Back-compat field: LIVE money only (never mixes in dry results).
            "trade_pnl": live_pnl,
            "trade_count": int(summary["trade_count"] if summary else 0),
            "live_pnl": live_pnl,
            "live_count": int(summary["live_count"] or 0) if summary else 0,
            "dry_pnl": dry_pnl,
            "dry_count": int(summary["dry_count"] or 0) if summary else 0,
            "trades": [dict(r) for r in rows],
            "by_strategy": [dict(r) for r in by_strategy],
        }
    finally:
        if own:
            conn.close()


def _parse_iso_to_ist_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.date().isoformat()
        from datetime import timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        return dt.astimezone(ist).date().isoformat()
    except Exception:
        return None


def _latest_option_ltp_from_csv(symbol: str, trade_date: str) -> dict:
    path = SHARED_LIVE_DIR / trade_date / f"{LIVE_UNDERLYING}_options_1min.csv"
    if not path.exists():
        return {"ok": False, "error": "market_data_missing", "path": str(path)}

    latest = None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = DictReader(handle)
            for row in reader:
                if str(row.get("tradingsymbol") or "").strip() != symbol:
                    continue
                try:
                    ltp = float(row.get("ltp") or 0.0)
                except (TypeError, ValueError):
                    continue
                if ltp <= 0:
                    continue
                latest = {
                    "ltp": ltp,
                    "timestamp": row.get("timestamp"),
                    "spot": row.get("spot"),
                }
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "path": str(path)}
    if latest is None:
        return {"ok": False, "error": "symbol_ltp_missing", "path": str(path)}
    latest["ok"] = True
    latest["path"] = str(path)
    return latest


def open_position_mtm(user_id: str, conn_id: str,
                      conn: sqlite3.Connection = None) -> dict:
    """Estimated open-position MTM from shared 1-minute option data."""
    st = get_trade_state(user_id, conn_id, conn=conn)
    if (st.get("position") or "").upper() != "OPEN":
        return {"open": False}

    symbol = str(st.get("symbol") or "").strip()
    entry_price = float(st.get("entry_price") or 0.0)
    qty = int(st.get("qty") or 0)
    if qty <= 0:
        qty = get_lots(user_id, conn_id, conn) * UNDERLYINGS[LIVE_UNDERLYING]["lot_size"]
    trade_date = _parse_iso_to_ist_date(st.get("entry_time")) or _today_ist_iso()

    ltp_row = _latest_option_ltp_from_csv(symbol, trade_date)
    base = {
        "open": True,
        "side": st.get("side"),
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "entry_time": st.get("entry_time"),
        "entry_rule": st.get("entry_rule"),
        "dry_run": 1 if int(st.get("virtual") or 0) else 0,
    }
    if not ltp_row.get("ok"):
        base.update({
            "ltp_available": False,
            "error": ltp_row.get("error"),
            "latest_price": None,
            "latest_time": None,
            "gross_pnl": None,
            "charges_total": None,
            "net_pnl": None,
            "net_points": None,
        })
        return base

    latest_price = float(ltp_row["ltp"])
    pnl_info = calc_net_option_pnl(entry_price, latest_price, qty)
    base.update({
        "ltp_available": True,
        "latest_price": latest_price,
        "latest_time": ltp_row.get("timestamp"),
        "spot": ltp_row.get("spot"),
        "gross_pnl": pnl_info["gross_pnl"],
        "charges": pnl_info["charges"],
        "charges_total": pnl_info["charges"]["total_charges"],
        "net_pnl": pnl_info["net_pnl"],
        "net_points": pnl_info["net_points"],
    })
    return base


# Encrypted credential storage (Fernet) - keyed per conn_id
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
