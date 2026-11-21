"""Flask integration for the Labs site (labs_project on PythonAnywhere).

Usage (from the WSGI file, after the Labs app is imported):

    from strategy_tester.flask_app import install
    install(application)

install() registers the Strategy Tester API under /labs/strategy-tester/ and makes
/labs/backtest render the Strategy Tester page. It does not modify any Labs files.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from pathlib import Path

from flask import Blueprint, Response, json, jsonify, render_template, request

from . import service

log = logging.getLogger("strategy-tester")
BASE = "/labs/strategy-tester"
PKG = Path(__file__).resolve().parent

bp = Blueprint("strategy_tester", __name__, url_prefix=BASE,
               template_folder=str(PKG / "templates"),
               static_folder=str(PKG / "static"), static_url_path="/static")


def _clean(o):
    """JSON-safe: NaN/inf -> null (browsers reject NaN in JSON)."""
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _json(payload, status: int = 200) -> Response:
    return Response(json.dumps(_clean(payload), default=str), status=status, mimetype="application/json")


# Per-visitor limit on the endpoints that download data and run backtests (the page is public, PA web workers are
# few and shared with the labs site, and a Claude key would make every query cost money).
RATE_LIMIT = int(os.environ.get("ST_RATE_LIMIT", "20"))          # requests ...
RATE_WINDOW_S = float(os.environ.get("ST_RATE_WINDOW_S", "300"))  # ... per this many seconds, per client
_hits: dict[str, deque] = {}
_hits_lock = threading.Lock()


def _client_id() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else "") or request.remote_addr or "?"


def _rate_limited() -> bool:
    now, key = time.time(), _client_id()
    with _hits_lock:
        q = _hits.setdefault(key, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        if len(_hits) > 5000:                      # forget idle clients
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > RATE_WINDOW_S]:
                _hits.pop(k, None)
    return False


def _guard(fn, *args):
    try:
        return _json(fn(*args))
    except service.ServiceError as e:
        return _json({"detail": e.message}, e.status)
    except Exception as e:  # never surface a raw 500 page to the UI
        log.exception("strategy tester request failed")
        return _json({"detail": f"Something went wrong: {e}"}, 500)


@bp.get("/api/status")
def api_status():
    return _guard(service.status)


@bp.get("/api/strategies")
def api_strategies():
    return _guard(service.strategies)


@bp.post("/api/query")
def api_query():
    if _rate_limited():
        return _json({"detail": "Too many requests - wait a few minutes and try again."}, 429)
    body = request.get_json(silent=True) or {}
    return _guard(service.query, body.get("prompt", ""))


@bp.post("/api/backtest")
def api_backtest():
    if _rate_limited():
        return _json({"detail": "Too many requests - wait a few minutes and try again."}, 429)
    return _guard(service.detail, request.get_json(silent=True) or {})


def _asset_version() -> int:
    """Cache-buster: changes whenever the frontend files change."""
    return int(max((PKG / "static" / f).stat().st_mtime for f in ("st.js", "st.css")))


def page():
    return render_template("strategy_tester/page.html", base=BASE, version=_asset_version())


@bp.get("/")
def page_direct():
    return page()


def install(app, replace_endpoint: str = "labs.backtest_page") -> None:
    """Register the blueprint and serve the Strategy Tester at the Labs backtest URL."""
    app.register_blueprint(bp)
    if replace_endpoint in app.view_functions:
        app.view_functions[replace_endpoint] = page
    log.info("Strategy Tester installed at %s and %s", BASE, replace_endpoint)
