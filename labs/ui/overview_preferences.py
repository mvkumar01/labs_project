"""Personal Watch Lists and the shared, admin-curated Spotlight."""
import json
import sqlite3
from datetime import datetime, timezone

from flask import current_app, jsonify, request
from live.control_plane import is_admin
from live.auth_gate import current_user_id, csrf_protect, issue_csrf
from labs.services.overview_navigation import DEFAULT_SPOTLIGHT, validate_keys
from storage.live_db import get_live_conn


def _shared_keys(conn):
    try:
        row = conn.execute("SELECT strategy_keys_json FROM overview_spotlight WHERE singleton=1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        row = None
    return json.loads(row[0]) if row else list(DEFAULT_SPOTLIGHT)


def _valid_user(conn, user):
    return bool(user and conn.execute("SELECT 1 FROM live_users WHERE user_id=?", (user,)).fetchone())


def spotlight_settings():
    """Read only the public picks and whether this visitor can edit them."""
    try:
        conn = get_live_conn()
        try:
            keys = _shared_keys(conn)
            user = current_user_id()
            return keys, _valid_user(conn, user) and is_admin(user, conn)
        finally:
            conn.close()
    except sqlite3.Error:
        current_app.logger.exception("Overview Spotlight settings unavailable")
        return list(DEFAULT_SPOTLIGHT), False


def _private_response(keys, scope):
    response = jsonify(keys=keys, scope=scope, csrf_token=issue_csrf())
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    return response


def _payload_keys():
    payload = request.get_json(silent=True)
    return validate_keys(payload.get("keys") if isinstance(payload, dict) else None)


def register_routes(bp):
    @bp.route("/api/overview-preferences", methods=["GET", "POST"])
    @csrf_protect
    def overview_preferences():
        user = current_user_id()
        if not user:
            return jsonify(error="Sign in to save account preferences."), 401
        conn = get_live_conn()
        try:
            if not _valid_user(conn, user):
                return jsonify(error="Sign in again to save account preferences."), 401
            conn.execute("CREATE TABLE IF NOT EXISTS overview_preferences (user_id TEXT PRIMARY KEY "
                         "REFERENCES live_users(user_id), spotlight_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
            if request.method == "POST":
                try:
                    keys = _payload_keys()
                except ValueError as exc:
                    return jsonify(error=str(exc)), 400
                conn.execute("INSERT INTO overview_preferences VALUES(?,?,?) ON CONFLICT(user_id) "
                             "DO UPDATE SET spotlight_json=excluded.spotlight_json,updated_at=excluded.updated_at",
                             (user, json.dumps(keys), datetime.now(timezone.utc).isoformat()))
                conn.commit()
            row = conn.execute("SELECT spotlight_json FROM overview_preferences WHERE user_id=?", (user,)).fetchone()
            keys = json.loads(row[0]) if row else []
            return _private_response(keys, "account")
        finally:
            conn.close()

    @bp.route("/api/overview-spotlight", methods=["GET", "POST"])
    @csrf_protect
    def overview_spotlight():
        user = current_user_id()
        conn = get_live_conn()
        try:
            if request.method == "POST":
                conn.execute("BEGIN IMMEDIATE")
            if not _valid_user(conn, user):
                return jsonify(error="Sign in to manage Spotlight."), 401
            if not is_admin(user, conn):
                return jsonify(error="Administrator access required."), 403
            if request.method == "POST":
                try:
                    keys = _payload_keys()
                except ValueError as exc:
                    return jsonify(error=str(exc)), 400
                conn.execute("CREATE TABLE IF NOT EXISTS overview_spotlight (singleton INTEGER PRIMARY KEY "
                             "CHECK (singleton=1), strategy_keys_json TEXT NOT NULL, updated_by TEXT NOT NULL, "
                             "updated_at TEXT NOT NULL)")
                conn.execute("INSERT INTO overview_spotlight VALUES(1,?,?,?) ON CONFLICT(singleton) "
                             "DO UPDATE SET strategy_keys_json=excluded.strategy_keys_json, "
                             "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                             (json.dumps(keys), user, datetime.now(timezone.utc).isoformat()))
                conn.commit()
                return _private_response(keys, "spotlight")
            return _private_response(_shared_keys(conn), "spotlight")
        finally:
            conn.close()
