"""
CRUD operations for bots and bot_params.
All functions accept an optional conn; if None they open+close their own.
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Any

import pytz

from storage.db import get_conn

IST = pytz.timezone("Asia/Kolkata")
log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_bot(bot_id: str, conn=None) -> dict | None:
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        row = conn.execute("""
            SELECT b.*, p.rsi_period, p.ema_fast_period, p.ema_slow_period, p.sma_period,
                   p.rsi_oversold, p.rsi_overbought, p.entry_rules_json, p.exit_rules_json,
                   p.spot_target_pts, p.spot_sl_pts, p.ltp_target_pts, p.ltp_sl_pts,
                   p.max_trades_per_day, p.allow_reentry, p.session_start, p.session_end,
                   p.expiry_mode, p.strike_mode, p.strike_offset_pts, p.hold_same_contract
            FROM bots b JOIN bot_params p ON b.bot_id = p.bot_id
            WHERE b.bot_id = ?
        """, (bot_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def list_bots(conn=None) -> list[dict]:
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT b.*, p.rsi_period, p.ema_fast_period, p.ema_slow_period, p.sma_period,
                   p.rsi_oversold, p.rsi_overbought, p.entry_rules_json, p.exit_rules_json,
                   p.spot_target_pts, p.spot_sl_pts, p.ltp_target_pts, p.ltp_sl_pts,
                   p.max_trades_per_day, p.allow_reentry, p.session_start, p.session_end,
                   p.expiry_mode, p.strike_mode, p.strike_offset_pts, p.hold_same_contract
            FROM bots b JOIN bot_params p ON b.bot_id = p.bot_id
            ORDER BY b.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_bot(data: dict[str, Any], conn=None) -> dict:
    """
    data must contain: name, underlying, strategy_type.
    All other fields use defaults from bot_params schema.
    Returns the full bot dict.
    """
    close = conn is None
    if conn is None:
        conn = get_conn()

    bot_id = data.get("bot_id") or _make_slug(data["name"])
    now    = _now()

    bot = {
        "bot_id":        bot_id,
        "name":          data["name"],
        "underlying":    data["underlying"],
        "strategy_type": data["strategy_type"],
        "status":        data.get("status", "paused"),
        "signal_tf":     data.get("signal_tf", "5min"),
        "manage_tf":     data.get("manage_tf", "1min"),
        "cloned_from":   data.get("cloned_from"),
        "version":       int(data.get("version", 1)),
        "created_at":    now,
        "updated_at":    now,
    }

    params = {
        "bot_id":             bot_id,
        "rsi_period":         int(data.get("rsi_period", 3)),
        "ema_fast_period":    int(data.get("ema_fast_period", 9)),
        "ema_slow_period":    int(data.get("ema_slow_period", 13)),
        "sma_period":         int(data.get("sma_period", 50)),
        "rsi_oversold":       float(data.get("rsi_oversold", 25.0)),
        "rsi_overbought":     float(data.get("rsi_overbought", 75.0)),
        "entry_rules_json":   _to_json(data.get("entry_rules_json", "[]")),
        "exit_rules_json":    _to_json(data.get("exit_rules_json", "[]")),
        "spot_target_pts":    _float_or_none(data.get("spot_target_pts")),
        "spot_sl_pts":        _float_or_none(data.get("spot_sl_pts")),
        "ltp_target_pts":     _float_or_none(data.get("ltp_target_pts")),
        "ltp_sl_pts":         _float_or_none(data.get("ltp_sl_pts")),
        "max_trades_per_day": int(data.get("max_trades_per_day", 3)),
        "allow_reentry":      int(data.get("allow_reentry", 0)),
        "session_start":      data.get("session_start", "09:20"),
        "session_end":        data.get("session_end", "15:15"),
        "expiry_mode":        data.get("expiry_mode", "nearest_weekly"),
        "strike_mode":        data.get("strike_mode", "atm"),
        "strike_offset_pts":  int(data.get("strike_offset_pts", 0)),
        "hold_same_contract": int(data.get("hold_same_contract", 1)),
        "updated_at":         now,
    }

    try:
        with conn:
            conn.execute("""
                INSERT INTO bots VALUES (
                    :bot_id, :name, :underlying, :strategy_type, :status,
                    :signal_tf, :manage_tf, :cloned_from, :version,
                    :created_at, :updated_at
                )
            """, bot)
            conn.execute("""
                INSERT INTO bot_params VALUES (
                    :bot_id, :rsi_period, :ema_fast_period, :ema_slow_period, :sma_period,
                    :rsi_oversold, :rsi_overbought, :entry_rules_json, :exit_rules_json,
                    :spot_target_pts, :spot_sl_pts, :ltp_target_pts, :ltp_sl_pts,
                    :max_trades_per_day, :allow_reentry, :session_start, :session_end,
                    :expiry_mode, :strike_mode, :strike_offset_pts, :hold_same_contract,
                    :updated_at
                )
            """, params)
        return get_bot(bot_id, conn=conn)
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def update_bot(bot_id: str, data: dict[str, Any], conn=None) -> dict | None:
    close = conn is None
    if conn is None:
        conn = get_conn()
    now = _now()
    try:
        with conn:
            # Update bots table fields that are editable
            conn.execute("""
                UPDATE bots SET name=?, underlying=?, strategy_type=?, updated_at=?
                WHERE bot_id=?
            """, (data.get("name"), data.get("underlying"),
                  data.get("strategy_type"), now, bot_id))

            conn.execute("""
                UPDATE bot_params SET
                    rsi_period=?, ema_fast_period=?, ema_slow_period=?, sma_period=?,
                    rsi_oversold=?, rsi_overbought=?,
                    entry_rules_json=?, exit_rules_json=?,
                    spot_target_pts=?, spot_sl_pts=?, ltp_target_pts=?, ltp_sl_pts=?,
                    max_trades_per_day=?, allow_reentry=?,
                    session_start=?, session_end=?,
                    expiry_mode=?, strike_mode=?, strike_offset_pts=?, hold_same_contract=?,
                    updated_at=?
                WHERE bot_id=?
            """, (
                int(data.get("rsi_period", 3)),
                int(data.get("ema_fast_period", 9)),
                int(data.get("ema_slow_period", 13)),
                int(data.get("sma_period", 50)),
                float(data.get("rsi_oversold", 25.0)),
                float(data.get("rsi_overbought", 75.0)),
                _to_json(data.get("entry_rules_json", "[]")),
                _to_json(data.get("exit_rules_json", "[]")),
                _float_or_none(data.get("spot_target_pts")),
                _float_or_none(data.get("spot_sl_pts")),
                _float_or_none(data.get("ltp_target_pts")),
                _float_or_none(data.get("ltp_sl_pts")),
                int(data.get("max_trades_per_day", 3)),
                int(data.get("allow_reentry", 0)),
                data.get("session_start", "09:20"),
                data.get("session_end", "15:15"),
                data.get("expiry_mode", "nearest_weekly"),
                data.get("strike_mode", "atm"),
                int(data.get("strike_offset_pts", 0)),
                int(data.get("hold_same_contract", 1)),
                now,
                bot_id,
            ))
        return get_bot(bot_id, conn=conn)
    finally:
        if close:
            conn.close()


def update_status(bot_id: str, status: str, conn=None) -> None:
    assert status in ("active", "paused", "archived")
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        old_row = conn.execute(
            "SELECT status FROM bots WHERE bot_id=?", (bot_id,)
        ).fetchone()
        old_status = old_row["status"] if old_row else None
        cur = conn.execute(
            "UPDATE bots SET status=?, updated_at=? WHERE bot_id=?",
            (status, _now(), bot_id)
        )
        conn.commit()
        log.info(
            "[bot-status] bot_id=%s old_status=%s new_status=%s rows=%s commit=ok",
            bot_id,
            old_status,
            status,
            cur.rowcount,
        )
        _checkpoint_wal(conn, bot_id)
    finally:
        if close:
            conn.close()


def _checkpoint_wal(conn, bot_id: str) -> None:
    try:
        rows = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        log.info("[bot-status] bot_id=%s checkpoint=ok result=%s", bot_id, [tuple(r) for r in rows])
    except Exception as exc:
        log.warning("[bot-status] bot_id=%s checkpoint=failed err=%s", bot_id, exc)


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------

def clone_bot(source_bot_id: str, new_name: str | None = None, conn=None) -> dict:
    """
    Deep-copies a bot with a new bot_id, bumps version, sets status=paused.
    """
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        source = get_bot(source_bot_id, conn=conn)
        if source is None:
            raise ValueError(f"Bot {source_bot_id!r} not found")

        new_version = source["version"] + 1
        new_name    = new_name or f"{source['name']} v{new_version}"
        new_bot_id  = _make_slug(new_name)

        overrides = {
            "bot_id":       new_bot_id,
            "name":         new_name,
            "status":       "paused",
            "version":      new_version,
            "cloned_from":  source_bot_id,
        }
        new_bot = create_bot({**source, **overrides}, conn=conn)
        # Copy legs from source to new bot
        source_legs = get_legs(source_bot_id, conn=conn)
        if source_legs:
            save_legs(new_bot_id, source_legs, conn=conn)
        return new_bot
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_bot(bot_id: str, conn=None) -> None:
    """Permanently delete a bot and all associated data."""
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        with conn:
            conn.execute("DELETE FROM lab_bot_legs WHERE bot_id=?", (bot_id,))
            conn.execute("DELETE FROM trades WHERE bot_id=?", (bot_id,))
            conn.execute("DELETE FROM positions WHERE bot_id=?", (bot_id,))
            conn.execute("DELETE FROM signals WHERE bot_id=?", (bot_id,))
            conn.execute("DELETE FROM daily_summary WHERE bot_id=?", (bot_id,))
            conn.execute("DELETE FROM bot_params WHERE bot_id=?", (bot_id,))
            conn.execute("DELETE FROM bots WHERE bot_id=?", (bot_id,))
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Leg CRUD
# ---------------------------------------------------------------------------

LEG_CODES = ["C1", "C2", "P1", "P2"]


def save_legs(bot_id: str, legs_data: list[dict], conn=None) -> None:
    """
    Upsert all legs for a bot.
    legs_data: list of dicts with keys matching lab_bot_legs columns.
    Legs not present in legs_data are disabled (not deleted).
    """
    close = conn is None
    if conn is None:
        conn = get_conn()
    now = _now()
    try:
        with conn:
            # Disable all existing legs first, then re-enable those in legs_data
            conn.execute("UPDATE lab_bot_legs SET is_enabled=0 WHERE bot_id=?", (bot_id,))
            for leg in legs_data:
                leg_code = leg["leg_code"]
                conn.execute("""
                    INSERT INTO lab_bot_legs
                        (id, bot_id, leg_code, is_enabled, entry_logic,
                         entry_conditions_json, entry_gates_json,
                         exit_conditions_json, stoploss_conditions_json, created_at)
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bot_id, leg_code) DO UPDATE SET
                        is_enabled=1,
                        entry_logic=excluded.entry_logic,
                        entry_conditions_json=excluded.entry_conditions_json,
                        entry_gates_json=excluded.entry_gates_json,
                        exit_conditions_json=excluded.exit_conditions_json,
                        stoploss_conditions_json=excluded.stoploss_conditions_json
                """, (
                    str(uuid.uuid4()), bot_id, leg_code,
                    leg.get("entry_logic", "AND"),
                    _to_json(leg.get("entry_conditions_json", "[]")),
                    _to_json(leg.get("entry_gates_json", "[]")),
                    _to_json(leg.get("exit_conditions_json", "[]")),
                    _to_json(leg.get("stoploss_conditions_json", "[]")),
                    now,
                ))
    finally:
        if close:
            conn.close()


def get_legs(bot_id: str, conn=None) -> list[dict]:
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM lab_bot_legs WHERE bot_id=? ORDER BY leg_code", (bot_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_slug(name: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"{slug}_{uuid.uuid4().hex[:6]}"


def _to_json(value) -> str:
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except Exception:
            pass
    return json.dumps(value if value is not None else [])


def _float_or_none(v) -> float | None:
    if v is None or v == "" or v == "None":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
