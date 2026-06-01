"""
Run at 08:55 IST daily (PA scheduled task).
Performs Zerodha TOTP login and writes access token to config/zerodha_token.json.
This is Labs-specific auth — completely separate from alphaIMB's token.

Credentials are read from config/zerodha_creds.json.
"""
import json
import sys
import time
from pathlib import Path

import pyotp
import requests

BASE_DIR    = Path(__file__).resolve().parent.parent
CREDS_PATH  = BASE_DIR / "config" / "zerodha_creds.json"
TOKEN_PATH  = BASE_DIR / "config" / "zerodha_token.json"

def _load_creds() -> dict:
    if not CREDS_PATH.exists():
        raise FileNotFoundError(f"Credentials file not found: {CREDS_PATH}")
    creds = json.loads(CREDS_PATH.read_text())
    missing = [k for k in ("api_key", "api_secret", "user_id", "password", "totp_key") if not creds.get(k)]
    if missing:
        raise ValueError(f"Missing fields in zerodha_creds.json: {missing}")
    return creds


def _get_request_token(kite, creds: dict) -> str:
    session = requests.Session()

    # Step 1: submit credentials
    resp = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": creds["user_id"], "password": creds["password"]},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Login failed: {data}")

    request_id = data["data"]["request_id"]

    # Step 2: submit TOTP
    totp = pyotp.TOTP(creds["totp_key"]).now()
    resp = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={"user_id": creds["user_id"], "request_id": request_id, "twofa_value": totp},
    )
    resp.raise_for_status()

    # Step 3: walk the redirect chain until we find request_token.
    # Zerodha goes: login_url → connect/finish?sess_id=... → localhost/?request_token=...
    url = kite.login_url()
    for _ in range(5):
        resp = session.get(url, allow_redirects=False)
        location = resp.headers.get("location", "")
        if "request_token=" in location:
            return location.split("request_token=")[1].split("&")[0]
        if not location:
            raise RuntimeError(f"Redirect chain ended without request_token. Last URL: {url}")
        url = location

    raise RuntimeError("Exhausted redirect hops without finding request_token.")


def generate_and_save_token() -> dict:
    from kiteconnect import KiteConnect
    creds = _load_creds()
    kite  = KiteConnect(api_key=creds["api_key"])

    print(f"[labs-auth] cwd={Path.cwd()} base={BASE_DIR}")
    request_token = _get_request_token(kite, creds)
    session_data  = kite.generate_session(request_token, api_secret=creds["api_secret"])
    access_token  = session_data["access_token"]

    payload = {
        "api_key":      creds["api_key"],
        "access_token": access_token,
        "user_id":      creds["user_id"],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    TOKEN_PATH.write_text(json.dumps(payload, indent=2))
    print(f"[labs-auth] Token saved: {TOKEN_PATH}")
    return payload


if __name__ == "__main__":
    generate_and_save_token()
