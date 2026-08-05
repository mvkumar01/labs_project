"""Render smoke test for the Baskets tab: the template must render with empty
state (pre-backfill) and with populated totals, and the route must whitelist
the tab. Catches Jinja syntax errors without needing labs.db."""
from pathlib import Path

from flask import Flask, render_template

ROOT = Path(__file__).resolve().parents[1]


def _render(**ctx):
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.add_url_rule(
        "/labs/live", endpoint="labs.live_strategy", view_func=lambda: ""
    )
    base = dict(
        active_live_tab="baskets",
        basket_defs={}, basket_totals={}, basket_by_date={},
        basket_pending=0, basket_error=None,
        rows=[], trades=[], stats={}, contract_variants={},
        comparison_variant_totals={}, comparison_by_date={},
        sensex_rows=[], sensex_trades=[], sensex_stats={},
        sensex_v211_rows=[], sensex_v211_trades=[], sensex_v211_stats={},
        overlay_rows=[], overlay_trades=[], overlay_stats={},
        overlay_version="v2.12",
        date_from=None, date_to=None,
    )
    base.update(ctx)
    with app.test_request_context():
        return render_template("live_strategy.html", **base)


def test_baskets_tab_renders_empty_state():
    html = _render()
    assert "v2.11 Baskets — signals as multi-leg structures" in html
    assert "No basket rows yet" in html


def test_baskets_tab_renders_populated():
    from labs.engine.basket_replay import BASKETS
    totals = {
        "CALL:long_synthetic": {
            "side": "CALL", "basket": "long_synthetic",
            "label": BASKETS["CALL"]["long_synthetic"]["label"],
            "net_total": 1234.5, "charges_total": 160.0, "trades": 4,
            "priced": 4, "unavailable": 0, "win_days": 2, "loss_days": 1,
            "best_day": 900.0, "worst_day": -150.0,
        },
    }
    html = _render(
        basket_defs=BASKETS,
        basket_totals=totals,
        basket_by_date={"2026-07-02": {"CALL:long_synthetic": 1234.5}},
        basket_pending=3,
    )
    assert "Long Synthetic" in html
    assert "Replay 3 pending day(s)" in html
    assert "2026-07-02" in html


def test_routes_whitelist_baskets_tab():
    routes = (ROOT / "labs" / "ui" / "routes.py").read_text(encoding="utf-8")
    assert '"baskets",' in routes
    assert "/api/baskets/refresh" in routes
