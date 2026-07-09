"""
live_routes.py — Flask blueprint `live_bp` mounted at /live (spec §11).

MULTI-USER (spec §2, §11): a generic N-user system. Users register and log in;
the session carries `user_id`. EVERY authenticated route derives
`user_id = current_user_id()` and `conn_id = svc.conn_id_for(user_id, broker)`
from the session — NEVER from a client-supplied value — and shows/mutates ONLY
that user's own rows. A user can never read or control another user's state.

HARD CONSTRAINT: routes never place or exit orders; broker reads are limited to
auth/funds refresh; no route imports a broker SDK. Going live is a config flag the always-on
`live_runner` observes — never the web layer.

Isolation: imports ONLY live.live_service, live.live_executor (pure 3-mode
machine + DB-only gate evaluation), live.auth_gate. NEVER imports
labs.services.* / labs.engine.* / a broker SDK.

Security: all POST routes are CSRF-protected and (except register/login) require
a session user_id (enforced by live.auth_gate.register_auth_gate in app.py).
TOTP secret / PIN / API secret / passcode are WRITE-ONLY — never echoed in any
GET or JSON response. GET cred routes show only `set` / `unset` status.
"""
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, jsonify,
)

from live import live_service as svc
from live import live_executor as ex
from live.auth_gate import (
    current_user_id, login_throttled, record_attempt, issue_csrf, csrf_protect,
    registration_open, verify_invite_code,
)
from live.brokers.zerodha import build_login_url, exchange_request_token

log = logging.getLogger("live.routes")

live_bp = Blueprint("live", __name__, url_prefix="/live")

SUPPORTED_BROKERS = {
    "zerodha": {
        "label": "Zerodha Kite Connect",
        "fields": [
            {"name": "api_key", "label": "API Key", "secret": False},
            {"name": "api_secret", "label": "API Secret", "secret": True},
        ],
    },
    "angel": {
        "label": "Angel One SmartAPI",
        "fields": [
            {"name": "api_key", "label": "API Key", "secret": False},
            {"name": "client_code", "label": "Client Code", "secret": False},
            {"name": "pin", "label": "PIN", "secret": True},
            {"name": "totp_secret", "label": "TOTP Secret", "secret": True},
        ],
    },
}

LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 15, "SENSEX": 20}

# Phase cap (spec §13): Phase 0/1 = 1 lot, hard ceiling 2.
LOTS_PHASE_CAP = 1
LOTS_HARD_CAP = ex.LOTS_HARD_CAP  # == 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_date(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def _status_trade_date() -> str:
    return _valid_date(request.args.get("trade_date")) or svc.today_ist_iso()


def _status_date_range() -> tuple[str, str]:
    """Inclusive IST date range for trade history: ?date_from=&date_to=
    (or legacy single ?trade_date=). Defaults to today/today."""
    single = _valid_date(request.args.get("trade_date"))
    date_from = _valid_date(request.args.get("date_from")) or single or svc.today_ist_iso()
    date_to = _valid_date(request.args.get("date_to")) or single or svc.today_ist_iso()
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def _is_stale_iso(value: str | None, *, max_age_s: int) -> bool:
    if not value:
        return True
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() > max_age_s
    except Exception:
        return True


def _current_conn_id():
    """Derive this user's active conn_id from session user_id + selected broker.
    Returns (user_id, broker, conn_id) or (user_id, None, None) if no broker
    selected yet. NEVER trusts a client-supplied id."""
    user_id = current_user_id()
    broker = (session.get("live_broker") or "").lower()
    if broker not in SUPPORTED_BROKERS:
        broker = None
    if user_id and not broker:
        broker = svc.get_selected_broker(user_id)
        if broker:
            session["live_broker"] = broker
    if user_id and not broker:
        connections = svc.list_user_connections(user_id)
        if connections:
            connections.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
            broker = (connections[0].get("broker") or "").lower()
            if broker in SUPPORTED_BROKERS:
                session["live_broker"] = broker
                svc.set_selected_broker(user_id, broker)
    conn_id = svc.conn_id_for(user_id, broker) if (user_id and broker) else None
    return user_id, broker, conn_id


def _normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def _account_ref_from_creds(broker: str, creds: dict) -> str | None:
    if broker == "angel":
        client_code = str(creds.get("client_code") or "").strip()
        return f"angel:{client_code}" if client_code else None
    if broker == "zerodha":
        user_id = str(creds.get("user_id") or "").strip()
        return f"zerodha:{user_id}" if user_id else None
    return None


def _adapter_for(broker: str, *, user_id: str, conn_id: str, creds: dict):
    """Build a broker adapter without importing SDKs outside live/brokers."""
    if broker == "angel":
        from live.brokers.angel import AngelAdapter
        return AngelAdapter(user_id=user_id, conn_id=conn_id, creds=creds)
    if broker == "zerodha":
        from live.brokers.zerodha import ZerodhaAdapter
        return ZerodhaAdapter(user_id=user_id, conn_id=conn_id, creds=creds)
    raise ValueError(f"unsupported broker: {broker}")


def _connection_status_for_refresh(broker: str, creds: dict, connected: bool) -> str:
    """Persist an honest broker status after an auth/funds refresh attempt."""
    if connected:
        return "connected"
    if broker == "zerodha":
        return "disconnected" if creds.get("access_token") else "configured"
    if broker == "angel":
        required = ("api_key", "client_code", "pin", "totp_secret")
        return "disconnected" if all(creds.get(k) for k in required) else "configured"
    return "disconnected"


def _refresh_broker_funds(user_id: str, broker: str, conn_id: str,
                          creds: dict | None = None) -> bool:
    """Connect once and persist a non-secret funds snapshot for /live/."""
    creds = creds if creds is not None else None
    try:
        creds = creds if creds is not None else svc.load_credentials(user_id, conn_id)
        adapter = _adapter_for(broker, user_id=user_id, conn_id=conn_id, creds=creds)
        adapter.connect()
        connected = bool(adapter.is_connected())
        funds = None
        funds_error = ""
        if connected:
            try:
                funds = adapter.available_funds()
                if funds is None:
                    funds_error = "funds_unavailable"
            except Exception as exc:
                funds_error = type(exc).__name__
        row = svc.get_connection(user_id, conn_id)
        svc.upsert_connection(
            user_id,
            conn_id,
            broker=broker,
            account_label=(row.get("account_label")
                           or creds.get("client_code")
                           or creds.get("user_id")
                           or creds.get("api_key")
                           or broker),
            account_ref=adapter.account_ref(),
            status=_connection_status_for_refresh(broker, creds, connected),
        )
        svc.update_connection_funds(user_id, conn_id, funds, funds_error)
        return connected
    except Exception as exc:
        try:
            if creds is None:
                creds = svc.load_credentials(user_id, conn_id)
        except Exception:
            creds = {}
        row = svc.get_connection(user_id, conn_id)
        if row:
            svc.upsert_connection(
                user_id,
                conn_id,
                broker=broker,
                account_label=row.get("account_label"),
                account_ref=row.get("account_ref"),
                status=_connection_status_for_refresh(broker, creds or {}, False),
            )
        svc.update_connection_funds(user_id, conn_id, None, type(exc).__name__)
        log.warning("funds refresh failed user=%s broker=%s err=%s",
                    user_id, broker, type(exc).__name__)
        return False


# ══════════════════════════════════════════════════════════════════════════
# REGISTER / LOGIN / LOGOUT (multi-user identity, spec §10)
# ══════════════════════════════════════════════════════════════════════════
@live_bp.route("/register", methods=["GET", "POST"])
def register():
    svc.ensure_schema()
    # Self-service registration is gated by an invite code (env LIVE_INVITE_CODE).
    # Fails closed: with no code configured, registration is disabled entirely.
    open_reg = registration_open()
    if request.method == "GET":
        return render_template("live_register.html", csrf_token=issue_csrf(),
                               registration_open=open_reg,
                               error=request.args.get("error"))
    if not _csrf_ok():
        return render_template("live_register.html", csrf_token=issue_csrf(),
                               registration_open=open_reg,
                               error="Session expired, try again."), 400
    if not open_reg:
        return render_template("live_register.html", csrf_token=issue_csrf(),
                               registration_open=False,
                               error="Registration is closed. Ask the operator for access."), 403
    username = _normalize_username(request.form.get("username", ""))
    passcode = request.form.get("passcode", "")
    confirm_passcode = request.form.get("confirm_passcode", "")
    invite = request.form.get("invite_code", "")
    if login_throttled(username):
        return render_template("live_register.html", csrf_token=issue_csrf(),
                               registration_open=True,
                               error="Too many attempts. Wait a minute."), 429
    record_attempt(username)
    if not verify_invite_code(invite):  # never logs the code
        return render_template("live_register.html", csrf_token=issue_csrf(),
                               registration_open=True,
                               error="Invalid invite code."), 403
    if not username or not passcode:
        return render_template("live_register.html", csrf_token=issue_csrf(),
                               registration_open=True,
                               error="Username and passcode are required."), 400
    if passcode != confirm_passcode:
        return render_template("live_register.html", csrf_token=issue_csrf(),
                               registration_open=True,
                               error="Passcodes do not match."), 400
    try:
        user_id = svc.create_user(username, passcode)
    except ValueError:
        return render_template("live_register.html", csrf_token=issue_csrf(),
                               error="That username is taken."), 409
    session.clear()
    session["user_id"] = user_id
    session.permanent = True
    log.info("registered new user (username len=%d)", len(username))
    return redirect(url_for("live.connect"))


@live_bp.route("/login", methods=["GET", "POST"])
def login():
    svc.ensure_schema()
    if request.method == "GET":
        return render_template("live_login.html", csrf_token=issue_csrf(),
                               error=request.args.get("error"))
    if not _csrf_ok():
        return render_template("live_login.html", csrf_token=issue_csrf(),
                               error="Session expired, try again."), 400
    username = _normalize_username(request.form.get("username", ""))
    passcode = request.form.get("passcode", "")
    if login_throttled(username):
        return render_template("live_login.html", csrf_token=issue_csrf(),
                               error="Too many attempts. Wait a minute."), 429
    record_attempt(username)
    user_id = svc.verify_user(username, passcode)  # never logs the passcode
    if user_id:
        session.clear()
        session["user_id"] = user_id
        selected_broker = svc.get_selected_broker(user_id)
        if selected_broker:
            session["live_broker"] = selected_broker
        session.permanent = True
        return redirect(url_for("live.dashboard"))
    log.warning("failed /live/login attempt")
    return render_template("live_login.html", csrf_token=issue_csrf(),
                           error="Incorrect username or passcode."), 401


@live_bp.route("/logout", methods=["POST"])
@csrf_protect
def logout():
    session.clear()
    return redirect(url_for("live.login"))


def _csrf_ok() -> bool:
    """CSRF check usable inside the auth routes (which can't use the decorator
    because they run before a user_id exists)."""
    from live.auth_gate import check_csrf
    return check_csrf()


# ══════════════════════════════════════════════════════════════════════════
# DASHBOARD / STATUS PAGE (scoped to the session user)
# ══════════════════════════════════════════════════════════════════════════
@live_bp.route("/")
def dashboard():
    svc.ensure_schema()
    user_id, broker, conn_id = _current_conn_id()
    connection = svc.get_connection(user_id, conn_id) if conn_id else {}
    if not connection:
        return redirect(url_for("live.connect"))
    return render_template(
        "live.html",
        csrf_token=issue_csrf(),
        username=(svc.get_user(user_id) or {}).get("username", ""),
        mode=svc.get_mode(user_id, conn_id),
        kill_switch=svc.is_kill_switch_on(user_id, conn_id),
        connection=connection,
        configured=bool(connection),
        lots=svc.get_lots(user_id, conn_id),
        daily_loss_cap=svc.get_daily_loss_cap(user_id, conn_id),
        strategy_label=STRATEGY_LABELS[_current_strategy_preset(user_id, conn_id)],
        trade_date=svc.today_ist_iso(),
    )


# ══════════════════════════════════════════════════════════════════════════
# CONNECT — broker selector (this user)
# ══════════════════════════════════════════════════════════════════════════
@live_bp.route("/connect", methods=["GET", "POST"])
@csrf_protect
def connect():
    svc.ensure_schema()
    user_id = current_user_id()
    if request.method == "GET":
        selected = (
            session.get("live_broker")
            or svc.get_selected_broker(user_id)
            or "angel"
        )
        return render_template(
            "live_connect.html",
            csrf_token=issue_csrf(),
            brokers=SUPPORTED_BROKERS,
            selected=selected if selected in SUPPORTED_BROKERS else "angel",
        )
    broker = request.form.get("broker", "")
    if broker not in SUPPORTED_BROKERS:
        return redirect(url_for("live.connect"))
    session["live_broker"] = broker
    svc.set_selected_broker(user_id, broker)
    return redirect(url_for("live.credentials", broker=broker))


# ══════════════════════════════════════════════════════════════════════════
# CREDENTIALS — per-broker write-only form (this user's conn)
# ══════════════════════════════════════════════════════════════════════════
@live_bp.route("/credentials/<broker>", methods=["GET", "POST"])
@csrf_protect
def credentials(broker):
    svc.ensure_schema()
    if broker not in SUPPORTED_BROKERS:
        return redirect(url_for("live.connect"))
    user_id = current_user_id()
    spec = SUPPORTED_BROKERS[broker]
    conn_id = svc.conn_id_for(user_id, broker)

    if request.method == "GET":
        # SAFE status only — never echo secret values.
        return render_template(
            "live_credentials.html",
            csrf_token=issue_csrf(),
            broker=broker,
            broker_label=spec["label"],
            fields=spec["fields"],
            cred_status=svc.credentials_status(user_id, conn_id),
        )

    # POST — collect, encrypt, persist. NEVER log/echo the form dict.
    try:
        existing = svc.load_credentials(user_id, conn_id)
    except Exception:
        existing = {}
    creds = dict(existing)
    for f in spec["fields"]:
        val = request.form.get(f["name"], "").strip()
        if val:
            creds[f["name"]] = val
    if not creds:
        return redirect(url_for("live.credentials", broker=broker))

    account_label = creds.get("client_code") or creds.get("api_key") or broker
    svc.store_credentials(user_id, conn_id, broker, creds)
    svc.upsert_connection(user_id, conn_id, broker=broker,
                          account_label=account_label,
                          account_ref=_account_ref_from_creds(broker, creds),
                          status="configured")
    session["live_broker"] = broker
    svc.set_selected_broker(user_id, broker)
    log.info("stored credentials user=%s broker=%s (fields=%s)",
             user_id, broker, sorted(creds.keys()))  # field NAMES only
    if broker == "zerodha" and creds.get("api_key") and creds.get("api_secret"):
        return redirect(url_for("live.zerodha_login"))
    if broker == "angel":
        _refresh_broker_funds(user_id, broker, conn_id, creds)
    return redirect(url_for("live.configure"))


@live_bp.route("/zerodha/login")
def zerodha_login():
    user_id = current_user_id()
    conn_id = svc.conn_id_for(user_id, "zerodha")
    try:
        creds = svc.load_credentials(user_id, conn_id)
    except Exception:
        creds = {}
    api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        return redirect(url_for("live.credentials", broker="zerodha"))
    session["live_broker"] = "zerodha"
    svc.set_selected_broker(user_id, "zerodha")
    return redirect(build_login_url(api_key))


# ══════════════════════════════════════════════════════════════════════════
# ZERODHA OAUTH CALLBACK (host-pinned; stores encrypted token only, this user)
# ══════════════════════════════════════════════════════════════════════════
@live_bp.route("/zerodha/callback")
def zerodha_callback():
    user_id = current_user_id()
    request_token = request.args.get("request_token", "")
    status = request.args.get("status", "")
    conn_id = svc.conn_id_for(user_id, "zerodha")
    if request_token:
        try:
            blob = svc.load_credentials(user_id, conn_id)
        except Exception:
            blob = {}
        api_key = str(blob.get("api_key") or "").strip()
        api_secret = str(blob.get("api_secret") or "").strip()
        try:
            if api_key and api_secret:
                blob.update(
                    exchange_request_token(
                        api_key=api_key,
                        api_secret=api_secret,
                        request_token=request_token,
                    )
                )
        except Exception as exc:
            log.warning("zerodha token exchange failed user=%s err=%s", user_id, type(exc).__name__)
        blob["request_token"] = request_token
        svc.store_credentials(user_id, conn_id, "zerodha", blob)
        svc.upsert_connection(
            user_id,
            conn_id,
            broker="zerodha",
            account_label=blob.get("user_id") or blob.get("api_key") or "zerodha",
            account_ref=_account_ref_from_creds("zerodha", blob),
            status="connected" if blob.get("access_token") else "configured",
        )
        if blob.get("access_token"):
            _refresh_broker_funds(user_id, "zerodha", conn_id, blob)
        session["live_broker"] = "zerodha"
        svc.set_selected_broker(user_id, "zerodha")
        log.info("stored zerodha request_token user=%s (status=%s)", user_id, status)
    return redirect(url_for("live.configure"))


# Selectable strategy presets -> (decision_engine, strategy_version). Available
# to every user from the configure screen. The runner gates on these two configs
# (decision_engine == "champion_replay" enables the champion replay; combined
# with strategy_version == "v2.12" it enables the entry-spot recovery overlay).
STRATEGY_PRESETS = {
    "legacy_v211":   ("signal_engine",   "hybrid_alpha_v28"),
    "champion_v211": ("champion_replay", "v2.11"),
    "champion_v212": ("champion_replay", "v2.12"),
}
STRATEGY_LABELS = {
    "legacy_v211":   "Alpha v2.11 — legacy signal engine",
    "champion_v211": "Alpha v2.11 — champion replay",
    "champion_v212": "Alpha v2.12 — champion + entry-spot recovery",
}


def _current_strategy_preset(user_id: str, conn_id: str) -> str:
    de = svc.get_config(user_id, conn_id, "decision_engine")
    sv = svc.get_config(user_id, conn_id, "strategy_version")
    if de == "champion_replay" and sv == "v2.12":
        return "champion_v212"
    if de == "champion_replay":
        return "champion_v211"
    return "legacy_v211"


# ══════════════════════════════════════════════════════════════════════════
# CONFIGURE — lots + decision strategy + daily-loss (this user's conn)
# ══════════════════════════════════════════════════════════════════════════
def _render_configure(user_id: str, conn_id: str, *, error: str | None = None):
    return render_template(
        "live_configure.html",
        csrf_token=issue_csrf(),
        lots=svc.get_lots(user_id, conn_id),
        lots_phase_cap=LOTS_PHASE_CAP,
        lots_hard_cap=LOTS_HARD_CAP,
        daily_loss_cap=svc.get_daily_loss_cap(user_id, conn_id),
        lot_sizes=LOT_SIZES,
        mode=svc.get_mode(user_id, conn_id),
        kill_switch=svc.is_kill_switch_on(user_id, conn_id),
        strategy=_current_strategy_preset(user_id, conn_id),
        strategy_presets=STRATEGY_LABELS,
        error=error,
    )


@live_bp.route("/configure", methods=["GET", "POST"])
@csrf_protect
def configure():
    svc.ensure_schema()
    user_id, broker, conn_id = _current_conn_id()
    if not conn_id:
        return redirect(url_for("live.connect"))

    if request.method == "POST":
        strat = (request.form.get("strategy") or "").strip()
        strategy_changed = (
            strat in STRATEGY_PRESETS
            and strat != _current_strategy_preset(user_id, conn_id)
        )
        if strategy_changed:
            state = svc.get_trade_state(user_id, conn_id)
            if (state.get("position") or "NONE").upper() == "OPEN":
                return _render_configure(
                    user_id,
                    conn_id,
                    error=(
                        "Decision strategy cannot change while this connection "
                        "has an open position. Close it first, then save."
                    ),
                )

        try:
            lots = int(request.form.get("lots", "1"))
        except ValueError:
            lots = 1
        lots = max(1, min(lots, LOTS_PHASE_CAP, LOTS_HARD_CAP))
        svc.set_config(user_id, conn_id, "lots", lots)

        try:
            cap = float(request.form.get("daily_loss_cap", "3000"))
        except ValueError:
            cap = 3000.0
        svc.set_config(user_id, conn_id, "daily_loss_cap", abs(cap))

        # Strategy preset -> decision_engine + strategy_version. Only applied when
        # a known preset is submitted, so an unrelated POST never clobbers it.
        if strategy_changed:
            decision_engine, strat_version = STRATEGY_PRESETS[strat]
            svc.set_config(user_id, conn_id, "decision_engine", decision_engine)
            svc.set_config(user_id, conn_id, "strategy_version", strat_version)
            svc.clear_v212_latches(user_id, conn_id)

        return redirect(url_for("live.dashboard"))

    return _render_configure(user_id, conn_id)


# ══════════════════════════════════════════════════════════════════════════
# MODE CONTROLS (config mutations only — runner observes; never places here)
# ══════════════════════════════════════════════════════════════════════════
def _require_conn():
    """Resolve (user_id, conn_id) for a mutating control; (None, None) if the
    user hasn't selected/connected a broker yet."""
    user_id, broker, conn_id = _current_conn_id()
    return user_id, conn_id


@live_bp.route("/arm_dry_run", methods=["POST"])
@csrf_protect
def arm_dry_run():
    user_id, conn_id = _require_conn()
    if not conn_id:
        return jsonify({"ok": False, "error": "no_connection"}), 409
    try:
        mode = ex.arm_dry_run(user_id, conn_id)
        return jsonify({"ok": True, "mode": mode.value})
    except ex.InvalidTransition as e:
        return jsonify({"ok": False, "error": str(e),
                        "mode": svc.get_mode(user_id, conn_id)}), 409


@live_bp.route("/arm", methods=["POST"])
@csrf_protect
def arm():
    """Arm LIVE for THIS user's conn. Evaluate all gates against DB/config (NO
    broker call: the web process holds no broker session). If all DB-checkable
    gates pass, transition DRY_RUN -> LIVE_ARMED. The runner re-checks all gates
    with a live adapter immediately before any real order."""
    user_id, conn_id = _require_conn()
    if not conn_id:
        return jsonify({"ok": False, "error": "no_connection"}), 409
    broker = session.get("live_broker")
    if broker:
        _refresh_broker_funds(user_id, broker, conn_id)
    conn_row = svc.get_connection(user_id, conn_id)

    class _DbAdapter:
        """Lightweight stand-in exposing account_ref/is_connected from the DB
        row so gate evaluation needs no broker SDK in the web process."""
        def account_ref(self):
            return (conn_row.get("account_ref")
                    or conn_row.get("account_label", ""))

        def is_connected(self):
            return conn_row.get("status") == "connected"

    gates = ex.evaluate_all(_DbAdapter(), user_id, conn_id)
    # mode_armed is False until we transition; require the OTHER gates first.
    non_mode = [g for g in gates if g.name != "mode_armed"]
    if not ex.all_passed(non_mode):
        failed = [{"name": g.name, "detail": g.detail}
                  for g in non_mode if not g.passed]
        return jsonify({"ok": False, "failed_gates": failed,
                        "mode": svc.get_mode(user_id, conn_id),
                        "broker": broker,
                        "connection_status": (conn_row or {}).get("status"),
                        "relogin_url": (
                            url_for("live.zerodha_login")
                            if broker == "zerodha" else None
                        )}), 409
    try:
        mode = ex.arm_live(user_id, conn_id)
        return jsonify({"ok": True, "mode": mode.value})
    except ex.InvalidTransition as e:
        return jsonify({"ok": False, "error": str(e),
                        "mode": svc.get_mode(user_id, conn_id)}), 409


@live_bp.route("/disarm", methods=["POST"])
@csrf_protect
def disarm():
    user_id, conn_id = _require_conn()
    if not conn_id:
        return jsonify({"ok": False, "error": "no_connection"}), 409
    mode = ex.disarm(user_id, conn_id)
    return jsonify({"ok": True, "mode": mode.value})


@live_bp.route("/kill", methods=["POST"])
@csrf_protect
def kill():
    user_id, conn_id = _require_conn()
    if not conn_id:
        return jsonify({"ok": False, "error": "no_connection"}), 409
    svc.set_config(user_id, conn_id, "kill_switch", 1)
    svc.set_config(user_id, conn_id, "armed", 0)
    return jsonify({"ok": True, "kill_switch": 1})


@live_bp.route("/resume", methods=["POST"])
@csrf_protect
def resume():
    user_id, conn_id = _require_conn()
    if not conn_id:
        return jsonify({"ok": False, "error": "no_connection"}), 409
    svc.set_config(user_id, conn_id, "kill_switch", 0)
    return jsonify({"ok": True, "kill_switch": 0})


@live_bp.route("/refresh_funds", methods=["POST"])
@csrf_protect
def refresh_funds():
    user_id, broker, conn_id = _current_conn_id()
    if not conn_id or not broker:
        return jsonify({"ok": False, "error": "no_connection"}), 409
    ok = _refresh_broker_funds(user_id, broker, conn_id)
    connection = svc.get_connection(user_id, conn_id) or {}
    return jsonify({
        "ok": ok,
        "broker": connection.get("broker"),
        "account_label": connection.get("account_label"),
        "funds_available": connection.get("funds_available"),
        "funds_updated_at": connection.get("funds_updated_at"),
        "funds_error": connection.get("funds_error"),
        "connection_status": connection.get("status"),
    })


# ══════════════════════════════════════════════════════════════════════════
# STATUS — JSON for live.js polling (scoped to session user; never echoes secrets)
# ══════════════════════════════════════════════════════════════════════════
@live_bp.route("/status")
def status():
    svc.ensure_schema()
    user_id, broker, conn_id = _current_conn_id()
    if not conn_id:
        return jsonify({"mode": "DISARMED", "connected": False})
    reconcile_blocked = svc.get_config_int(user_id, conn_id, "reconcile_blocked") == 1
    date_from, date_to = _status_date_range()
    day = svc.get_day_pnl(user_id, conn_id)
    trades = svc.trade_history(user_id, conn_id, limit=100,
                               date_from=date_from, date_to=date_to)
    open_mtm = svc.open_position_mtm(user_id, conn_id)
    connection = svc.get_connection(user_id, conn_id) or {}
    if (
        broker
        and connection.get("status") == "connected"
        and _is_stale_iso(connection.get("funds_updated_at"), max_age_s=60)
    ):
        _refresh_broker_funds(user_id, broker, conn_id)
        connection = svc.get_connection(user_id, conn_id) or {}
    return jsonify({
        "mode": svc.get_mode(user_id, conn_id),
        "kill_switch": 1 if svc.is_kill_switch_on(user_id, conn_id) else 0,
        "lots": svc.get_lots(user_id, conn_id),
        "bot_variant": svc.get_config(user_id, conn_id, "bot_variant"),
        "daily_loss_cap": svc.get_daily_loss_cap(user_id, conn_id),
        # Realized buckets are SEPARATE: today_pnl == LIVE money only.
        "today_pnl": float(day.get("realized_pnl") or 0.0),
        "today_pnl_live": float(day.get("realized_pnl") or 0.0),
        "today_pnl_dry": float(day.get("realized_pnl_dry") or 0.0),
        "reconcile_ok": (not reconcile_blocked),
        "reconcile_warning": svc.get_config(user_id, conn_id, "reconcile_message"),
        "broker": connection.get("broker"),
        "account_label": connection.get("account_label"),
        "connection_status": connection.get("status"),
        "funds_available": connection.get("funds_available"),
        "funds_updated_at": connection.get("funds_updated_at"),
        "funds_error": connection.get("funds_error"),
        "last_orders": svc.recent_orders(user_id, conn_id, limit=20),
        "trade_date": trades.get("trade_date"),
        "date_from": trades["date_from"],
        "date_to": trades["date_to"],
        "trade_pnl": trades["trade_pnl"],
        "trade_count": trades["trade_count"],
        "live_pnl": trades["live_pnl"],
        "live_count": trades["live_count"],
        "dry_pnl": trades["dry_pnl"],
        "dry_count": trades["dry_count"],
        "trades": trades["trades"],
        "by_strategy": trades["by_strategy"],
        "strategy": _current_strategy_preset(user_id, conn_id),
        "open_mtm": open_mtm,
    })
