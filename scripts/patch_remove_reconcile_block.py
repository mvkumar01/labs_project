"""Remove the DB-vs-broker reconciliation block from the live bots (2026-10-28).

The live DB trade state is not kept current, so the "MISMATCH ... new entries blocked"
check fired on stale rows and left connections blocked until a restart. The broker book
is the truth: both runners now log a disagreement and carry on (an OPEN DB row with a
flat broker is cleared), reconcile_blocked is always written as "0", and arming / order
entry no longer refuse on it. The short-position guard in the NIFTY runner still skips
the cycle but no longer persists a block.

Usage: python scripts/patch_remove_reconcile_block.py [ROOT]   (idempotent)
"""
import sys
from pathlib import Path


def _rep(text, old, new, marker):
    if marker in text:
        return text
    assert text.count(old) == 1, f"anchor not unique/found: {old[:90]!r}"
    return text.replace(old, new)


def patch_live_runner(t):
    t = _rep(t, '''    broker_open = pos.qty > 0
    if not db_open and not broker_open:
        ok, msg = True, "both flat"
    elif (db_open and broker_open and pos.symbol == db_symbol
          and pos.qty == abs(db_qty)):
        ok, msg = True, "both open — agree (broker truth adopted)"
    else:
        ok = False
        msg = (f"MISMATCH db={db_symbol}/{db_qty} "
               f"broker={pos.symbol}/{pos.qty} — new entries blocked")

    svc.set_config(user_id, conn_id, "reconcile_blocked", "0" if ok else "1", conn)
    svc.set_config(user_id, conn_id, "reconcile_message", "" if ok else msg, conn)
    return ReconcileResult(ok, db_symbol, db_qty, pos.symbol, pos.qty, msg)''', '''    # 2026-10-28: a DB/broker disagreement no longer blocks entries - the DB trade
    # state is not kept current, so the block fired on stale rows. The broker book
    # is the truth: an OPEN DB row with a flat broker is cleared; anything else is
    # only logged.
    broker_open = pos.qty > 0
    if not db_open and not broker_open:
        msg = "both flat"
    elif (db_open and broker_open and pos.symbol == db_symbol
          and pos.qty == abs(db_qty)):
        msg = "both open — agree (broker truth adopted)"
    else:
        msg = (f"db={db_symbol}/{db_qty} broker={pos.symbol}/{pos.qty} "
               "differ — broker book used")
        if db_open and not broker_open:
            svc.reset_trade_state(user_id, conn_id, conn)
        log.warning("reconcile conn=%s %s", conn_id, msg)

    svc.set_config(user_id, conn_id, "reconcile_blocked", "0", conn)
    svc.set_config(user_id, conn_id, "reconcile_message", "", conn)
    return ReconcileResult(True, db_symbol, db_qty, pos.symbol, pos.qty, msg)''', "differ — broker book used")
    # broker read failure at startup: log, don't block
    t = _rep(t, '''        msg = f"broker position read failed: {type(e).__name__}"
        svc.set_config(user_id, conn_id, "reconcile_blocked", "1", conn)
        svc.set_config(user_id, conn_id, "reconcile_message", msg, conn)
        return ReconcileResult(False, db_symbol, db_qty, None, 0, msg)''', '''        msg = f"broker position read failed: {type(e).__name__}"
        log.warning("reconcile conn=%s %s", conn_id, msg)
        svc.set_config(user_id, conn_id, "reconcile_blocked", "0", conn)
        svc.set_config(user_id, conn_id, "reconcile_message", "", conn)
        return ReconcileResult(True, db_symbol, db_qty, None, 0, msg)''', 'log.warning("reconcile conn=%s %s", conn_id, msg)\n        svc.set_config(user_id, conn_id, "reconcile_blocked", "0", conn)')
    # per-cycle short guard: skip the cycle, no persisted block
    t = _rep(t, '''                f"broker={broker_symbol}/{pos.qty}; short position detected, automation blocked"
            )
            log.warning("live state mismatch | conn=%s %s", conn_id, msg)
            prior_msg = svc.get_config(user_id, conn_id, "reconcile_message")
            svc.set_config(user_id, conn_id, "reconcile_blocked", "1")
            svc.set_config(user_id, conn_id, "reconcile_message", msg)
            if prior_msg != msg:
                notify_telegram(f"LIVE automation blocked: {msg}")
            return''', '''                f"broker={broker_symbol}/{pos.qty}; short position detected, cycle skipped"
            )
            log.warning("live state mismatch | conn=%s %s", conn_id, msg)
            return''', "short position detected, cycle skipped")
    # per-cycle symbol/qty disagreement: use the broker book, carry on
    t = _rep(t, '''        elif broker_symbol != db_symbol or int(pos.qty or 0) != abs(db_qty):
            msg = (
                f"MISMATCH db={db_symbol}/{db_qty} "
                f"broker={broker_symbol}/{pos.qty}; automation blocked"
            )
            log.warning("live state mismatch | conn=%s %s", conn_id, msg)
            prior_msg = svc.get_config(user_id, conn_id, "reconcile_message")
            svc.set_config(user_id, conn_id, "reconcile_blocked", "1")
            svc.set_config(user_id, conn_id, "reconcile_message", msg)
            if prior_msg != msg:
                notify_telegram(f"LIVE automation blocked: {msg}")
            return''', '''        elif broker_symbol != db_symbol or int(pos.qty or 0) != abs(db_qty):
            # The DB row is stale; the broker book drives current_* below.
            log.warning(
                "live state differs (broker book used) | conn=%s db=%s/%s broker=%s/%s",
                conn_id, db_symbol, db_qty, broker_symbol, pos.qty,
            )''', "live state differs (broker book used)")
    return t


def patch_proposer_runner(t):
    return _rep(t, '''        else:
            ok = False
            msg = (f"MISMATCH db={st.get('symbol')}/{st.get('qty') if db_open else 0} "
                   f"broker={pos.symbol}/{pos.qty} - new entries blocked")
    svc.set_config(user_id, conn_id, "reconcile_blocked", "0" if ok else "1")
    svc.set_config(user_id, conn_id, "reconcile_message", "" if ok else msg)
    if not ok:
        notify_telegram(f"Proposer automation blocked: {msg}")
    return ok''', '''        else:
            ok = False
            msg = (f"db={st.get('symbol')}/{st.get('qty') if db_open else 0} "
                   f"broker={pos.symbol}/{pos.qty} differ")
    # 2026-10-28: logged only - the DB trade state is not kept current, so a
    # disagreement no longer blocks new entries.
    if not ok:
        log.warning("reconcile conn=%s %s", conn_id, msg)
    svc.set_config(user_id, conn_id, "reconcile_blocked", "0")
    svc.set_config(user_id, conn_id, "reconcile_message", "")
    return True''', "logged only - the DB trade state is not kept current")


def patch_control_plane(t):
    for old in ('''    if svc.get_config_int(user_id, conn_id, "reconcile_blocked", c):
        raise ControlError("Resolve the broker reconciliation mismatch first")
''', '''        if operation == "entry" and svc.get_config_int(
            user_id, conn_id, "reconcile_blocked", c
        ):
            raise ControlError("Resolve the broker reconciliation mismatch first")
'''):
        assert t.count(old) <= 1
        t = t.replace(old, "")
    return t


def patch_live_js(t):
    return _rep(t, '''          if (!s.reconcile_ok) {''', '''          if (false && !s.reconcile_ok) {  // reconcile banner retired 2026-10-28''',
                "reconcile banner retired 2026-10-28")


FILES = (("live/live_runner.py", patch_live_runner), ("live/proposer_runner.py", patch_proposer_runner),
         ("live/control_plane.py", patch_control_plane), ("static/live.js", patch_live_js))


def main(root):
    for rel, fn in FILES:
        p = root / rel
        raw = p.read_bytes().decode("utf-8")
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        out = fn(text)
        if crlf:
            out = out.replace("\n", "\r\n")
        if out != raw:
            p.write_bytes(out.encode("utf-8"))
            print("patched", rel)
        else:
            print("unchanged", rel)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
