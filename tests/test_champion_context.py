from pathlib import Path

import pandas as pd
import pytest

from live.engine import champion_decider, champion_inputs, champion_sim


DATE = "2026-06-11"


def _prior_frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "timestamp": [
                "2026-06-10 09:15:00",
                "2026-06-10 15:29:00",
            ],
            "spot": [23233.95, 23214.95],
        }
    )
    frame.attrs["source_path"] = "archive/2026-06-10/NIFTY.parquet.zst"
    return frame


def test_previous_close_uses_prior_shared_session_not_sparse_vix(
    monkeypatch, tmp_path: Path
) -> None:
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    pd.DataFrame(
        [{"date": "2026-04-24", "nifty_close": 23897.95}]
    ).to_csv(analytics / "vix_history.csv", index=False)
    monkeypatch.setattr(champion_inputs, "ALPHA_DATA_DIR", tmp_path)
    monkeypatch.setattr(
        champion_inputs, "_previous_trading_days", lambda *_: ["2026-06-10"]
    )
    monkeypatch.setattr(
        champion_inputs, "load_options_frame", lambda *_args, **_kwargs: _prior_frame()
    )

    date, close, source = champion_inputs.previous_session_close(DATE)

    assert date == "2026-06-10"
    assert close == 23214.95
    assert source.endswith("NIFTY.parquet.zst")


def test_previous_close_fails_closed_when_shared_session_missing(
    monkeypatch, tmp_path: Path
) -> None:
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    pd.DataFrame(
        [{"date": "2026-04-24", "nifty_close": 23897.95}]
    ).to_csv(analytics / "vix_history.csv", index=False)
    monkeypatch.setattr(champion_inputs, "ALPHA_DATA_DIR", tmp_path)
    monkeypatch.setattr(
        champion_inputs, "_previous_trading_days", lambda *_: ["2026-06-10"]
    )

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("archive absent")

    monkeypatch.setattr(champion_inputs, "load_options_frame", missing)

    with pytest.raises(
        champion_inputs.ContextInputError, match="No preceding non-frozen"
    ):
        champion_inputs.previous_session_close(DATE)


def test_vix_fallback_requires_exact_trade_date(
    monkeypatch, tmp_path: Path
) -> None:
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    path = analytics / "vix_history.csv"
    pd.DataFrame(
        [{"date": "2026-04-24", "vix_open": 19.71}]
    ).to_csv(path, index=False)
    monkeypatch.setattr(champion_inputs, "ALPHA_DATA_DIR", tmp_path)

    assert champion_inputs.resolve_vix_open(DATE, None) == (
        None,
        "missing_exact_date",
    )

    pd.DataFrame(
        [
            {"date": "2026-04-24", "vix_open": 19.71},
            {"date": DATE, "vix_open": 15.63},
        ]
    ).to_csv(path, index=False)
    assert champion_inputs.resolve_vix_open(DATE, None) == (
        15.63,
        f"vix_history:{DATE}",
    )


def test_implausible_supplied_vix_fails_closed() -> None:
    with pytest.raises(champion_inputs.ContextInputError, match="Implausible VIX"):
        champion_inputs.resolve_vix_open(DATE, 23897.95)


def test_day_context_rejects_direction_disagreement(monkeypatch) -> None:
    monkeypatch.setattr(
        champion_inputs,
        "previous_session_close",
        lambda *_: ("2026-06-10", 23214.95, "shared_store"),
    )
    ohlc = champion_sim.OHLC(
        {"09:15": (23101.35, 23124.1, 23081.15, 23105.75)}
    )

    with pytest.raises(
        champion_inputs.ContextInputError, match="Gap direction mismatch"
    ):
        champion_inputs.resolve_day_context(
            DATE, ohlc, "UP", 15.63, vix_source="backfill_override"
        )


def test_day_context_records_exact_provenance(monkeypatch) -> None:
    monkeypatch.setattr(
        champion_inputs,
        "previous_session_close",
        lambda *_: ("2026-06-10", 23214.95, "shared_store"),
    )
    ohlc = champion_sim.OHLC(
        {"09:15": (23101.35, 23124.1, 23081.15, 23105.75)}
    )

    context = champion_inputs.resolve_day_context(
        DATE, ohlc, "DOWN", 15.63, vix_source="backfill_override"
    )

    assert context.previous_session_date == "2026-06-10"
    assert context.prev_close == 23214.95
    assert context.sgap == pytest.approx(-113.60)
    assert context.direction == "DOWN"
    assert context.vix_open == 15.63
    assert context.vix_source == "backfill_override"
    assert context.regime == "TRAIL"
    assert context.open_spot_source == "session_ohlc_09:15"


def test_day_context_accepts_complete_audited_previous_session_override(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        champion_inputs,
        "previous_session_close",
        lambda *_: pytest.fail("shared lookup must not run for audited override"),
    )
    ohlc = champion_sim.OHLC(
        {"09:15": (23615.90, 23640.0, 23590.0, 23610.0)}
    )

    context = champion_inputs.resolve_day_context(
        "2026-06-01",
        ohlc,
        "UP",
        16.18,
        vix_source="backfill_override",
        previous_session_date="2026-05-29",
        prev_close=23576.75,
        prev_close_source="audited_manifest:shared_market_2026-05-29",
    )

    assert context.previous_session_date == "2026-05-29"
    assert context.prev_close == 23576.75
    assert context.prev_close_source == "audited_manifest:shared_market_2026-05-29"
    assert context.sgap == pytest.approx(39.15)


def test_day_context_rejects_partial_previous_session_override() -> None:
    ohlc = champion_sim.OHLC(
        {"09:15": (23615.90, 23640.0, 23590.0, 23610.0)}
    )

    with pytest.raises(champion_inputs.ContextInputError, match="requires both"):
        champion_inputs.resolve_day_context(
            "2026-06-01",
            ohlc,
            "UP",
            16.18,
            previous_session_date="2026-05-29",
        )


def test_day_context_uses_verified_open_with_provenance(monkeypatch) -> None:
    monkeypatch.setattr(
        champion_inputs,
        "previous_session_close",
        lambda *_: ("2026-06-19", 24042.70, "audited_manifest"),
    )
    ohlc = champion_sim.OHLC(
        {"09:15": (24071.30, 24090.0, 24050.0, 24080.0)}
    )

    context = champion_inputs.resolve_day_context(
        "2026-06-23",
        ohlc,
        "UP",
        12.93,
        open_spot=24073.30,
        open_spot_source="kite_historical_verified:hybrid_range_writer_log",
    )

    assert context.open_spot == 24073.30
    assert context.open_spot_source == "kite_historical_verified:hybrid_range_writer_log"
    assert context.sgap == pytest.approx(30.60)


def test_live_reconcile_holds_when_context_is_unavailable() -> None:
    target = {
        "position": "UNAVAILABLE",
        "context_error": "previous session missing",
    }

    assert champion_decider.reconcile(target, "CALL") == {
        "action": "HOLD",
        "side": "CALL",
        "reason": "context_unavailable",
        "rule": None,
    }
