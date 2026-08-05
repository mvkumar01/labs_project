import sqlite3

from flask import Flask

from labs.engine import alpha_v212_tracker, paper_strategy_tracker
from labs.ui.routes import labs_bp


def _app(monkeypatch, conn):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(labs_bp)
    monkeypatch.setattr("storage.db.get_conn", lambda: conn)
    return app


def _insert_v211_day(conn, trade_date, net, charges):
    conn.execute(
        "INSERT INTO paper_strategy_daily "
        "(trade_date,status,tier,gap_dir,n_trades,pnl_pts,gross_rs,charges_rs,"
        "net_rs,strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_date,
            "traded",
            "PC50",
            "UP",
            1,
            10.0,
            net + charges,
            charges,
            net,
            "test",
            f"{trade_date}T15:30:00+05:30",
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
    _insert_v211_day(conn, "2026-07-29", -900.0, 90.0)
    _insert_v211_day(conn, "2026-07-30", 500.0, 50.0)
    conn.commit()

    client = _app(monkeypatch, conn).test_client()
    html = client.get(
        "/labs/live?tab=nifty&date_from=2026-07-30&date_to=2026-07-30"
    ).get_data(as_text=True)

    assert "2026-07-30" in html
    assert "2026-07-29" not in html
    assert "₹500" in html
    assert "₹50" in html


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
