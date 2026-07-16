from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_calibration_config_is_fail_closed_and_covers_every_index_interval():
    config = json.loads((ROOT / "config" / "calibration_center.json").read_text(encoding="utf-8"))
    assert config["operating_mode"] == "recommendation_only"
    assert config["automatic_promotion_enabled"] is False
    assert "minimum_sessions" in config["production_gates"]
    for symbol in ("NIFTY", "BANKNIFTY", "SENSEX"):
        for interval in ("5", "15"):
            assert config["meaningful_move"][symbol][interval]["enabled"] is False


def test_calibration_page_and_api_expose_version_history_and_controls():
    import app as labs_app

    client = labs_app.app.test_client()
    response = client.get("/labs/calibration?symbol=BANKNIFTY&interval=15")
    assert response.status_code == 200
    markup = response.get_data(as_text=True)
    assert "OI Market Read Calibration Center" in markup
    assert "BANKNIFTY_15M_v1" in markup
    assert "Recalculate Candidate" in markup
    assert "Shadow Test Again" in markup
    assert "Automatic Promotion" in markup

    payload = client.get("/labs/api/calibration?symbol=NIFTY&interval=5").get_json()
    assert payload["current"]["version"] == "NIFTY_5M_v1"
    assert payload["config"]["automatic_promotion_enabled"] is False


def test_approval_is_blocked_without_a_green_shadow_candidate(tmp_path, monkeypatch):
    from labs.services import calibration_service as service

    state_path = tmp_path / "state.json"
    monkeypatch.setattr(service, "STATE_PATH", state_path)
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "candidates": {"NIFTY:5": {"recommendation": "AMBER", "shadow_status": "NOT_RUN"}},
        "history": {"NIFTY:5": []},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="Approval blocked"):
        service.candidate_action("NIFTY", 5, "approve")


def test_calibration_navigation_is_present_on_main_labs_pages():
    for template in ("labs.html", "backtest.html", "live_strategy.html"):
        markup = (ROOT / "templates" / template).read_text(encoding="utf-8")
        assert 'href="/labs/calibration"' in markup


def test_outcome_metrics_use_strict_window_and_positive_adverse_magnitude():
    from labs.services.calibration_service import _outcomes

    payload = {
        "spot_series": [
            {"timestamp": "09:15", "spot": 100}, {"timestamp": "09:16", "spot": 102},
            {"timestamp": "09:17", "spot": 99}, {"timestamp": "09:18", "spot": 101},
            {"timestamp": "09:20", "spot": 103},
        ],
        "market_read_series": [
            {"timestamp": "2026-07-15T09:15:00", "state": "BULLISH"},
            {"timestamp": "2026-07-15T09:20:00", "state": "BEARISH"},
        ],
    }
    bullish, pending = _outcomes(payload, {"enabled": True, "points": 2})
    assert bullish["touch"] is True
    assert bullish["next"] is True
    assert bullish["meaningful"] is True
    assert bullish["mfe"] == 2
    assert bullish["mae"] == 1
    assert pending["pending"] is True


def test_shadow_then_approval_succeeds_only_when_all_gates_pass(tmp_path, monkeypatch):
    from labs.services import calibration_service as service

    state_path = tmp_path / "state.json"
    candidate = {
        "candidate_version": "NIFTY_5M_v2", "recommendation": "AMBER",
        "shadow_status": "NOT_RUN", "status": "CANDIDATE",
        "metrics": {
            "current": {"next_prediction_accuracy_pct": 50, "coverage_pct": 60},
            "candidate": {
                "sessions": 3, "completed_predictions": 25,
                "coverage_pct": 60, "balanced_accuracy_pct": 60,
                "holdout_accuracy_pct": 60, "walk_forward": {"pass_pct": 100},
                "bull_to_bear_flip_pct": 5, "bear_to_bull_flip_pct": 5,
                "median_signal_duration_minutes": 15, "one_strike_dominance_pct": 10,
                "average_mfe": 2, "average_mae": 1,
                "next_prediction_accuracy_pct": 60,
            },
        },
    }
    state_path.write_text(json.dumps({"schema_version": 1, "candidates": {"NIFTY:5": candidate}, "history": {"NIFTY:5": []}}), encoding="utf-8")
    config = {
        "shadow": {"minimum_sessions": 3, "minimum_completed_timestamps": 25},
        "production_gates": {
            "minimum_sessions": 3, "minimum_coverage_pct": 50,
            "minimum_balanced_next_accuracy_pct": 50, "minimum_holdout_accuracy_pct": 50,
            "minimum_walk_forward_pass_pct": 50, "maximum_flip_pct": 20,
            "minimum_median_signal_minutes": 10, "maximum_one_strike_dominance_pct": 20,
            "minimum_mfe_mae_ratio": 1,
        },
    }
    monkeypatch.setattr(service, "STATE_PATH", state_path)
    monkeypatch.setattr(service, "_config", lambda: config)
    monkeypatch.setattr(service, "_write_artifacts", lambda _candidate: None)
    monkeypatch.setattr(service, "_audit", lambda *_args, **_kwargs: None)

    shadowed = service.shadow_test("NIFTY", 5)
    assert shadowed["shadow_status"] == "PASS"
    assert shadowed["recommendation"] == "GREEN"
    approved = service.candidate_action("NIFTY", 5, "approve")
    assert approved["status"] == "APPROVED_PENDING_DEPLOYMENT"
