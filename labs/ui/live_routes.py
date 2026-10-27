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
import secrets
import hmac
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, jsonify,
)

from live import live_service as svc
from live import live_executor as ex
from live import control_plane as cp
from live import proposer_sizing as sizing
from live.engine import proposer_engine as pe
from live.auth_gate import (
    current_user_id, login_throttled, record_attempt, issue_csrf, csrf_protect,
    registration_open, verify_invite_code,
)
from live.brokers.zerodha import build_login_url, exchange_request_token

log = logging.getLogger("live.routes")

live_bp = Blueprint("live", __name__, url_prefix="/live")


@live_bp.context_processor
def _nav_context():
    """Every Live page shows the same nav; Administration only for admins."""
    try:
        user_id = current_user_id()
        return {"is_admin": bool(user_id) and cp.is_admin(user_id)}
    except Exception:                                   # noqa: BLE001 - nav must never 500
        return {"is_admin": False}

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

# No lot ceiling (operator, 2026-10-02). One order cannot exceed the exchange freeze
# quantity (SENSEX 1,000 / NIFTY 1,800 units): the bots size an entry down to it, and
# step down further when the broker refuses the size (live_executor size step-down).
FREEZE_QTY = ex.FREEZE_QTY


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
    broker = (svc.get_selected_broker(user_id) or "").lower() if user_id else None
    if broker not in SUPPORTED_BROKERS:
        broker = None
    if user_id and not broker:
        broker = svc.get_selected_broker(user_id)
        if broker:
            session["live_broker"] = broker
    if broker:
        session['live_broker'] = broker
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
        is_admin=cp.is_admin(user_id),
        csrf_token=issue_csrf(),
        username=(svc.get_user(user_id) or {}).get("username", ""),
        mode=svc.get_mode(user_id, conn_id),
        kill_switch=svc.is_kill_switch_on(user_id, conn_id),
        connection=connection,
        configured=bool(connection),
        lots=svc.get_lots(user_id, conn_id),
        daily_loss_cap=svc.get_daily_loss_cap(user_id, conn_id),
        strategy_label=_strategy_label(user_id, conn_id),
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

    if broker == 'zerodha' and any(creds.get(k) != existing.get(k) for k in ('api_key','api_secret')):
        for key in ('access_token','public_token','request_token','user_id'):
            creds.pop(key, None)

    try:
        cp.save_connection_credentials(user_id,conn_id,broker,creds,existing)
    except cp.ControlError as exc:
        return redirect(url_for('live.credentials',broker=broker,error=str(exc)))
    log.info("stored credentials user=%s broker=%s (fields=%s)",
             user_id, broker, sorted(creds.keys()))  # field NAMES only
    if broker == "zerodha" and creds.get("api_key") and creds.get("api_secret"):
        return redirect(url_for("live.zerodha_login"))
    if broker == "angel":
        if not _refresh_broker_funds(user_id, broker, conn_id, creds):
            return redirect(url_for('live.credentials', broker=broker, error='Broker authentication failed; active broker unchanged'))
        try:
            cp.activate_broker(user_id, broker)
            session['live_broker'] = broker
        except cp.ControlError as exc:
            return redirect(url_for('live.credentials', broker=broker, error=str(exc)))
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
    session['kite_login_pending'] = _now_iso()
    session['kite_login_state'] = secrets.token_urlsafe(32)
    return redirect(build_login_url(api_key, session['kite_login_state']))


# ══════════════════════════════════════════════════════════════════════════
# ZERODHA OAUTH CALLBACK (host-pinned; stores encrypted token only, this user)
# ══════════════════════════════════════════════════════════════════════════
@live_bp.route("/zerodha/callback")
def zerodha_callback():
    user_id = current_user_id()
    request_token = request.args.get("request_token", "")
    pending = session.pop('kite_login_pending', None)
    expected_state = session.pop('kite_login_state', '')
    if not pending or not expected_state or not hmac.compare_digest(expected_state, request.args.get('state','')) or _is_stale_iso(pending, max_age_s=600):
        return redirect(url_for('live.credentials', broker='zerodha', error='Login expired; start Kite login again'))
    conn_id = svc.conn_id_for(user_id, "zerodha")
    if request_token:
        try:
            blob = svc.load_credentials(user_id, conn_id)
        except Exception:
            blob = {}
        api_key = str(blob.get("api_key") or "").strip()
        api_secret = str(blob.get("api_secret") or "").strip()
        previous_blob = dict(blob)
        try:
            if not api_key or not api_secret:
                raise ValueError('Missing credentials')
            fresh = exchange_request_token(api_key=api_key, api_secret=api_secret, request_token=request_token)
            if not fresh.get('access_token'):
                raise ValueError('Missing access token')
            blob.update(fresh)
        except Exception as exc:
            log.warning("zerodha token exchange failed user=%s err=%s", user_id, type(exc).__name__)
            return redirect(url_for('live.credentials', broker='zerodha', error='Kite login failed; active broker unchanged'))
        blob.pop('request_token', None)
        try:
            cp.save_connection_credentials(user_id,conn_id,'zerodha',blob,previous_blob)
        except cp.ControlError as exc:
            return redirect(url_for('live.credentials',broker='zerodha',error=str(exc)))
        if not _refresh_broker_funds(user_id, "zerodha", conn_id, blob):
            return redirect(url_for('live.credentials', broker='zerodha', error='Kite authentication failed; active broker unchanged'))
        try:
            cp.activate_broker(user_id,'zerodha')
            session['live_broker'] = 'zerodha'
        except cp.ControlError as exc:
            return redirect(url_for('live.credentials', broker='zerodha', error=str(exc)))
    return redirect(url_for("live.configure"))


@live_bp.route('/select_broker/<broker>', methods=['POST'])
@csrf_protect
def select_broker(broker):
    if broker not in SUPPORTED_BROKERS:
        return 'Unsupported broker', 400
    user_id = current_user_id()
    if not _refresh_broker_funds(user_id, broker, svc.conn_id_for(user_id, broker)):
        return redirect(url_for('live.credentials', broker=broker, error='Reconnect this broker before selecting it'))
    try:
        cp.activate_broker(user_id,broker)
    except cp.ControlError as exc:
        return redirect(url_for('live.credentials',broker=broker,error=str(exc)))
    session['live_broker']=broker
    return redirect(url_for('live.dashboard'))


# Selectable strategy presets -> (decision_engine, strategy_version). Available
# to every user from the configure screen. The runner gates on these two configs
# (decision_engine == "champion_replay" enables the champion replay; combined
# with the strategy version it enables the corresponding overlay).
STRATEGY_PRESETS = {
    "legacy_v211":   ("signal_engine",   "hybrid_alpha_v28"),
    "champion_v211": ("champion_replay", "v2.11"),
    "champion_v211b": ("champion_replay", "v2.11b"),
    "champion_v212": ("champion_replay", "v2.12"),
    "champion_v212_close_confirmed": (
        "champion_replay", "v2.12_closed_confirmed"
    ),
    "champion_v212_b10": ("champion_replay", "v2.12_b10"),
    "champion_v214": ("champion_replay", "v2.14"),
    # v2.14 with the Renko 30 overlay of paper book v2.14 C and no new position from 14:00.
    "champion_v214e": ("champion_replay", "v2.14e"),
    "champion_v213": ("champion_replay", "v2.13"),
    # SENSEX options, run by its own runner process (live/proposer_runner.py).
    "proposer_dt25": ("proposer", "proposer_dt25"),
    # The same Proposer with the 1-min price-action exit and a one-loss-per-day stop.
    "proposer_dt25_px": ("proposer", "proposer_dt25_px"),
    # The base Proposer taking only entries that go with the day (75 points beyond the 09:15 close),
    # premium floor at -20%.
    "proposer_dt25_v3": ("proposer", "proposer_dt25_v3"),
    # Sensex V4 / V5: v3 at a 3.5% day target with Sentinel's strike and the 270 s wait. One preset; its
    # loss floor (SENSEX_V45_FLOORS) picks the version: -20% of the premium is proposer_dt35_v3 (Sensex V4),
    # 150 SENSEX points against the entry is proposer_dt35_v5 (Sensex V5). The default is V4.
    "proposer_sensex_v45": ("proposer", "proposer_dt35_v3"),
    "nifty_scalper_v1": ("proposer", "proposer_nifty_scalper_v1"),
}
SENSEX_V45 = "proposer_sensex_v45"
SENSEX_V45_FLOORS = {
    "premium_20": ("proposer_dt35_v3", "Premium down 20% (Sensex V4)"),
    "spot_150": ("proposer_dt35_v5", "SENSEX 150 points against the entry (Sensex V5, no premium floor)"),
}
STRATEGY_LABELS = {
    "legacy_v211":   "Alpha v2.11 (legacy)",
    "champion_v211": "Alpha v2.11",
    "champion_v211b": "Alpha 2.11 replay (B)",
    "champion_v212": "Alpha v2.12",
    "champion_v212_close_confirmed": "Alpha v2.12 (close-confirmed)",
    "champion_v212_b10": "Alpha v2.14 A (B10: 10-pt stop buffer)",
    "champion_v214": "Alpha v2.14 B (v2.11 replay (B) + B10)",
    "champion_v214e": "Alpha v2.14 E (v2.14 C: Renko 30 exit and re-entry; no new position from 14:00)",
    "champion_v213": "Alpha v2.13",
    "proposer_dt25": "SENSEX Proposer (2.5% day target)",
    "proposer_dt25_px": "SENSEX Proposer + Renko-50 exit, one loss per day",
    "proposer_dt25_v3": "SENSEX Proposer v3 (improved: trades only with the day's direction, stop at -20%)",
    "proposer_sensex_v45": "Sensex V4 / V5 (v3 with the day, Sentinel strike, 270 s wait; "
                           "loss floor and day target chosen below)",
    "nifty_scalper_v1": "Nifty Scalper V1 (Sensex V5 signals on NIFTY options; 1.5% day target)",
}


# Day-target choices for Sensex V4 / V5 (percent; "other" = typed in). Stored per connection in
# proposer_day_target_pct; blank = the preset's 3.5%.
V45_DAY_TARGETS = {"3.5": "3.5% (Sensex preset)", "2.5": "2.5% (as Pramanaa's 2.5% accounts)",
                   "1.5": "1.5% (tested on NIFTY)"}
V45_DEFAULT_DAY_TARGET = "3.5"


def _v45_day_target(user_id: str, conn_id: str) -> str:
    """The connection's day target as a percent string ("3.5" when unset or unreadable)."""
    value = pe.parse_day_target(svc.get_config(user_id, conn_id, "proposer_day_target_pct"))
    default = ("1.5" if svc.get_config(user_id, conn_id, "strategy_version") ==
               pe.STRATEGY_VERSION_NIFTY_SCALPER_V1 else V45_DEFAULT_DAY_TARGET)
    return f"{value * 100:g}" if value else default


# Print clock for any Proposer preset (config proposer_print_clock; blank = ours). See
# live/proposer_runner.py PRINT_CLOCKS.
PRINT_CLOCK_CHOICES = {"ours": "Ours - first print 09:22, then every 5 min",
                       "pramanaa": "Pramanaa's - first print 09:21 (forming candle counted), then every 310.5 s"}


def _print_clock(user_id: str, conn_id: str) -> str:
    value = (svc.get_config(user_id, conn_id, "proposer_print_clock") or "").strip()
    return value if value in PRINT_CLOCK_CHOICES else "ours"


def _v45_floor(version: str | None) -> str:
    return next((k for k, (v, _) in SENSEX_V45_FLOORS.items() if v == version), "premium_20")


def _strategy_label(user_id: str, conn_id: str) -> str:
    preset = _current_strategy_preset(user_id, conn_id)
    if preset != SENSEX_V45:
        return STRATEGY_LABELS[preset]
    floor = SENSEX_V45_FLOORS[_v45_floor(svc.get_config(user_id, conn_id, "strategy_version"))][1]
    return f"Sensex V4 / V5 - loss floor: {floor}; day target {_v45_day_target(user_id, conn_id)}%"


def _current_strategy_preset(user_id: str, conn_id: str) -> str:
    de = svc.get_config(user_id, conn_id, "decision_engine")
    sv = svc.get_config(user_id, conn_id, "strategy_version")
    if de == "proposer":
        if sv in {v for v, _ in SENSEX_V45_FLOORS.values()}:
            return SENSEX_V45
        return next((k for k, pair in STRATEGY_PRESETS.items() if pair == (de, sv)), "proposer_dt25")
    if de == "champion_replay" and sv == "v2.11b":
        return "champion_v211b"
    if de == "champion_replay" and sv == "v2.13":
        return "champion_v213"
    if de == "champion_replay" and sv == "v2.12_b10":
        return "champion_v212_b10"
    if de == "champion_replay" and sv == "v2.14":
        return "champion_v214"
    if de == "champion_replay" and sv == "v2.14e":
        return "champion_v214e"
    if de == "champion_replay" and sv == "v2.12_closed_confirmed":
        return "champion_v212_close_confirmed"
    if de == "champion_replay" and sv == "v2.12":
        return "champion_v212"
    if de == "champion_replay":
        return "champion_v211"
    return "legacy_v211"


# ══════════════════════════════════════════════════════════════════════════
# CONFIGURE — lots + decision strategy + daily-loss (this user's conn)
# ══════════════════════════════════════════════════════════════════════════
def _runner_not_ready_for(user_id: str, conn_id: str, preset: str, version: str | None = None) -> str | None:
    """A NIFTY strategy the running runner has not loaded must not be selected: a runner
    process from an earlier release trades a version it does not know as plain v2.11. The
    runner publishes the decision contract it loaded with every heartbeat. The same holds for
    the Proposer runner (an unknown version would trade on the base preset's rules)."""
    decision_engine, preset_version = STRATEGY_PRESETS[preset]
    version = version or preset_version
    if decision_engine == "proposer":
        loaded = svc.get_config(user_id, conn_id, "runner_decision_abi") or ""
        if loaded.startswith("proposer-") and loaded != ex.expected_decision_abi(version):
            return ("The SENSEX runner is still running an earlier release and has not loaded this "
                    "strategy. It has to be restarted first; the strategy was not changed.")
        return None
    if decision_engine != "champion_replay":
        return None
    loaded = svc.get_config(user_id, conn_id, "runner_decision_abi") or ""
    if loaded.startswith("alpha-") and loaded != ex.expected_decision_abi(version):
        return ("The NIFTY runner is still running an earlier release and has not loaded this "
                "strategy. It has to be restarted first; the strategy was not changed.")
    return None


def _render_configure(user_id: str, conn_id: str, *, error: str | None = None):
    return render_template(
        "live_configure.html",
        csrf_token=issue_csrf(),
        lots=svc.get_lots(user_id, conn_id),
        freeze_qty=FREEZE_QTY,
        daily_loss_cap=svc.get_daily_loss_cap(user_id, conn_id),
        lot_sizes=LOT_SIZES,
        mode=svc.get_mode(user_id, conn_id),
        kill_switch=svc.is_kill_switch_on(user_id, conn_id),
        strategy=_current_strategy_preset(user_id, conn_id),
        strategy_presets=STRATEGY_LABELS,
        v45_preset=SENSEX_V45,
        v45_floors={k: label for k, (_, label) in SENSEX_V45_FLOORS.items()},
        v45_floor=_v45_floor(svc.get_config(user_id, conn_id, "strategy_version")),
        v45_day_targets=V45_DAY_TARGETS,
        v45_day_target=_v45_day_target(user_id, conn_id),
        v45_day_target_bounds=(pe.DAY_TARGET_MIN_PCT, pe.DAY_TARGET_MAX_PCT),
        print_clocks=PRINT_CLOCK_CHOICES,
        print_clock=_print_clock(user_id, conn_id),
        proposer_presets=[k for k, pair in STRATEGY_PRESETS.items() if pair[0] == "proposer"],
        proposer_sizing_modes=sizing.MODES,
        proposer_sizing_mode=svc.get_config(user_id, conn_id, "proposer_sizing_mode") or sizing.FIXED,
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
        target = STRATEGY_PRESETS.get(strat)
        if strat == SENSEX_V45:
            floor = (request.form.get("loss_floor") or "").strip()
            if floor not in SENSEX_V45_FLOORS:
                floor = _v45_floor(svc.get_config(user_id, conn_id, "strategy_version"))
            target = ("proposer", SENSEX_V45_FLOORS[floor][0])
        day_target = None
        if strat in (SENSEX_V45, "nifty_scalper_v1"):
            choice = (request.form.get("day_target") or "").strip()
            if choice == "other":
                typed = request.form.get("day_target_custom")
                parsed = pe.parse_day_target(typed)
                if parsed is None:
                    return _render_configure(user_id, conn_id, error=(
                        f"Day target {str(typed or '').strip() or '(blank)'} is not a percent between "
                        f"{pe.DAY_TARGET_MIN_PCT:g} and {pe.DAY_TARGET_MAX_PCT:g}. Nothing was changed."))
                day_target = f"{parsed * 100:g}"
            elif choice in V45_DAY_TARGETS:
                day_target = choice
            else:
                day_target = ("1.5" if strat == "nifty_scalper_v1" and
                              strat != _current_strategy_preset(user_id, conn_id)
                              else _v45_day_target(user_id, conn_id))
        strategy_changed = target is not None and (
            strat != _current_strategy_preset(user_id, conn_id)
            or target[1] != svc.get_config(user_id, conn_id, "strategy_version")
            or (day_target is not None and day_target != _v45_day_target(user_id, conn_id))
        )
        current_sizing = svc.get_config(user_id, conn_id, "proposer_sizing_mode") or sizing.FIXED
        sizing_mode = current_sizing
        is_proposer = target is not None and target[0] == "proposer"
        if is_proposer:
            sizing_mode = request.form.get("proposer_sizing_mode", current_sizing)
            if sizing_mode not in sizing.MODES:
                return _render_configure(user_id, conn_id, error="Select Fixed or Dynamic lot sizing. Nothing was changed.")
        sizing_changed = is_proposer and sizing_mode != current_sizing
        print_clock = (request.form.get("print_clock") or "").strip()
        clock_changed = is_proposer and print_clock in PRINT_CLOCK_CHOICES and print_clock != _print_clock(user_id, conn_id)
        if clock_changed:
            state = svc.get_trade_state(user_id, conn_id)
            if (state.get("position") or "NONE").upper() == "OPEN":
                return _render_configure(user_id, conn_id, error=(
                    "The print clock cannot change while this connection has an open position. "
                    "Close it first, then save."))
            if svc.get_config(user_id, conn_id, "runner_decision_abi") != ex.PROPOSER_DECISION_ABI:
                return _render_configure(user_id, conn_id, error=(
                    "The print clock needs the updated Proposer runner. Restart it first; nothing was changed."))
        if strategy_changed or sizing_changed:
            state = svc.get_trade_state(user_id, conn_id)
            if (state.get("position") or "NONE").upper() == "OPEN":
                return _render_configure(
                    user_id,
                    conn_id,
                    error=(
                        ("Decision strategy cannot change while this connection " if strategy_changed else
                         "Lot sizing cannot change while this connection ") +
                        "has an open position. Close it first, then save."
                    ),
                )
            not_ready = _runner_not_ready_for(user_id, conn_id, strat, target[1])
            if not_ready:
                return _render_configure(user_id, conn_id, error=not_ready)
        if is_proposer and sizing_mode == sizing.DYNAMIC:
            if svc.get_config(user_id, conn_id, "runner_decision_abi") != ex.PROPOSER_DECISION_ABI:
                return _render_configure(user_id, conn_id, error=(
                    "Dynamic sizing requires the updated Proposer runner. Restart it first; nothing was changed."))

        try:
            lots = int(request.form.get("lots", "1"))
        except ValueError:
            lots = 1
        lots = max(1, lots)
        svc.set_config(user_id, conn_id, "lots", lots)

        try:
            cap = float(request.form.get("daily_loss_cap", "3000"))
        except ValueError:
            cap = 3000.0
        svc.set_config(user_id, conn_id, "daily_loss_cap", abs(cap))
        if sizing_changed:
            svc.set_config(user_id, conn_id, "proposer_sizing_mode", sizing_mode)
        if clock_changed:
            svc.set_config(user_id, conn_id, "proposer_print_clock", print_clock)

        # Strategy preset -> decision_engine + strategy_version. Only applied when
        # a known preset is submitted, so an unrelated POST never clobbers it.
        if strategy_changed:
            decision_engine, strat_version = target
            svc.set_config(user_id, conn_id, "decision_engine", decision_engine)
            svc.set_config(user_id, conn_id, "strategy_version", strat_version)
            if day_target is not None:
                svc.set_config(user_id, conn_id, "proposer_day_target_pct", day_target)
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
    from labs.ui.live_admin_routes import readiness
    ready = readiness(user_id, conn_id)
    if not ready['ready']:
        return jsonify({'ok':False,'failed_gates':[{'name':c['name'],'detail':c['detail']} for c in ready['checks'] if not c['passed']]}),409
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


from labs.ui.live_admin_routes import register_routes
register_routes(live_bp)
