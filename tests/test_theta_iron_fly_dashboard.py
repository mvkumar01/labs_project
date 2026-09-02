import sqlite3

import pytest

pytest.importorskip("flask")
from flask import Flask

from labs.engine import paper_strategy_tracker, theta_iron_fly_tracker as tracker
from labs.ui.routes import labs_bp


def test_dashboard_date_filter_recomputes_iron_fly_summary(monkeypatch):
    conn = sqlite3.connect(":memory:")
    paper_strategy_tracker._ensure_tables(conn)
    tracker._ensure_tables(conn)
    columns = (
        "trade_date,status,expiry_code,atm_strike,lower_wing_strike,"
        "upper_wing_strike,entry_ts,exit_ts,entry_spot,lot_size,lots,qty,"
        "n_legs,priced_legs,capital_required_rs,net_credit_rs,target_rs,"
        "gross_rs,charges_rs,net_rs,return_on_capital_pct,exit_reason,"
        "margin_method,strategy_version,updated_at"
    )
    base = [
        "closed", "26804", 25000, 24600, 25400,
        "T09:20:00+05:30", "T15:00:00+05:30", 25000.0,
        tracker.LOT_SIZE, 1, tracker.QTY, 4, 4, 14000.0, 12000.0,
        2400.0, 1000.0, 200.0, 800.0, 5.7143, "TIME_1500",
        tracker.MARGIN_METHOD, tracker.STRATEGY_VERSION,
    ]
    for trade_date, net_rs in (("2026-08-31", 800.0), ("2026-09-01", -300.0)):
        row = list(base)
        row[5] = f"{trade_date}{row[5]}"
        row[6] = f"{trade_date}{row[6]}"
        row[16] = net_rs + row[17]
        row[18] = net_rs
        row[19] = 100 * net_rs / row[13]
        row.append(f"{trade_date}T15:01:00+05:30")
        conn.execute(
            f"INSERT INTO theta_iron_fly_daily ({columns}) "
            f"VALUES ({','.join('?' for _ in range(len(row) + 1))})",
            (trade_date, *row),
        )
    conn.commit()

    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(labs_bp)
    monkeypatch.setattr("storage.db.get_conn", lambda: conn)
    html = app.test_client().get(
        "/labs/live?tab=theta_iron_fly&date_from=2026-09-01&date_to=2026-09-01"
    ).get_data(as_text=True)

    assert "2026-09-01" in html
    assert "2026-08-31" not in html
    assert "₹-300.00" in html
    assert "0/1 completed" in html
