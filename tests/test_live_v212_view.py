from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_v212_live_view_is_not_frozen_at_june() -> None:
    routes = (ROOT / "labs" / "ui" / "routes.py").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "live_strategy.html").read_text(
        encoding="utf-8"
    )

    assert 'active_live_tab in {"alpha_v212", "alpha_v213"}' in routes
    assert 'f"FROM {overlay_prefix}_daily WHERE trade_date >= \'2026-06-01\' "' in routes
    assert "tab=alpha_v213" in template
    assert "trade_date < '2026-07-01'" not in routes
    assert "June coverage" not in template
    assert "June daily history" not in template
