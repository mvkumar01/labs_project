import sqlite3
from datetime import datetime, timedelta

from labs.engine.paper_strategy_tracker import IST
from labs.services.paper_trade_alerts import emit_paper_trade_alerts


TODAY = "2026-08-13"
NOW = datetime(2026, 8, 13, 11, 0, tzinfo=IST)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE paper_strategy_trades (
            trade_date TEXT, seq INTEGER, side TEXT, strike INTEGER,
            entry_ts TEXT, exit_ts TEXT, entry_spot REAL, exit_spot REAL,
            entry_prem REAL, exit_prem REAL, net_rs REAL,
            entry_rule TEXT, exit_reason TEXT
        );
        CREATE TABLE alpha_v212_trades (
            trade_date TEXT, seq INTEGER, status TEXT, side TEXT, strike INTEGER,
            tradingsymbol TEXT, entry_ts TEXT, exit_ts TEXT,
            entry_spot REAL, exit_spot REAL, entry_ask REAL, exit_bid REAL,
            net_rs REAL, entry_rule TEXT, exit_reason TEXT
        );
        """
    )
    return conn


def test_v211_open_entry_then_exit_are_sent_once():
    conn = _conn()
    conn.execute(
        "INSERT INTO paper_strategy_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TODAY, 1, "CALL", 25000, f"{TODAY}T10:00:00+05:30",
         f"{TODAY}T10:05:00+05:30", 25200, 25210, 120, 125, 285,
         "R1", "holding"),
    )
    messages = []
    sender = lambda message: messages.append(message) or True

    assert emit_paper_trade_alerts(
        "v2.11", TODAY, connection=conn, sender=sender, now=NOW
    ) == 1
    assert "ENTRY" in messages[0]
    assert emit_paper_trade_alerts(
        "v2.11", TODAY, connection=conn, sender=sender, now=NOW
    ) == 0

    conn.execute(
        "UPDATE paper_strategy_trades SET exit_reason='ALPHA_STALL', "
        "exit_ts=?, exit_prem=?, net_rs=? WHERE trade_date=? AND seq=1",
        (f"{TODAY}T10:15:00+05:30", 130, 610, TODAY),
    )
    assert emit_paper_trade_alerts(
        "v2.11", TODAY, connection=conn, sender=sender, now=NOW
    ) == 1
    assert "EXIT" in messages[-1]
    assert "ALPHA_STALL" in messages[-1]


def test_v212_closed_trade_sends_entry_then_exit_and_is_idempotent():
    conn = _conn()
    conn.execute(
        "INSERT INTO alpha_v212_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TODAY, 1, "closed", "PUT", 25400, "NIFTY26AUG25400PE",
         f"{TODAY}T10:00:00+05:30", f"{TODAY}T10:01:00+05:30",
         25200, 25180, 140, 148, 470, "R2", "ENTRY_SPOT_STOP"),
    )
    messages = []
    sender = lambda message: messages.append(message) or True

    assert emit_paper_trade_alerts(
        "v2.12", TODAY, connection=conn, sender=sender, now=NOW
    ) == 2
    assert [message.splitlines()[0].endswith(kind) for message, kind in zip(
        messages, ("ENTRY", "EXIT")
    )] == [True, True]
    assert emit_paper_trade_alerts(
        "v2.12", TODAY, connection=conn, sender=sender, now=NOW
    ) == 0


def test_failed_send_is_released_for_next_pass():
    conn = _conn()
    conn.execute(
        "INSERT INTO paper_strategy_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TODAY, 1, "CALL", 25000, f"{TODAY}T10:00:00+05:30",
         f"{TODAY}T10:05:00+05:30", 25200, 25210, 120, 125, 285,
         "R1", "holding"),
    )
    assert emit_paper_trade_alerts(
        "v2.11", TODAY, connection=conn, sender=lambda _: False, now=NOW
    ) == 0
    assert emit_paper_trade_alerts(
        "v2.11", TODAY, connection=conn, sender=lambda _: True,
        now=NOW + timedelta(minutes=1)
    ) == 1


def test_historical_replay_never_alerts():
    conn = _conn()
    assert emit_paper_trade_alerts(
        "v2.11", "2026-08-12", connection=conn,
        sender=lambda _: True, now=NOW
    ) == 0
