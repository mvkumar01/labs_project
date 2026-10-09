"""Read-only probe of an Angel One connection for MCX futures. NO ORDER IS PLACED.

Answers, before any MCX order path exists:
  - is the commodity segment on the account (profile exchanges)?
  - what does Angel's margin calculator ask for one lot of GOLDM, GOLD and CRUDEOILM, and is its
    ``qty`` counted in lots or in the contract's units (grams / barrels)? The same position is
    priced at several quantities; the one whose margin matches one lot's notional tells the unit.
  - the account's free margin, to set against those figures.

It logs in with the stored credentials exactly as the live runner does (TOTP), makes only GET /
calculator calls (profile, RMS limit, LTP, margin batch) and writes a JSON report with no
credential, token or client code in it. It refuses to run while the connection holds a position.

    python3 scripts/angel_mcx_probe.py [conn_id]      -> state/angel_mcx_probe.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from live.env_loader import load_private_env  # noqa: E402

WANT = {"GOLDM": (1, 10, 100), "GOLD": (1, 100), "CRUDEOILM": (1, 10)}
ROLL_DAYS = 5


def _nearest(master: list, name: str) -> dict | None:
    rows = []
    for ins in master:
        if ins.get("exch_seg") != "MCX" or ins.get("name") != name or ins.get("instrumenttype") != "FUTCOM":
            continue
        try:
            exp = datetime.strptime(str(ins.get("expiry")), "%d%b%Y").date()
        except ValueError:
            continue
        if (exp - date.today()).days >= ROLL_DAYS:
            rows.append((exp, ins))
    rows.sort(key=lambda r: r[0])
    return rows[0][1] if rows else None


def _margin(smart, payload: dict) -> dict:
    if hasattr(smart, "getMarginApi"):
        return smart.getMarginApi(payload)
    return smart._postRequest("api.margin.api", payload)


def main() -> int:
    load_private_env(BASE_DIR)
    from config.labs_config import STATE_DIR
    from live import live_service as svc
    from live.brokers.angel import AngelAdapter
    from storage.live_db import get_live_conn

    out: dict = {"ran_at": datetime.now().isoformat(timespec="seconds"), "orders_placed": 0}
    dest = STATE_DIR / "angel_mcx_probe.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = get_live_conn()
        try:
            rows = conn.execute("SELECT user_id, conn_id FROM live_broker_connections WHERE broker='angel'").fetchall()
        finally:
            conn.close()
        want = sys.argv[1] if len(sys.argv) > 1 else None
        pick = [(r[0], r[1]) for r in rows if want is None or r[1] == want]
        if len(pick) != 1:
            raise RuntimeError(f"expected one Angel connection, found {len(pick)}")
        user_id, conn_id = pick[0]
        state = svc.get_trade_state(user_id, conn_id)
        if (state.get("position") or "NONE").upper() == "OPEN":
            raise RuntimeError("the connection holds a position - probe not run")
        adapter = AngelAdapter(user_id=user_id, conn_id=conn_id, creds=svc.load_credentials(user_id, conn_id))
        adapter.connect()
        smart = adapter._smart

        try:
            prof = (smart.getProfile(smart.refresh_token) or {}).get("data") or {}
            out["profile"] = {"exchanges": prof.get("exchanges"), "products": prof.get("products")}
        except Exception as e:
            out["profile_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        try:
            rms = (smart.rmsLimit() or {}).get("data") or {}
            out["funds"] = {k: rms.get(k) for k in ("net", "availablecash", "availableintradaypayin",
                                                    "availablelimitmargin", "collateral", "utiliseddebits",
                                                    "utilisedspan", "utilisedexposure") if k in rms}
        except Exception as e:
            out["funds_error"] = f"{type(e).__name__}: {str(e)[:120]}"

        master = adapter._ensure_instrument_master()
        out["contracts"] = {}
        for name, qtys in WANT.items():
            ins = _nearest(master, name)
            if not ins:
                out["contracts"][name] = {"error": "no contract in the instrument master"}
                continue
            row = {"symbol": ins.get("symbol"), "token": str(ins.get("token")), "expiry": ins.get("expiry"),
                   "lotsize": ins.get("lotsize"), "tick_size": ins.get("tick_size"), "margin": []}
            try:
                ltp = (smart.ltpData("MCX", ins.get("symbol"), str(ins.get("token"))) or {}).get("data") or {}
                row["ltp"] = ltp.get("ltp")
            except Exception as e:
                row["ltp_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            for product in ("INTRADAY", "CARRYFORWARD"):
                for qty in qtys:
                    payload = {"positions": [{"exchange": "MCX", "qty": qty, "price": 0, "productType": product,
                                              "token": str(ins.get("token")), "tradeType": "SELL",
                                              "orderType": "MARKET"}]}      # AB4033 without an order type
                    time.sleep(0.6)                                          # stay under Angel's rate limit
                    try:
                        resp = _margin(smart, payload) or {}
                        data = resp.get("data") or {}
                        row["margin"].append({"product": product, "qty": qty, "status": resp.get("status"),
                                              "message": resp.get("message"), "errorcode": resp.get("errorcode"),
                                              "total": data.get("totalMarginRequired"),
                                              "components": data.get("marginComponents")})
                    except Exception as e:
                        row["margin"].append({"product": product, "qty": qty,
                                              "error": f"{type(e).__name__}: {str(e)[:160]}"})
            out["contracts"][name] = row
        out["ok"] = True
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    dest.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print("probe written:", dest, "ok" if out.get("ok") else out.get("error"))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
