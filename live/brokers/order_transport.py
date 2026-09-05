"""The sole HTTP transport for live order mutations. Never retries a submission."""

import contextlib
from urllib.parse import quote

import requests

from live import control_plane as cp
from storage.live_db import get_live_conn


class OrderTransportError(RuntimeError):
    pass


def send_order(adapter, operation, params, intent_key):
    broker = adapter.broker_name
    if broker not in ("angel", "zerodha"):
        raise cp.ControlError("Unsupported order broker")
    params = dict(params)
    if operation in ("modify", "cancel"):
        order_id = str(params.get("order_id") or params.get("orderid") or "")
        with contextlib.closing(get_live_conn()) as c:
            if not c.execute(
                "SELECT 1 FROM live_orders WHERE user_id=? AND conn_id=? AND broker_order_id=? AND dry_run=0",
                (adapter.user_id, adapter.conn_id, order_id),
            ).fetchone():
                raise cp.ControlError("Order does not belong to this connection")
    if broker == "zerodha":
        token = adapter._kite.access_token
        key = adapter._creds["api_key"]
        headers = {"X-Kite-Version": "3", "Authorization": f"token {key}:{token}"}
        variety = params.pop("variety", "regular")
        if variety != "regular":
            raise cp.ControlError("Unsupported order variety")
        url = "https://api.kite.trade/orders/regular"
        method = "POST"
        if operation in ("modify", "cancel"):
            url += "/" + quote(str(params.pop("order_id")), safe="")
            method = "PUT" if operation == "modify" else "DELETE"
        payload = {"data": params}
    else:
        headers = dict(adapter._smart.requestHeaders())
        headers["Authorization"] = "Bearer " + adapter._smart.access_token
        path = {
            "entry": "placeOrder",
            "exit": "placeOrder",
            "modify": "modifyOrder",
            "cancel": "cancelOrder",
        }.get(operation)
        if not path:
            raise cp.ControlError("Unsupported order operation")
        url = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/" + path
        method = "POST"
        payload = {"json": params}
    request_id, endpoint = cp.reserve(
        adapter.user_id,
        adapter.conn_id,
        operation,
        intent_key,
        broker=broker,
        identity=adapter._creds,
    )
    outcome = "uncertain"
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.proxies = {"http": endpoint, "https": endpoint}
            response = session.request(
                method,
                url,
                headers=headers,
                timeout=(5, 15),
                allow_redirects=False,
                **payload,
            )
            if response.status_code == 429:
                outcome = "rejected"
                raise OrderTransportError(
                    "Broker throttled the order; no automatic retry"
                )
            data = response.json()
            if response.status_code >= 500 or 300 <= response.status_code < 400:
                raise OrderTransportError(
                    "Order outcome uncertain; reconcile broker order book"
                )
            accepted = (
                (data.get("status") == "success")
                if broker == "zerodha"
                else (data.get("status") is True)
            )
            if not accepted or response.status_code >= 400:
                outcome = "rejected"
                raise OrderTransportError(
                    "Broker rejected the order; inspect broker order book"
                )
            result = data.get("data") or {}
            order_id = (
                result.get("order_id") if broker == "zerodha" else result.get("orderid")
            )
            if not order_id:
                raise OrderTransportError(
                    "Order acknowledgement missing; reconcile broker order book"
                )
            outcome = "acknowledged"
            return str(order_id) if broker == "zerodha" else data
    except OrderTransportError:
        raise
    except Exception:
        # Never expose requests exceptions: they can embed authenticated proxy URLs.
        raise OrderTransportError(
            "Order outcome uncertain; reconcile broker order book before retrying"
        ) from None
    finally:
        cp.finish_request(request_id, outcome)


def verify_route(actor, route_id, revision):
    """One explicit admin probe; consumes route quota and sends no broker credentials."""
    import json
    import time
    import uuid
    from live import live_service as svc

    with cp.transaction() as c:
        cp.require_admin(actor, c)
        row = c.execute(
            "SELECT * FROM proxy_routes WHERE route_id=?", (route_id,)
        ).fetchone()
        if not row or row["revision"] != int(revision) or not row["enabled"]:
            raise cp.ControlError("Route changed or disabled; reload")
        counts = cp.usage(c, route_id)
        if (
            counts["second"] >= row["per_second"]
            or counts["minute"] >= row["per_minute"]
            or counts["day"] >= row["daily_quota"] - row["exit_reserve"]
            or counts["month"] >= row["monthly_quota"] - row["exit_reserve"]
        ):
            raise cp.ControlError("Route probe budget exhausted")
        endpoint = cp.validate_endpoint(
            svc._fernet().decrypt(row["endpoint_enc"]).decode()
        )
        rid = uuid.uuid4().hex
        c.execute(
            "INSERT INTO live_order_requests VALUES(?,?,?,?,?,?,?,?,?)",
            (
                rid,
                route_id,
                row["revision"],
                actor,
                "",
                "probe",
                rid,
                time.time(),
                "reserved",
            ),
        )
    observed = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.proxies = {"http": endpoint, "https": endpoint}
            response = session.get(
                "https://api.ipify.org?format=json",
                timeout=(5, 10),
                allow_redirects=False,
            )
            response.raise_for_status()
            observed = response.json()["ip"]
        if observed not in json.loads(row["expected_ips"]):
            raise cp.ControlError("Observed IP does not match the route expected IPs")
    except Exception:
        with cp.transaction() as c:
            c.execute(
                "UPDATE proxy_routes SET verified_at=NULL,observed_ip=NULL WHERE route_id=? AND revision=?",
                (route_id, revision),
            )
            cp.audit(c, actor, "egress_failed", route_id, {"revision": revision})
        raise cp.ControlError(
            "Egress verification failed; check proxy credentials and expected IPs"
        ) from None
    finally:
        cp.finish_request(rid, "probe")
    with cp.transaction() as c:
        c.execute(
            "UPDATE proxy_routes SET verified_at=?,observed_ip=? WHERE route_id=? AND revision=?",
            (time.time(), observed, route_id, revision),
        )
        cp.audit(
            c,
            actor,
            "egress_verified",
            route_id,
            {"revision": revision, "observed_ip": observed},
        )
