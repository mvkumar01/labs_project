"""Flask page and JSON API for historical Live Simulation."""
from __future__ import annotations

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from labs.simulation.market_data import exchange_request_token, kite_login_url
from labs.simulation.service import SimulationService


simulation_bp = Blueprint("simulation", __name__, url_prefix="/labs/simulation")


def _service() -> SimulationService:
    return SimulationService()


def _json_body() -> dict:
    return request.get_json(silent=True) or {}


def _ok(call):
    try:
        return jsonify({"ok": True, **call()})
    except KeyError as exc:
        return jsonify({"ok": False, "error": str(exc).strip("'")}), 404
    except (ValueError, RuntimeError, FileNotFoundError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@simulation_bp.route("/")
def page():
    return render_template("simulation.html")


@simulation_bp.route("/api/bootstrap")
def bootstrap():
    return _ok(lambda: {"bootstrap": _service().bootstrap()})


@simulation_bp.route("/api/dates")
def dates():
    instrument = (request.args.get("instrument") or "NIFTY").upper()
    return _ok(lambda: {"dates": _service().dates(instrument)})


@simulation_bp.route("/api/data/fetch", methods=["POST"])
def fetch_data():
    body = _json_body()
    return _ok(lambda: {"result": _service().fetch_day(body["instrument"], body["trade_date"])})


@simulation_bp.route("/api/sessions", methods=["POST"])
def create_session():
    body = _json_body()
    return _ok(lambda: _service().create_session(body.get("starting_capital", 1_000_000)))


@simulation_bp.route("/api/sessions/<session_id>")
def get_session(session_id):
    return _ok(lambda: _service().get(session_id))


@simulation_bp.route("/api/sessions/<session_id>/configure", methods=["POST"])
def configure_session(session_id):
    return _ok(lambda: _service().configure(session_id, _json_body()))


@simulation_bp.route("/api/sessions/<session_id>/start", methods=["POST"])
def start_session(session_id):
    return _ok(lambda: _service().start(session_id))


@simulation_bp.route("/api/sessions/<session_id>/status", methods=["POST"])
def session_status(session_id):
    return _ok(lambda: _service().set_status(session_id, _json_body().get("status")))


@simulation_bp.route("/api/sessions/<session_id>/step", methods=["POST"])
def step_session(session_id):
    return _ok(lambda: _service().step(session_id, _json_body().get("count", 1)))


@simulation_bp.route("/api/sessions/<session_id>/reset", methods=["POST"])
def reset_session(session_id):
    return _ok(lambda: _service().reset(session_id, _json_body()))


@simulation_bp.route("/api/sessions/<session_id>/orders", methods=["POST"])
def submit_simulated_order(session_id):
    return _ok(lambda: _service().submit_order(session_id, _json_body()))


@simulation_bp.route("/api/sessions/<session_id>/orders/<order_id>", methods=["PATCH", "DELETE"])
def pending_order(session_id, order_id):
    if request.method == "DELETE":
        return _ok(lambda: _service().cancel_order(session_id, order_id))
    return _ok(lambda: _service().modify_order(session_id, order_id, _json_body()))


@simulation_bp.route("/api/sessions/<session_id>/positions/<symbol>/exit", methods=["POST"])
def exit_position(session_id, symbol):
    return _ok(lambda: _service().exit_position(session_id, symbol, _json_body().get("qty")))


@simulation_bp.route("/api/sessions/<session_id>/positions/<symbol>", methods=["PATCH"])
def modify_position(session_id, symbol):
    return _ok(lambda: _service().modify_position(session_id, symbol, _json_body()))


@simulation_bp.route("/kite/login")
def kite_login():
    try:
        return redirect(kite_login_url())
    except RuntimeError as exc:
        return redirect(url_for("simulation.page", kite_error=str(exc)))


@simulation_bp.route("/kite/callback")
def kite_callback():
    request_token = request.args.get("request_token")
    if not request_token:
        return redirect(url_for("simulation.page", kite_error="Kite request token missing"))
    try:
        exchange_request_token(request_token)
        return redirect(url_for("simulation.page", kite_connected="1"))
    except Exception as exc:
        return redirect(url_for("simulation.page", kite_error=type(exc).__name__))
