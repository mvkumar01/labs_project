from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_v212_live_view_is_not_frozen_at_june() -> None:
    routes = (ROOT / "labs" / "ui" / "routes.py").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "live_strategy.html").read_text(
        encoding="utf-8"
    )

    assert "FROM alpha_v212_daily WHERE trade_date >= '2026-06-01'" in routes
    assert "trade_date < '2026-07-01'" not in routes
    assert "June coverage" not in template
    assert "June daily history" not in template
