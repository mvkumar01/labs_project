"""
auth_gate.py — per-user session auth before_request + CSRF + login throttle
(spec §10). Multi-user: there is NO single shared passcode. Each user has a row
in live_users (username + bcrypt/argon2 passcode_hash); registration/verification
live in live_service. This module only enforces that a valid session['user_id']
exists for the /live blueprint and provides CSRF + throttle helpers.

Isolation: imports ONLY neutral stdlib + flask. NEVER a broker SDK, NEVER
labs.engine.*. Places no orders; mutates no trading state.

Security:
  * The gate FAILS CLOSED — no session['user_id'] => no access to /live/* (other
    than the exempt login/register pages + static).
  * Session cookies hardened: HttpOnly + SameSite=Lax (+ Secure in prod) and a
    30-minute idle timeout.
  * Login attempts throttled (5 / minute / username+remote-addr) to blunt brute
    force. The submitted passcode is NEVER logged or echoed.
"""
import hmac
import logging
import time
from collections import defaultdict, deque
from functools import wraps

from flask import redirect, request, session, url_for, abort

log = logging.getLogger("live.auth")

# In-process throttle: (username|ip) -> deque[timestamps]. Resets on restart.
_ATTEMPTS = defaultdict(deque)
_WINDOW_S = 60
_MAX_ATTEMPTS = 5

_CSRF_KEY = "live_csrf"


def _client_id() -> str:
    return request.headers.get(
        "X-Forwarded-For", request.remote_addr or "?"
    ).split(",")[0].strip()


def current_user_id():
    """The logged-in user's id, or None. Routes use this — they NEVER trust a
    client-supplied user_id/conn_id (spec §2, §11)."""
    return session.get("user_id")


def registration_open() -> bool:
    """True only if an invite code is configured (env LIVE_INVITE_CODE).
    Fails closed: with no code set, self-service registration is DISABLED so a
    fresh public deploy can never be open to anyone."""
    import os
    return bool(os.environ.get("LIVE_INVITE_CODE"))


def verify_invite_code(submitted: str) -> bool:
    """Constant-time check of the submitted invite code against env
    LIVE_INVITE_CODE. Returns False (fail closed) when no code is configured or
    nothing was submitted. The code itself is never logged."""
    import os
    expected = os.environ.get("LIVE_INVITE_CODE", "")
    if not expected or not submitted:
        return False
    return hmac.compare_digest(submitted.encode(), expected.encode())


def login_throttled(username: str = "") -> bool:
    """True if this username+IP has exceeded the attempt budget in the window."""
    key = f"{username}|{_client_id()}"
    now = time.time()
    dq = _ATTEMPTS[key]
    while dq and now - dq[0] > _WINDOW_S:
        dq.popleft()
    return len(dq) >= _MAX_ATTEMPTS


def record_attempt(username: str = "") -> None:
    _ATTEMPTS[f"{username}|{_client_id()}"].append(time.time())


# Back-compat alias (some callers used the bare name).
def throttled() -> bool:
    return login_throttled("")


# ── CSRF (per-session token; checked on every mutating POST) ───────────────
def issue_csrf() -> str:
    import secrets as _secrets
    tok = session.get(_CSRF_KEY)
    if not tok:
        tok = _secrets.token_hex(16)
        session[_CSRF_KEY] = tok
    return tok


def check_csrf() -> bool:
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    have = session.get(_CSRF_KEY, "")
    return bool(have) and hmac.compare_digest(sent, have)


def csrf_protect(fn):
    """Decorator for POST handlers: 400 on missing/mismatched CSRF token."""
    @wraps(fn)
    def _wrap(*a, **kw):
        if request.method == "POST" and not check_csrf():
            abort(400, "CSRF token invalid")
        return fn(*a, **kw)
    return _wrap


def register_auth_gate(app) -> None:
    """Install @app.before_request: allow /live/login, /live/register, and
    static; otherwise require a valid session['user_id'] (redirect unauthenticated
    /live pages to the login page; 401 JSON for API). Routes outside /live are
    untouched (paper /labs unaffected). Fails closed.

    Also hardens session cookies (HttpOnly + SameSite=Lax, Secure in prod) and a
    30-minute idle timeout."""
    from datetime import timedelta

    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    if not app.debug:
        app.config["SESSION_COOKIE_SECURE"] = True
    app.permanent_session_lifetime = timedelta(minutes=30)

    @app.before_request
    def _live_auth_gate():
        path = request.path or ""
        # Only guard the /live blueprint. Everything else is out of scope.
        if not path.startswith("/live"):
            return None
        # Exempt the login + register endpoints and static assets.
        if (path.startswith("/live/login")
                or path.startswith("/live/register")
                or path.startswith("/static")):
            return None
        if session.get("user_id"):
            session.permanent = True  # refresh idle timeout
            return None
        # Unauthenticated: API calls get 401 JSON; pages redirect to login.
        if (path.startswith("/live/api")
                or request.accept_mimetypes.best == "application/json"):
            return ("Unauthorized", 401)
        return redirect(url_for("live.login"))
