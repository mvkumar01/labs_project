# LIVE BROKER CONNECTION — Implementation Plan (Zerodha + Angel One)

**For:** a fresh agent/session (Sonnet) implementing/finishing broker session connectivity for the labs live stack.
**Repo:** `C:/Users/vipin/labs_project` → PA `labs-mvkumar01.pythonanywhere.com` (source `/home/mvkumar01/labs_project`, Python 3.10, **no virtualenv** — use `pip3.10 install --user`).
**Context:** Phase-0/dry-run core is built & pushed (`origin/main`). The order guard (`_LIVE_ORDERS_ENABLED`) stays OFF — this plan is only about **establishing/refreshing the broker SESSION** so the `broker_connected` gate passes and funds show. Do NOT flip the order guard here.

Key code: `live/brokers/angel.py`, `live/brokers/zerodha.py`, `live/proxy.py`, `labs/ui/live_routes.py` (`_refresh_broker_funds`, `/arm`, `/zerodha/login`, `/zerodha/callback`).

---

## A. ANGEL ONE — auto-login (no daily manual step) — PRIMARY, do this first

**Status:** `AngelAdapter` is fully implemented: `connect()` does `generateSession(client_code, pin, pyotp.TOTP(totp_secret).now())`, plus `is_connected()`, `available_funds()`, `get_spot/get_ltp/quote`, `get_position`, `get_order_status`, instrument-master token resolution, and guarded `place_order/exit_all`. Because the **TOTP secret** is stored, the session can be regenerated automatically any time — **no interactive login, no daily user action**. The runner/web can reconnect on its own each morning.

**Only blockers are operational:**

### A1. Install the Angel SDK on PA (this is the current failure)
PA error log shows `funds refresh failed ... broker=angel err=ModuleNotFoundError`. On a PA Bash console:
```
pip3.10 install --user smartapi-python pyotp logzero websocket-client requests
python3.10 -c "import SmartApi, pyotp; print('angel sdk ok')"
```
Reload the web app + restart the always-on runner task after install.
- **Acceptance:** the import line prints ok; no more `ModuleNotFoundError` in the error log on connect.

### A2. Outbound connectivity / static IP (only if needed)
`live/proxy.py:configure_outbound_proxy()` is opt-in via env. Angel calls reach `apiconnect.angelbroking.com` and the instrument master at `margincalculator.angelbroking.com`.
- Paid PA accounts have unrestricted outbound → usually nothing to do.
- If Angel requires **IP whitelisting** (or outbound is blocked), set `LIVE_OUTBOUND_PROXY_URL` (a QuotaGuard-style static-IP proxy) in BOTH the web WSGI env and the runner task env, and whitelist that IP in the Angel developer app.
- **Acceptance:** `_ensure_instrument_master()` downloads `OpenAPIScripMaster.json` to `storage/state/angel_instruments.json` without a network error.

### A3. Credentials correctness (common mistake)
The Angel credential form needs: `api_key`, `client_code`, `pin`, and the **TOTP secret** = the base32 seed from Angel's "Enable TOTP" QR/secret — **NOT** a 6-digit code. If a 6-digit code was entered, `pyotp.TOTP(...).now()` produces garbage and login fails.
- Add a hint on the `live_credentials.html` Angel form: "TOTP Secret = the base32 key shown when enabling TOTP, not the 6-digit code."
- **Acceptance:** with correct creds, `connect()` + `is_connected()` (`rmsLimit()` ping) succeed.

### A4. Verify end-to-end on PA (read-only diagnostic, operator-run)
After A1–A3, confirm a live session. Operator runs in a PA Bash console (reads only; no orders — guard is off anyway):
```
cd ~/labs_project && python3.10 -c "
import os; os.environ.setdefault('LABS_CRED_KEY', open('/home/mvkumar01/.labs_cred_key').read().strip()) if os.path.exists('/home/mvkumar01/.labs_cred_key') else None
from live import live_service as svc
# find the angel conn_id for the user, load creds, connect
# (use the real user_id; list via: svc... )
"
```
Simpler: on the dashboard click **Refresh** funds — Angel funds should populate and status flip to `connected`; then **Arm LIVE** no longer fails `broker_connected`.
- **Acceptance:** `/live/status` shows Angel `connected` + funds; Arm LIVE passes the broker gate (order placement still blocked by the guard).

### A5. Runner auto-reconnect (so it survives daily token expiry)
Angel session tokens also expire daily, but since login is automatable, the **always-on runner must (re)connect per boot and re-establish on auth failure**. In `live/live_runner.py`, ensure each connection's adapter is `connect()`-ed at boot and **reconnected when `is_connected()` returns False mid-session** (not cached forever). Add a once-per-day (or on-failure) reconnect.
- **Acceptance:** runner started before market open is `connected` without any manual step; recovers if the session drops.

---

## B. ZERODHA — accepted manual daily login (no automation)

**Decision (operator):** Zerodha will require a **manual login each trading day**. Do NOT build TOTP auto-login for Zerodha. Just make the daily flow clean and obvious.

**Model:** Kite Connect tokens are valid one trading day (~6 AM IST expiry). `ZerodhaAdapter.connect()` uses the per-user `access_token` from the encrypted blob (no fallback). Flow already implemented: `/live/connect` → credentials (`api_key`,`api_secret`) → `/live/zerodha/login` → `build_login_url` → Kite login → `/live/zerodha/callback?request_token=…` → `exchange_request_token()` → store `access_token`+`user_id`.

**Work to make the daily flow clean:**

### B1. Fix the stale "connected" badge (real bug)
Today the dashboard showed `connected` + `--` funds with an expired token. `_refresh_broker_funds` / status must set the connection to **`disconnected`/`expired`** when `is_connected()` (profile ping) fails, instead of leaving a stale `connected`.
- **Acceptance:** with an expired token, `/live/status` shows `disconnected` (not `connected`).

### B2. Add a one-click "Re-login to Kite" CTA
On `live_status.html` (and when arming fails on `broker_connected` for a zerodha conn), show a prominent "Zerodha session expired — Re-login" button → `/live/zerodha/login`. This is the daily action the operator/user takes.
- **Acceptance:** clicking it runs the Kite login → callback → fresh `access_token`; funds populate; arm gate passes.

### B3. Kite app config (one-time, per user)
Each Zerodha user needs their own Kite Connect app with redirect URI exactly:
`https://labs-mvkumar01.pythonanywhere.com/live/zerodha/callback`
Document this on the credentials page.
- **Acceptance:** the OAuth round-trip completes without redirect-mismatch errors.

### B4. (Optional, operator-only convenience — SKIP unless asked)
The operator's own account (YVR034) already gets a daily token from the scheduled `auth/generate_token.py` → `config/zerodha_token.json`. A small fallback in `ZerodhaAdapter.connect()` (use that file's token when the per-user blob has none) would auto-refresh the operator's live Zerodha. **Operator said manual daily login is acceptable → leave this out unless they change their mind.**

---

## C. Cross-cutting
- After any PA `pip install`: reload the web app AND restart the always-on runner task (new packages aren't picked up live).
- Env vars: web WSGI already has `LABS_SECRET_KEY`, `LABS_CRED_KEY`, `LIVE_INVITE_CODE`; runner task needs `LABS_CRED_KEY` (and `LIVE_OUTBOUND_PROXY_URL` if A2 applies).
- Keep `tests/test_live_isolation.py` green; broker SDK imports stay inside `live/brokers/*`.
- Do NOT flip `_LIVE_ORDERS_ENABLED` / set `LIVE_ORDERS_ENABLED=1` in this plan — session connectivity only.

## D. Definition of done
- Angel: connects automatically (no manual login), funds + status `connected`, Arm-LIVE broker gate passes, runner auto-reconnects daily.
- Zerodha: clean daily manual re-login via a visible CTA; status correctly reflects expired vs connected; arm gate passes after login.
