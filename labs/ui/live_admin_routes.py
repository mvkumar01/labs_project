"""Authenticated operator views; trading credentials are never rendered."""

import contextlib
import json
import logging
from functools import wraps

from flask import abort, jsonify, redirect, render_template, request, url_for

from live import control_plane as cp
from live import live_service as svc
from live import live_executor as ex
from live.auth_gate import current_user_id, csrf_protect, issue_csrf
from storage.live_db import get_live_conn


def readiness(user_id, conn_id):
    row = svc.get_connection(user_id, conn_id) or {}

    class Snapshot:
        def account_ref(self):
            return row.get("account_ref", "")

        def is_connected(self):
            return row.get("status") == "connected"

    checks = [
        {"name": g.name, "passed": g.passed, "detail": g.detail}
        for g in ex.evaluate_all(Snapshot(), user_id, conn_id)
        if g.name != "mode_armed"
    ]
    checks.append(
        {
            "name": "reconciliation",
            "passed": not bool(
                svc.get_config_int(user_id, conn_id, "reconcile_blocked")
            ),
            "detail": svc.get_config(user_id, conn_id, "reconcile_message")
            or "No recorded mismatch",
        }
    )
    import os

    checks.append(
        {
            "name": "live_orders_enabled",
            "passed": os.environ.get("LIVE_ORDERS_ENABLED", "0").lower()
            in ("1", "true", "yes"),
            "detail": "Operator live-order enablement",
        }
    )
    checks.append(
        {
            "name": "dry_run_prepared",
            "passed": svc.get_mode(user_id, conn_id) in ("DRY_RUN", "LIVE_ARMED"),
            "detail": "Arm DRY-RUN before arming LIVE",
        }
    )
    return {
        "ready": all(c["passed"] for c in checks),
        "checks": checks,
        "egress": cp.route_status(user_id, conn_id),
    }


def register_routes(bp):
    def admin_only(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            svc.ensure_schema()
            if not cp.is_admin(current_user_id()):
                abort(403)
            return fn(*args, **kwargs)

        return wrapped

    @bp.route("/admin", methods=["GET", "POST"])
    @admin_only
    @csrf_protect
    def admin():
        error = None
        if request.method == "POST":
            actor = current_user_id()
            f = request.form
            try:
                action = f.get("action")
                if action == "route":
                    cp.save_route(actor, f)
                elif action == "assign":
                    cp.assign_route(
                        actor,
                        f["user_id"],
                        f["conn_id"],
                        f["route_id"],
                        int(f["revision"]),
                        f.get("confirmed") == "1",
                    )
                elif action == "verify":
                    from live.brokers.order_transport import verify_route

                    verify_route(actor, f["route_id"], int(f["revision"]))
                elif action == "settings":
                    cp.update_settings(
                        actor,
                        f["user_id"],
                        f["conn_id"],
                        f["lots"],
                        f["daily_loss_cap"],
                    )
                elif action == "role":
                    cp.set_admin(actor, f["user_id"], f.get("enabled") == "1")
                else:
                    raise cp.ControlError("Unknown action")
                return redirect(url_for("live.admin", saved="1"))
            except Exception as exc:
                # Only domain errors are safe to render. Never reflect submitted secrets.
                logging.getLogger("live.admin").warning(
                    "Admin action failed: %s", type(exc).__name__
                )
                error = (
                    str(exc)
                    if isinstance(exc, cp.ControlError)
                    else "Invalid settings; reload and check the form"
                )
        with contextlib.closing(get_live_conn()) as c:
            routes = [
                dict(r)
                for r in c.execute(
                    "SELECT route_id,label,expected_ips,revision,enabled,per_second,per_minute,daily_quota,monthly_quota,exit_reserve,observed_ip,verified_at FROM proxy_routes ORDER BY label"
                )
            ]
            for r in routes:
                r["expected_ips"] = ", ".join(json.loads(r["expected_ips"]))
                r["usage"] = cp.usage(c, r["route_id"])
            users = [
                dict(r)
                for r in c.execute(
                    "SELECT user_id,username FROM live_users ORDER BY username"
                )
            ]
            connections = [
                dict(r)
                for r in c.execute(
                    """SELECT b.user_id,b.conn_id,b.broker,b.status,b.account_ref,u.username,a.route_id,a.confirmed_revision
                FROM live_broker_connections b JOIN live_users u ON u.user_id=b.user_id
                LEFT JOIN live_proxy_assignments a ON a.user_id=b.user_id AND a.conn_id=b.conn_id ORDER BY u.username,b.broker"""
                )
            ]
            for row in connections:
                row["mode"] = svc.get_mode(row["user_id"], row["conn_id"], c)
                row["lots"] = svc.get_lots(row["user_id"], row["conn_id"], c)
                row["daily_loss_cap"] = svc.get_daily_loss_cap(
                    row["user_id"], row["conn_id"], c
                )
            audits = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM live_admin_audit ORDER BY id DESC LIMIT 100"
                )
            ]
            requests = [
                dict(r)
                for r in c.execute(
                    "SELECT request_id,route_id,conn_id,operation,requested_at,outcome FROM live_order_requests ORDER BY requested_at DESC LIMIT 100"
                )
            ]
        return render_template(
            "live_admin.html",
            csrf_token=issue_csrf(),
            routes=routes,
            users=users,
            connections=connections,
            audits=audits,
            order_requests=requests,
            error=error,
        ), (400 if error else 200)

    @bp.route("/readiness")
    def readiness_api():
        svc.ensure_schema()
        user_id = current_user_id()
        broker = svc.get_selected_broker(user_id)
        if not broker:
            return jsonify(
                {
                    "ready": False,
                    "checks": [],
                    "egress": {"ready": False, "detail": "Choose and connect a broker"},
                }
            )
        return jsonify(readiness(user_id, svc.conn_id_for(user_id, broker)))

    @bp.app_template_filter("operator_ist")
    def operator_ist(timestamp):
        from datetime import datetime, timezone, timedelta

        return (
            datetime.fromtimestamp(
                timestamp, timezone(timedelta(hours=5, minutes=30))
            ).strftime("%Y-%m-%d %H:%M:%S IST")
            if timestamp
            else "Not verified"
        )
