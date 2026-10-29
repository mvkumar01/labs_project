"""Personal display preferences. No trading config, account selection or order changes."""
import json
from datetime import datetime, timezone

from flask import jsonify, request
from live.auth_gate import current_user_id, csrf_protect, issue_csrf
from labs.services.overview_navigation import validate_keys
from storage.live_db import get_live_conn


def register_routes(bp):
    @bp.route("/api/overview-preferences", methods=["GET", "POST"])
    @csrf_protect
    def overview_preferences():
        user = current_user_id()
        if not user:
            return jsonify(error="Sign in to save account preferences."), 401
        conn = get_live_conn()
        try:
            if not conn.execute("SELECT 1 FROM live_users WHERE user_id=?", (user,)).fetchone():
                return jsonify(error="Sign in again to save account preferences."), 401
            conn.execute("CREATE TABLE IF NOT EXISTS overview_preferences (user_id TEXT PRIMARY KEY "
                         "REFERENCES live_users(user_id), spotlight_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
            if request.method == "POST":
                payload = request.get_json(silent=True)
                try:
                    keys = validate_keys(payload.get("keys") if isinstance(payload, dict) else None)
                except ValueError as exc:
                    return jsonify(error=str(exc)), 400
                conn.execute("INSERT INTO overview_preferences VALUES(?,?,?) ON CONFLICT(user_id) "
                             "DO UPDATE SET spotlight_json=excluded.spotlight_json,updated_at=excluded.updated_at",
                             (user, json.dumps(keys), datetime.now(timezone.utc).isoformat()))
                conn.commit()
            row = conn.execute("SELECT spotlight_json FROM overview_preferences WHERE user_id=?", (user,)).fetchone()
            keys = json.loads(row[0]) if row else []
            response = jsonify(keys=keys, scope="account", csrf_token=issue_csrf())
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Cookie"
            return response
        finally:
            conn.close()
