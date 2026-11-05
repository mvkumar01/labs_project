"""The live page's order and trade tables name each row's strategy (Vipin, 2026-11-05)."""
from __future__ import annotations

from pathlib import Path

import pytest

from labs.ui import live_routes as lr
from live import live_service as svc
from tests.test_configure_strategy import CONN_ID, USER_ID, client  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("version,name", [
    ("proposer_nifty_scalper_v4", "Nifty Scalper V4"), ("proposer_dt35_v5", "Sensex V5"),
    ("proposer_dt35_v3", "Sensex V4"), ("hybrid_alpha_v28", "Alpha v2.11 (legacy)"),
    ("proposer_oi_unwind_sensex_v1", "OI Unwind SENSEX"), ("gold_cci_v2", "Gold CCI V2"),
    ("not_a_preset", "not_a_preset"), (None, ""),
])
def test_strategy_names(version, name):
    assert lr.strategy_name(version) == name


def test_status_names_the_strategy_of_each_order(client):  # noqa: F811
    svc.insert_order_ledger(f"{CONN_ID}|k1", user_id=USER_ID, conn_id=CONN_ID, trade_date="2026-11-05",
                            strategy_version="proposer_nifty_scalper_v4", bar_timestamp="2026-11-05T09:43",
                            action="ENTER", side="CALL", entry_rule="proposer_5class_strong_bull", intent_seq=0,
                            symbol="NIFTY09NOV2624300CE", qty=325, order_type="LIMIT", limit_price=100.0, dry_run=0)
    orders = client.get("/live/status").get_json()["last_orders"]
    assert [(o["strategy_version"], o["strategy_name"]) for o in orders] == [
        ("proposer_nifty_scalper_v4", "Nifty Scalper V4")]


def test_orders_table_has_a_strategy_column():
    page = (ROOT / "templates" / "live.html").read_text(encoding="utf-8")
    assert "<th>P&amp;L</th><th>Strategy</th><th>Mode</th>" in page
    js = (ROOT / "static" / "live.js").read_text(encoding="utf-8")
    assert "o.strategy_name" in js and "t.strategy_name" in js
