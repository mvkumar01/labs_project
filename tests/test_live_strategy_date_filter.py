import sqlite3

from flask import Flask

from labs.engine import alpha_v212_tracker, paper_strategy_tracker
from labs.ui.routes import labs_bp


def _app(monkeypatch, conn):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(labs_bp)
    monkeypatch.setattr("storage.db.get_conn", lambda: conn)
    return app


def _insert_v211_day(conn, trade_date, net, charges, tier="PC50", n_trades=1):
    conn.execute(
        "INSERT INTO paper_strategy_daily "
        "(trade_date,status,tier,gap_dir,n_trades,pnl_pts,gross_rs,charges_rs,"
        "net_rs,strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_date,
            "traded",
            tier,
            "UP",
            n_trades,
            10.0,
            net + charges,
            charges,
            net,
            "test",
            f"{trade_date}T15:30:00+05:30",
        ),
    )


def _insert_v211_trade(conn, trade_date, seq, side, gross, charges):
    conn.execute(
        "INSERT INTO paper_strategy_trades "
        "(trade_date,seq,side,strike,entry_ts,exit_ts,entry_spot,exit_spot,"
        "entry_prem,exit_prem,pnl_pts,gross_rs,charges_rs,net_rs,entry_rule,"
        "exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_date,
            seq,
            side,
            25000,
            f"{trade_date}T09:20:00+05:30",
            f"{trade_date}T10:20:00+05:30",
            25000.0,
            25010.0,
            100.0,
            120.0,
            20.0,
            gross,
            charges,
            gross - charges,
            "test",
            "alpha_exit",
        ),
    )


def _insert_v212_day(conn, trade_date, net, charges):
    conn.execute(
        "INSERT INTO alpha_v212_daily "
        "(trade_date,status,tier,gap_dir,expiry_code,n_segments,priced_segments,"
        "unavailable_segments,spot_pnl_pts,gross_rs,charges_rs,net_rs,"
        "strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_date,
            "traded",
            "PC50",
            "UP",
            "26804",
            1,
            1,
            0,
            10.0,
            net + charges,
            charges,
            net,
            "test",
            f"{trade_date}T15:30:00+05:30",
        ),
    )


def test_v211_filter_recomputes_summary(monkeypatch):
    conn = sqlite3.connect(":memory:")
    paper_strategy_tracker._ensure_tables(conn)
    _insert_v211_day(conn, "2026-07-29", 1900.0, 100.0)
    _insert_v211_trade(conn, "2026-07-29", 1, "CALL", 2000.0, 100.0)
    _insert_v211_day(conn, "2026-07-30", 920.0, 80.0, tier="PC400", n_trades=2)
    _insert_v211_trade(conn, "2026-07-30", 1, "PUT", 700.0, 50.0)
    _insert_v211_trade(conn, "2026-07-30", 2, "PUT", 300.0, 30.0)
    conn.commit()

    client = _app(monkeypatch, conn).test_client()
    html = client.get(
        "/labs/live?tab=nifty&date_from=2026-07-30&date_to=2026-07-30"
    ).get_data(as_text=True)

    assert "2026-07-30" in html
    assert "2026-07-29" not in html
    assert "P&amp;L by tier and side" in html
    assert "PC400" in html
    assert "PUT" in html
    assert "₹1,000" in html
    assert "₹80" in html
    assert "₹920" in html
    assert "₹2,000" not in html


def test_v212_filter_recomputes_summary_and_latest_day(monkeypatch):
    conn = sqlite3.connect(":memory:")
    paper_strategy_tracker._ensure_tables(conn)
    alpha_v212_tracker._ensure_tables(conn)
    _insert_v212_day(conn, "2026-07-29", 1200.0, 120.0)
    _insert_v212_day(conn, "2026-07-30", -100.0, 50.0)
    conn.commit()

    client = _app(monkeypatch, conn).test_client()
    html = client.get(
        "/labs/live?tab=alpha_v212&date_from=2026-07-30&date_to=2026-07-30"
    ).get_data(as_text=True)

    assert "2026-07-30" in html
    assert "2026-07-29" not in html
    assert "₹-100.00" in html
    assert "after ₹50.00 charges" in html
