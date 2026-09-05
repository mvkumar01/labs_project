"""Order routing and operator controls. No broker calls or plaintext secrets in views."""

import contextlib
import ipaddress
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

from storage.live_db import get_live_conn
from live import live_service as svc


class ControlError(ValueError):
    pass


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS proxy_routes (
            route_id TEXT PRIMARY KEY, label TEXT NOT NULL,
            endpoint_enc BLOB NOT NULL, expected_ips TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
            per_second INTEGER NOT NULL, per_minute INTEGER NOT NULL,
            daily_quota INTEGER NOT NULL, monthly_quota INTEGER NOT NULL,
            exit_reserve INTEGER NOT NULL, observed_ip TEXT, verified_at REAL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS live_proxy_assignments (
            conn_id TEXT PRIMARY KEY REFERENCES live_broker_connections(conn_id),
            user_id TEXT NOT NULL, route_id TEXT NOT NULL REFERENCES proxy_routes(route_id),
            confirmed_revision INTEGER NOT NULL, assigned_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS live_order_requests (
            request_id TEXT PRIMARY KEY, route_id TEXT NOT NULL, revision INTEGER NOT NULL,
            user_id TEXT NOT NULL, conn_id TEXT NOT NULL, operation TEXT NOT NULL,
            intent_key TEXT NOT NULL, requested_at REAL NOT NULL, outcome TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS live_request_route_time ON live_order_requests(route_id,requested_at);
        CREATE INDEX IF NOT EXISTS live_request_conn_time ON live_order_requests(conn_id,requested_at);
        CREATE TABLE IF NOT EXISTS live_admin_roles (user_id TEXT PRIMARY KEY REFERENCES live_users(user_id));
        CREATE TABLE IF NOT EXISTS live_admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
            action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL,
            created_at REAL NOT NULL
        );
    """)


@contextlib.contextmanager
def transaction():
    c = get_live_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def audit(c, actor, action, target, detail):
    c.execute(
        "INSERT INTO live_admin_audit(actor,action,target,detail,created_at) VALUES(?,?,?,?,?)",
        (actor, action, target, json.dumps(detail, sort_keys=True), time.time()),
    )


def is_admin(user_id, conn=None):
    # Bootstrap with immutable user IDs, never registration-controlled usernames.
    if not user_id:
        return False
    if user_id in os.environ.get("LIVE_ADMIN_USER_IDS", "").split(","):
        return True
    with (
        contextlib.closing(get_live_conn())
        if conn is None
        else contextlib.nullcontext(conn)
    ) as c:
        return (
            c.execute(
                "SELECT 1 FROM live_admin_roles WHERE user_id=?", (user_id,)
            ).fetchone()
            is not None
        )


def require_admin(actor, c):
    if not is_admin(actor, c):
        raise ControlError("Administrator access required")


def require_idle(c, user_id, conn_id):
    if not c.execute(
        "SELECT 1 FROM live_broker_connections WHERE user_id=? AND conn_id=?",
        (user_id, conn_id),
    ).fetchone():
        raise ControlError("Connection not found")
    mode = svc.get_mode(user_id, conn_id, c)
    if mode != "DISARMED" or svc.is_armed(user_id, conn_id, c):
        raise ControlError("Disarm this connection before changing its settings")
    row = c.execute(
        "SELECT position FROM live_trade_state WHERE user_id=? AND conn_id=?",
        (user_id, conn_id),
    ).fetchone()
    if row and row["position"] == "OPEN":
        raise ControlError("Resolve the open position before changing settings")
    if svc.get_config_int(user_id, conn_id, "reconcile_blocked", c):
        raise ControlError("Resolve the broker reconciliation mismatch first")
    if c.execute(
        "SELECT 1 FROM live_orders WHERE user_id=? AND conn_id=? AND dry_run=0 AND lower(status) NOT IN ('complete','completed','filled','cancelled','canceled','rejected','failed','no_long_position','exit_qty_exceeds_position','blocked','skipped','gate_blocked') LIMIT 1",
        (user_id, conn_id),
    ).fetchone():
        raise ControlError(
            "Resolve pending or uncertain orders before changing settings"
        )
    if c.execute(
        "SELECT 1 FROM live_order_requests WHERE conn_id=? AND outcome IN ('reserved','uncertain') LIMIT 1",
        (conn_id,),
    ).fetchone():
        raise ControlError(
            "An order request needs reconciliation before changing settings"
        )


def validate_endpoint(endpoint):
    try:
        p = urlsplit(endpoint)
        port = p.port
        allowed = {"quotaguard.com"} | {
            x.strip().lower()
            for x in os.environ.get("LIVE_PROXY_ALLOWED_HOSTS", "").split(",")
            if x.strip()
        }
        host = (p.hostname or "").lower()
        if (
            p.scheme not in ("http", "https")
            or not port
            or p.path not in ("", "/")
            or p.query
            or p.fragment
        ):
            raise ValueError()
        if not any(
            host == h or (h == "quotaguard.com" and host.endswith(".quotaguard.com"))
            for h in allowed
        ):
            raise ValueError()
        if not p.username or not p.password:
            raise ValueError()
    except ValueError:
        raise ControlError(
            "Use an authenticated HTTP(S) proxy URL on an approved proxy host"
        ) from None
    return endpoint


def save_route(actor, values):
    route_id = str(values.get("route_id") or uuid.uuid4().hex)
    label = str(values.get("label", "")).strip()
    if not label or len(label) > 80:
        raise ControlError("Route name must contain 1 to 80 characters")
    try:
        ips = sorted(
            {
                str(ipaddress.ip_address(x.strip()))
                for x in str(values.get("expected_ips", "")).split(",")
                if x.strip()
            }
        )
        if not 1 <= len(ips) <= 2 or any(
            not ipaddress.ip_address(ip).is_global for ip in ips
        ):
            raise ValueError()
        limits = {
            k: int(values.get(k, default))
            for k, default in [
                ("per_second", 5),
                ("per_minute", 100),
                ("daily_quota", 1000),
                ("monthly_quota", 10000),
                ("exit_reserve", 20),
            ]
        }
        if (
            any(v <= 0 for v in limits.values())
            or limits["per_second"] > 5
            or limits["per_minute"] > 100
        ):
            raise ValueError()
        if (
            limits["daily_quota"] > 1000
            or limits["monthly_quota"] < limits["daily_quota"]
            or limits["exit_reserve"] >= limits["daily_quota"]
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ControlError(
            "Use 1-2 public IPs and positive limits: at most 5/sec, 100/min, 1000/day; reserve below daily quota"
        ) from None
    with transaction() as c:
        require_admin(actor, c)
        old = c.execute(
            "SELECT * FROM proxy_routes WHERE route_id=?", (route_id,)
        ).fetchone()
        if old:
            if int(values.get("revision", 0)) != old["revision"]:
                raise ControlError(
                    "Route changed in another session; reload before saving"
                )
            for a in c.execute(
                "SELECT user_id,conn_id FROM live_proxy_assignments WHERE route_id=?",
                (route_id,),
            ).fetchall():
                require_idle(c, a["user_id"], a["conn_id"])
        endpoint = str(values.get("endpoint", "")).strip()
        if not old and not endpoint and values.get("import_existing") == "1":
            from live.proxy import order_proxy_url

            endpoint = order_proxy_url()
        enc = (
            svc._fernet().encrypt(validate_endpoint(endpoint).encode())
            if endpoint
            else (old["endpoint_enc"] if old else None)
        )
        if not enc:
            raise ControlError("Proxy URL required for a new route")
        revision = old["revision"] + 1 if old else 1
        c.execute(
            """INSERT INTO proxy_routes(route_id,label,endpoint_enc,expected_ips,revision,enabled,per_second,per_minute,daily_quota,monthly_quota,exit_reserve,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(route_id) DO UPDATE SET
            label=excluded.label,endpoint_enc=excluded.endpoint_enc,expected_ips=excluded.expected_ips,revision=excluded.revision,enabled=excluded.enabled,
            per_second=excluded.per_second,per_minute=excluded.per_minute,daily_quota=excluded.daily_quota,monthly_quota=excluded.monthly_quota,
            exit_reserve=excluded.exit_reserve,observed_ip=NULL,verified_at=NULL""",
            (
                route_id,
                label,
                enc,
                json.dumps(ips),
                revision,
                int(values.get("enabled", "1") == "1"),
                *limits.values(),
                time.time(),
            ),
        )
        audit(
            c,
            actor,
            "route_saved",
            route_id,
            {"revision": revision, "ips": ips, "limits": limits},
        )
    return route_id


def assign_route(actor, user_id, conn_id, route_id, revision, confirmed):
    with transaction() as c:
        require_admin(actor, c)
        require_idle(c, user_id, conn_id)
        route = c.execute(
            "SELECT * FROM proxy_routes WHERE route_id=?", (route_id,)
        ).fetchone()
        if (
            not route
            or not route["enabled"]
            or not confirmed
            or int(revision) != route["revision"]
        ):
            raise ControlError(
                "Choose an enabled route and confirm its current IPs are whitelisted at this broker"
            )
        c.execute(
            """INSERT INTO live_proxy_assignments VALUES(?,?,?,?,?) ON CONFLICT(conn_id) DO UPDATE SET
            route_id=excluded.route_id,confirmed_revision=excluded.confirmed_revision,assigned_at=excluded.assigned_at""",
            (conn_id, user_id, route_id, route["revision"], time.time()),
        )
        audit(
            c,
            actor,
            "route_assigned",
            conn_id,
            {
                "route_id": route_id,
                "revision": route["revision"],
                "broker_allowlist_confirmed": True,
            },
        )


def resolve_route(c, user_id, conn_id):
    r = c.execute(
        """SELECT r.*,a.confirmed_revision FROM live_proxy_assignments a JOIN proxy_routes r ON r.route_id=a.route_id
        JOIN live_broker_connections b ON b.conn_id=a.conn_id AND b.user_id=a.user_id WHERE a.user_id=? AND a.conn_id=?""",
        (user_id, conn_id),
    ).fetchone()
    if not r:
        raise ControlError("No order proxy assigned; ask an administrator")
    r = dict(r)
    if not r["enabled"] or r["confirmed_revision"] != r["revision"]:
        raise ControlError(
            "Route disabled or rotated; confirm broker allowlist and reassign"
        )
    if not r["verified_at"] or r["observed_ip"] not in json.loads(r["expected_ips"]):
        raise ControlError("Route egress has not been verified for this revision")
    return r


def route_status(user_id, conn_id, conn=None):
    with (
        contextlib.closing(get_live_conn())
        if conn is None
        else contextlib.nullcontext(conn)
    ) as c:
        try:
            r = resolve_route(c, user_id, conn_id)
            counts = usage(c, r["route_id"])
            ready = (
                counts["day"] < r["daily_quota"] - r["exit_reserve"]
                and counts["month"] < r["monthly_quota"] - r["exit_reserve"]
            )
            return {
                "ready": ready,
                "detail": (
                    "Order route ready"
                    if ready
                    else "Entry quota exhausted; exit reserve retained"
                ),
                "label": r["label"],
                "expected_ips": json.loads(r["expected_ips"]),
                "observed_ip": r["observed_ip"],
                "verified_at": r["verified_at"],
                "revision": r["revision"],
                "usage": counts,
                "daily_quota": r["daily_quota"],
                "monthly_quota": r["monthly_quota"],
                "exit_reserve": r["exit_reserve"],
            }
        except ControlError as exc:
            return {"ready": False, "detail": str(exc)}


def usage(c, route_id, now=None):
    now = time.time() if now is None else now
    ist = timezone(timedelta(hours=5, minutes=30))
    day = datetime.fromtimestamp(now, ist).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    starts = {
        "second": now - 1,
        "minute": now - 60,
        "day": day.timestamp(),
        "month": day.replace(day=1).timestamp(),
    }
    return {
        k: c.execute(
            "SELECT count(*) FROM live_order_requests WHERE route_id=? AND requested_at>=?",
            (route_id, start),
        ).fetchone()[0]
        for k, start in starts.items()
    }


def reserve(user_id, conn_id, operation, intent_key, *, broker=None, identity=None):
    if operation not in ("entry", "exit", "modify", "cancel") or not intent_key:
        raise ControlError("Valid order operation and intent key required")
    with transaction() as c:
        r = resolve_route(c, user_id, conn_id)
        if os.environ.get("LIVE_ORDERS_ENABLED", "0").lower() not in (
            "1",
            "true",
            "yes",
        ):
            raise ControlError("Live orders are disabled by the operator")
        if broker is not None:
            connection = svc.get_connection(user_id, conn_id, c)
            stored = svc.load_credentials(user_id, conn_id, c)
            if (
                connection.get("broker") != broker
                or not stored
                or any(
                    stored.get(k) != (identity or {}).get(k)
                    for k in ("api_key", "client_code", "user_id")
                )
            ):
                raise ControlError(
                    "Broker credentials changed; reconnect the runner session"
                )
        if (
            svc.get_mode(user_id, conn_id, c) != "LIVE_ARMED"
            or not svc.is_armed(user_id, conn_id, c)
            or svc.is_kill_switch_on(user_id, conn_id, c)
        ):
            raise ControlError("Connection is not armed for orders")
        if c.execute(
            "SELECT 1 FROM live_order_requests WHERE conn_id=? AND outcome IN ('reserved','uncertain')",
            (conn_id,),
        ).fetchone():
            raise ControlError(
                "Previous request is uncertain; reconcile before retrying"
            )
        if c.execute(
            "SELECT 1 FROM live_order_requests WHERE conn_id=? AND intent_key=? AND outcome='acknowledged'",
            (conn_id, intent_key),
        ).fetchone():
            raise ControlError(
                "This order intent was already acknowledged; reconcile before retrying"
            )
        if operation == "entry" and svc.get_config_int(
            user_id, conn_id, "reconcile_blocked", c
        ):
            raise ControlError("Resolve the broker reconciliation mismatch first")
        now = time.time()
        counts = usage(c, r["route_id"], now)
        reserve_amount = r["exit_reserve"] if operation in ("entry", "modify") else 0
        for window, limit in [
            ("second", r["per_second"]),
            ("minute", r["per_minute"]),
            ("day", r["daily_quota"] - reserve_amount),
            ("month", r["monthly_quota"] - reserve_amount),
        ]:
            if counts[window] >= limit:
                raise ControlError("Order route " + window + " budget exhausted")
        # Account budgets span route changes and all web/runner processes.
        for seconds, limit in [(1, 5), (60, 100), (86400, 1000)]:
            if (
                c.execute(
                    "SELECT count(*) FROM live_order_requests WHERE conn_id=? AND requested_at>=?",
                    (conn_id, now - seconds),
                ).fetchone()[0]
                >= limit
            ):
                raise ControlError("Broker account request budget exhausted")
        endpoint = validate_endpoint(svc._fernet().decrypt(r["endpoint_enc"]).decode())
        rid = uuid.uuid4().hex
        c.execute(
            "INSERT INTO live_order_requests VALUES(?,?,?,?,?,?,?,?,?)",
            (
                rid,
                r["route_id"],
                r["revision"],
                user_id,
                conn_id,
                operation,
                intent_key,
                now,
                "reserved",
            ),
        )
        return rid, endpoint


def finish_request(request_id, outcome):
    with transaction() as c:
        c.execute(
            "UPDATE live_order_requests SET outcome=? WHERE request_id=?",
            (outcome, request_id),
        )


def activate_broker(user_id, broker):
    target = svc.conn_id_for(user_id, broker)
    with transaction() as c:
        row = c.execute(
            "SELECT status FROM live_broker_connections WHERE user_id=? AND conn_id=?",
            (user_id, target),
        ).fetchone()
        if not row or row["status"] != "connected":
            raise ControlError("Complete broker authentication before selecting it")
        current = svc.get_selected_broker(user_id, c)
        if current == broker:
            return
        for other in c.execute(
            "SELECT conn_id FROM live_broker_connections WHERE user_id=?", (user_id,)
        ).fetchall():
            require_idle(c, user_id, other["conn_id"])
        c.execute(
            "INSERT INTO live_config(user_id,conn_id,key,value,updated_at) VALUES(?,'__user__','selected_broker',?,?) ON CONFLICT(user_id,conn_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (user_id, broker, svc._now_iso()),
        )
        audit(
            c,
            user_id,
            "broker_selected",
            target,
            {"previous": current, "broker": broker},
        )


def save_connection_credentials(user_id, conn_id, broker, creds, expected):
    """Persist identity and credentials together; recheck idle state under the write lock."""
    encrypted = svc._fernet().encrypt(json.dumps(creds).encode())
    identity_keys = (
        "api_key",
        "api_secret",
        "client_code",
        "pin",
        "totp_secret",
        "user_id",
    )
    with transaction() as c:
        current = svc.load_credentials(user_id, conn_id, c)
        if current != expected:
            raise ControlError(
                "Credentials changed in another session; reload and reconnect"
            )
        exists = svc.get_connection(user_id, conn_id, c)
        if conn_id != svc.conn_id_for(user_id, broker) or broker not in (
            "angel",
            "zerodha",
        ):
            raise ControlError("Invalid broker connection")
        identity_changed = any(current.get(k) != creds.get(k) for k in identity_keys)
        if exists and identity_changed:
            require_idle(c, user_id, conn_id)
        account = (
            creds.get("client_code") if broker == "angel" else creds.get("user_id")
        )
        ref = f"{broker}:{account}" if account else None
        now = svc._now_iso()
        c.execute(
            """INSERT INTO live_broker_connections(conn_id,user_id,broker,account_label,account_ref,status,created_at,updated_at)
            VALUES(?,?,?,?,?,'configured',?,?) ON CONFLICT(conn_id) DO UPDATE SET account_label=excluded.account_label,
            account_ref=excluded.account_ref,status='configured',updated_at=excluded.updated_at""",
            (conn_id, user_id, broker, account or broker, ref, now, now),
        )
        c.execute(
            """INSERT INTO live_credentials_enc VALUES(?,?,?,?,?,?) ON CONFLICT(conn_id) DO UPDATE SET
            ciphertext=excluded.ciphertext,updated_at=excluded.updated_at""",
            (conn_id, user_id, broker, encrypted, now, now),
        )
        if identity_changed:
            c.execute(
                "DELETE FROM live_proxy_assignments WHERE user_id=? AND conn_id=?",
                (user_id, conn_id),
            )
        audit(
            c,
            user_id,
            "credentials_saved",
            conn_id,
            {"identity_changed": identity_changed},
        )


def update_settings(actor, user_id, conn_id, lots, daily_loss_cap):
    try:
        lots = int(lots)
        cap = float(daily_loss_cap)
        if lots != 1 or not math.isfinite(cap) or cap <= 0 or cap > 100000:
            raise ValueError()
    except (TypeError, ValueError):
        raise ControlError(
            "Phase limit is 1 lot; daily loss cap must be positive and at most 100000"
        ) from None
    with transaction() as c:
        require_admin(actor, c)
        require_idle(c, user_id, conn_id)
        for key, value in [("lots", lots), ("daily_loss_cap", cap)]:
            c.execute(
                "INSERT INTO live_config VALUES(?,?,?,?,?) ON CONFLICT(user_id,conn_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (user_id, conn_id, key, str(value), svc._now_iso()),
            )
        audit(c, actor, "risk_settings", conn_id, {"lots": lots, "daily_loss_cap": cap})


def set_admin(actor, user_id, enabled):
    with transaction() as c:
        require_admin(actor, c)
        if actor == user_id and not enabled:
            raise ControlError("Cannot remove your own administrator role")
        if not svc.get_user(user_id, c):
            raise ControlError("User not found")
        if enabled:
            c.execute("INSERT OR IGNORE INTO live_admin_roles VALUES(?)", (user_id,))
        else:
            c.execute("DELETE FROM live_admin_roles WHERE user_id=?", (user_id,))
        audit(c, actor, "admin_role", user_id, {"enabled": enabled})
