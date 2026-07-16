"""Governed OI Market Read calibration and version workflow for Labs.

The production classifier is imported from the sibling alphaIMB checkout. This
module owns only research evaluation, candidate artifacts, approval state and
audit history; automatic promotion is fail-closed by configuration.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import csv
import json
import math
from pathlib import Path
import statistics
import sys
import uuid

import pandas as pd

from config.labs_config import BASE_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR


CONFIG_PATH = BASE_DIR / "config" / "calibration_center.json"
STATE_PATH = BASE_DIR / "storage" / "calibration_center_state.json"
AUDIT_PATH = BASE_DIR / "storage" / "calibration_center_audit.jsonl"
REPORT_ROOT = BASE_DIR / "storage" / "calibration_reports"
SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")
INTERVALS = (5, 15)
THRESHOLD_FIELDS = (
    "min_abs_delta_oi", "min_pct_delta_oi", "min_abs_premium_change",
    "min_pct_premium_change", "max_premium_age_minutes", "min_valid_strikes",
    "min_previous_oi", "min_previous_premium", "neutral_band",
    "max_activity_weight", "active_top_n_per_side",
)


def _alpha_root() -> Path:
    candidates = (
        BASE_DIR.parent / "alphaIMB",
        BASE_DIR.parent.parent,
    )
    for candidate in candidates:
        if (candidate / "oi_market_read.py").is_file():
            return candidate
    raise FileNotFoundError("Sibling alphaIMB checkout was not found")


def _alpha_modules():
    root = _alpha_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from oi_market_read import (  # pylint: disable=import-outside-toplevel
        MarketReadThresholds, build_market_read_payload, load_market_read_version,
    )
    return root, MarketReadThresholds, build_market_read_payload, load_market_read_version


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _audit(action: str, symbol: str, interval: int, detail: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": datetime.now(timezone.utc).isoformat(), "action": action,
        "symbol": symbol, "interval_minutes": interval, "detail": detail,
    }
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _key(symbol: str, interval: int) -> str:
    symbol = symbol.upper()
    if symbol not in SYMBOLS or interval not in INTERVALS:
        raise ValueError("Calibration requires NIFTY/BANKNIFTY/SENSEX and 5/15 minutes")
    return f"{symbol}:{interval}"


def _config() -> dict:
    return _read_json(CONFIG_PATH, {})


def _state() -> dict:
    return _read_json(STATE_PATH, {"schema_version": 1, "candidates": {}, "history": {}})


def _version_context(symbol: str, interval: int) -> tuple[dict, list[dict]]:
    root, _, _, load_version = _alpha_modules()
    registry_path = root / "config" / "oi_market_read_versions.json"
    current = load_version(registry_path, symbol, interval)
    registry = _read_json(registry_path, {})
    history = registry.get("versions", {}).get(symbol, {}).get(str(interval), [])
    return current, history


def _session_paths(symbol: str) -> list[tuple[str, Path]]:
    found: dict[str, Path] = {}
    live_dir, archive_dir = SHARED_LIVE_DIR, SHARED_ARCHIVE_DIR
    if not live_dir.exists() and not archive_dir.exists():
        shared_root = _alpha_root().parent / "shared_market_data"
        live_dir, archive_dir = shared_root / "live", shared_root / "archive"
    if archive_dir.exists():
        for day in archive_dir.iterdir():
            if not day.is_dir():
                continue
            for suffix in (".parquet.zst", ".parquet.gz", ".parquet"):
                path = day / f"{symbol}_options_1min{suffix}"
                if path.is_file():
                    found[day.name] = path
                    break
    if live_dir.exists():
        for day in live_dir.iterdir():
            path = day / f"{symbol}_options_1min.csv"
            if path.is_file():
                found[day.name] = path
    return sorted(found.items())


def _read_session(path: Path) -> pd.DataFrame:
    columns = ["timestamp", "strike", "option_type", "expiry", "oi", "ltp", "spot"]
    if ".parquet" in "".join(path.suffixes).lower():
        return pd.read_parquet(path, columns=columns)
    return pd.read_csv(path, usecols=columns)


def _session_range(root: Path, symbol: str, frame: pd.DataFrame) -> tuple[float, float, str]:
    range_file = root / "config" / f"{symbol.lower()}_ranges.txt"
    if symbol == "NIFTY":
        range_file = root / "config" / "nifty_ranges.txt"
    try:
        configured = [float(value) for value in range_file.read_text(encoding="utf-8").strip().split(",")[:2]]
        half_width = abs(configured[1] - configured[0]) / 2
    except (OSError, ValueError, IndexError):
        half_width = {"NIFTY": 400.0, "BANKNIFTY": 700.0, "SENSEX": 800.0}[symbol]
    opening = frame.copy()
    opening["timestamp"] = pd.to_datetime(opening["timestamp"], errors="coerce")
    first = opening["timestamp"].min()
    spot = float(pd.to_numeric(opening.loc[opening["timestamp"] == first, "spot"], errors="coerce").median())
    step = 50 if symbol == "NIFTY" else 100
    lower = round((spot - half_width) / step) * step
    upper = round((spot + half_width) / step) * step
    return float(lower), float(upper), "reconstructed configured-width around session opening spot"


def _rate(records: list[dict], field: str, state: str | None = None) -> float | None:
    eligible = [r for r in records if not r["pending"] and (state is None or r["state"] == state)]
    return None if not eligible else 100.0 * sum(r[field] for r in eligible) / len(eligible)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _outcomes(payload: dict, meaningful: dict) -> list[dict]:
    spots = {row["timestamp"]: float(row["spot"]) for row in payload.get("spot_series", [])}
    labels = list(spots)
    positions = {label: index for index, label in enumerate(labels)}
    reads = payload.get("market_read_series", [])
    enabled = meaningful.get("enabled") is True and meaningful.get("points") is not None
    points = float(meaningful.get("points") or 0)
    rows = []
    for index, read in enumerate(reads):
        state = read.get("state")
        if state not in {"BULLISH", "BEARISH"}:
            continue
        time = str(read.get("timestamp", ""))[11:16]
        next_time = str(reads[index + 1].get("timestamp", ""))[11:16] if index + 1 < len(reads) else ""
        start, end = positions.get(time), positions.get(next_time)
        record = {"state": state, "pending": True, "touch": False, "next": False, "meaningful": None, "mfe": None, "mae": None}
        if start is not None and end is not None and end > start:
            prediction = spots[time]
            window = [spots[label] for label in labels[start + 1:end]]
            next_spot = spots[next_time]
            if window:
                high, low = max(window), min(window)
                if state == "BULLISH":
                    record.update(touch=high > prediction, next=next_spot > prediction,
                                  meaningful=(high >= prediction + points) if enabled else None,
                                  mfe=high - prediction, mae=max(0.0, prediction - low))
                else:
                    record.update(touch=low < prediction, next=next_spot < prediction,
                                  meaningful=(low <= prediction - points) if enabled else None,
                                  mfe=prediction - low, mae=max(0.0, high - prediction))
                record["pending"] = False
        rows.append(record)
    return rows


def _signal_metrics(reads: list[dict], interval: int) -> tuple[float | None, float, float]:
    states = [read.get("state") for read in reads if read.get("state") != "INSUFFICIENT DATA"]
    durations, run = [], 0
    previous = None
    bull_bear = bear_bull = directional_pairs = 0
    for state in states:
        if state == previous:
            run += 1
        else:
            if run:
                durations.append(run * interval)
            run = 1
        if previous in {"BULLISH", "BEARISH"} and state in {"BULLISH", "BEARISH"}:
            directional_pairs += 1
            bull_bear += previous == "BULLISH" and state == "BEARISH"
            bear_bull += previous == "BEARISH" and state == "BULLISH"
        previous = state
    if run:
        durations.append(run * interval)
    denominator = directional_pairs or 1
    return _median(durations), 100.0 * bull_bear / denominator, 100.0 * bear_bull / denominator


def _evaluate(symbol: str, interval: int, thresholds: dict, sessions: list[tuple[str, Path]], meaningful: dict) -> dict:
    root, Thresholds, build_payload, _ = _alpha_modules()
    threshold_object = Thresholds(**thresholds)
    outcomes, all_reads, trends = [], [], []
    dominance_hits = dominance_total = 0
    used = 0
    for trade_date, path in sessions:
        try:
            frame = _read_session(path)
            lower, upper, range_source = _session_range(root, symbol, frame)
            payload = build_payload(
                frame, symbol=symbol, trade_date=trade_date, lower=lower, upper=upper,
                thresholds=threshold_object, interval_minutes=interval,
                now_ist=pd.Timestamp(f"{trade_date} 16:00", tz="Asia/Kolkata"),
            )
        except Exception:
            continue
        reads = payload.get("market_read_series", [])
        if len(reads) < 2:
            continue
        used += 1
        session_outcomes = _outcomes(payload, meaningful)
        outcomes.extend(session_outcomes)
        all_reads.extend(reads)
        for read in reads:
            contributors = read.get("top_contributors") or []
            weights = [abs(float(item.get("activity_weight") or 0)) for item in contributors]
            if weights and sum(weights) > 0:
                dominance_total += 1
                dominance_hits += max(weights) / sum(weights) > 0.5
        valid = [read for read in reads[1:] if read.get("state") != "INSUFFICIENT DATA"]
        directional = [record for record in session_outcomes if not record["pending"]]
        trends.append({
            "date": trade_date,
            "touch_accuracy": _rate(directional, "touch"),
            "next_accuracy": _rate(directional, "next"),
            "coverage": 100.0 * len(valid) / max(1, len(reads) - 1),
            "flat_pct": 100.0 * sum(read.get("state") == "FLAT / MIXED" for read in reads[1:]) / max(1, len(reads) - 1),
            "flip_pct": sum(_signal_metrics(reads, interval)[1:]),
            "bull_bear_ratio": sum(read.get("state") == "BULLISH" for read in reads) / max(1, sum(read.get("state") == "BEARISH" for read in reads)),
            "average_mfe": _mean([record["mfe"] for record in directional if record["mfe"] is not None]),
            "average_mae": _mean([record["mae"] for record in directional if record["mae"] is not None]),
            "range_source": range_source,
        })
    total_reads = len(all_reads)
    valid_reads = [read for read in all_reads if read.get("state") != "INSUFFICIENT DATA"]
    directional = [record for record in outcomes if not record["pending"]]
    bullish_next = _rate(directional, "next", "BULLISH")
    bearish_next = _rate(directional, "next", "BEARISH")
    balanced = _mean([value for value in (bullish_next, bearish_next) if value is not None])
    median_duration, bull_bear_flip, bear_bull_flip = _signal_metrics(all_reads, interval)
    mfe = [record["mfe"] for record in directional if record["mfe"] is not None]
    mae = [record["mae"] for record in directional if record["mae"] is not None]
    return {
        "directional_touch_accuracy_pct": _rate(directional, "touch"),
        "next_prediction_accuracy_pct": _rate(directional, "next"),
        "meaningful_move_accuracy_pct": _rate([r for r in directional if r["meaningful"] is not None], "meaningful") if meaningful.get("enabled") else None,
        "bullish_accuracy_pct": bullish_next,
        "bearish_accuracy_pct": bearish_next,
        "balanced_accuracy_pct": balanced,
        "coverage_pct": 100.0 * len(valid_reads) / max(1, total_reads),
        "flat_pct": 100.0 * sum(read.get("state") == "FLAT / MIXED" for read in all_reads) / max(1, total_reads),
        "insufficient_data_pct": 100.0 * sum(read.get("state") == "INSUFFICIENT DATA" for read in all_reads) / max(1, total_reads),
        "median_signal_duration_minutes": median_duration,
        "bull_to_bear_flip_pct": bull_bear_flip,
        "bear_to_bull_flip_pct": bear_bull_flip,
        "average_mfe": _mean(mfe), "median_mfe": _median(mfe),
        "average_mae": _mean(mae), "median_mae": _median(mae),
        "mfe_beats_mae_pct": 100.0 * sum(r["mfe"] > r["mae"] for r in directional) / max(1, len(directional)),
        "one_strike_dominance_pct": 100.0 * dominance_hits / max(1, dominance_total),
        "sessions": used, "completed_predictions": len(directional),
        "trends": trends,
    }


def _walk_forward(current: dict, candidate: dict, config: dict) -> dict:
    current_by_date = {row["date"]: row for row in current.get("trends", [])}
    paired = [(current_by_date[row["date"]], row) for row in candidate.get("trends", []) if row["date"] in current_by_date]
    windows = max(1, min(int(config.get("walk_forward_windows", 5)), len(paired)))
    chunks = [paired[index::windows] for index in range(windows)] if paired else []
    results = []
    for index, chunk in enumerate(chunks, 1):
        current_accuracy = _mean([pair[0].get("next_accuracy") for pair in chunk if pair[0].get("next_accuracy") is not None])
        candidate_accuracy = _mean([pair[1].get("next_accuracy") for pair in chunk if pair[1].get("next_accuracy") is not None])
        passed = current_accuracy is not None and candidate_accuracy is not None and candidate_accuracy >= current_accuracy
        results.append({"window": index, "current": current_accuracy, "candidate": candidate_accuracy, "passed": passed})
    return {"windows": results, "pass_pct": 100.0 * sum(row["passed"] for row in results) / max(1, len(results))}


def _recommend(current: dict, candidate: dict, config: dict, shadow_status: str) -> tuple[str, int, list[str], dict]:
    gates = config.get("production_gates", {})
    flip = (candidate.get("bull_to_bear_flip_pct") or 0) + (candidate.get("bear_to_bull_flip_pct") or 0)
    mfe_mae = (candidate.get("average_mfe") or 0) / max(candidate.get("average_mae") or 0, 1e-9)
    checks = {
        "minimum_sessions": candidate.get("sessions", 0) >= gates.get("minimum_sessions", 0),
        "coverage": (candidate.get("coverage_pct") or 0) >= gates.get("minimum_coverage_pct", 0),
        "balanced_accuracy": (candidate.get("balanced_accuracy_pct") or 0) >= gates.get("minimum_balanced_next_accuracy_pct", 0),
        "holdout_accuracy": (candidate.get("holdout_accuracy_pct") or 0) >= gates.get("minimum_holdout_accuracy_pct", 0),
        "walk_forward": (candidate.get("walk_forward", {}).get("pass_pct") or 0) >= gates.get("minimum_walk_forward_pass_pct", 0),
        "flip_frequency": flip <= gates.get("maximum_flip_pct", 100),
        "signal_duration": (candidate.get("median_signal_duration_minutes") or 0) >= gates.get("minimum_median_signal_minutes", 0),
        "one_strike_dominance": (candidate.get("one_strike_dominance_pct") or 100) <= gates.get("maximum_one_strike_dominance_pct", 100),
        "mfe_mae": mfe_mae >= gates.get("minimum_mfe_mae_ratio", 0),
    }
    failed = [name.replace("_", " ") for name, passed in checks.items() if not passed]
    explanations = []
    for label, field in (("Next Prediction Accuracy", "next_prediction_accuracy_pct"), ("Coverage", "coverage_pct")):
        delta = (candidate.get(field) or 0) - (current.get(field) or 0)
        explanations.append(f"{label} {'improved' if delta >= 0 else 'decreased'} by {abs(delta):.1f} percentage points.")
    explanations.append(f"Walk-forward improvement passed {candidate.get('walk_forward', {}).get('pass_pct', 0):.0f}% of windows.")
    if failed:
        explanations.append("Production gates not met: " + ", ".join(failed) + ".")
        status = "RED" if len(failed) >= 2 else "AMBER"
    elif shadow_status != "PASS":
        status = "AMBER"
        explanations.append("Analytical gates pass, but shadow confirmation is still required.")
    else:
        status = "GREEN"
        explanations.append("All configured analytical gates and shadow checks pass.")
    confidence = round(100.0 * sum(checks.values()) / max(1, len(checks)))
    return status, confidence, explanations, checks


def _next_version(current_version: str) -> str:
    prefix, _, raw = current_version.rpartition("_v")
    return f"{prefix}_v{int(raw or 0) + 1}"


def _write_artifacts(candidate: dict) -> None:
    directory = REPORT_ROOT / candidate["symbol"] / f"{candidate['interval_minutes']}m" / candidate["candidate_version"]
    directory.mkdir(parents=True, exist_ok=True)
    comparison = candidate["comparison"]
    with (directory / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("metric", "current", "candidate", "difference"))
        writer.writeheader(); writer.writerows(comparison)
    (directory / "candidate.json").write_text(json.dumps(candidate, indent=2), encoding="utf-8")
    report = [
        f"# {candidate['candidate_version']} calibration report", "",
        f"- Recommendation: **{candidate['recommendation']}**",
        f"- Confidence: **{candidate['confidence']}%**",
        f"- Dataset: **{candidate['metrics']['candidate']['sessions']} sessions**",
        f"- Shadow: **{candidate['shadow_status']}**", "", "## Explanation", "",
        *[f"- {line}" for line in candidate["explanation"]], "", "## Threshold changes", "",
    ]
    for field in THRESHOLD_FIELDS:
        before = candidate["current_thresholds"].get(field)
        after = candidate["candidate_thresholds"].get(field)
        if before != after:
            report.append(f"- `{field}`: {before} → {after}")
    (directory / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def recalculate(symbol: str, interval: int, candidate_thresholds: dict | None = None) -> dict:
    key = _key(symbol, interval); symbol = symbol.upper()
    config = _config(); current_version, history = _version_context(symbol, interval)
    current_thresholds = dict(current_version["thresholds"])
    proposed = dict(current_thresholds)
    if candidate_thresholds:
        for field in THRESHOLD_FIELDS:
            if field in candidate_thresholds and candidate_thresholds[field] not in (None, ""):
                value = float(candidate_thresholds[field])
                proposed[field] = int(value) if field in {"min_valid_strikes", "active_top_n_per_side"} else value
    paths = _session_paths(symbol)[-int(config.get("max_sessions", 60)):]
    meaningful = config.get("meaningful_move", {}).get(symbol, {}).get(str(interval), {})
    current_metrics = _evaluate(symbol, interval, current_thresholds, paths, meaningful)
    candidate_metrics = (
        copy.deepcopy(current_metrics)
        if proposed == current_thresholds
        else _evaluate(symbol, interval, proposed, paths, meaningful)
    )
    candidate_metrics["walk_forward"] = _walk_forward(current_metrics, candidate_metrics, config)
    holdout_sessions = max(1, math.ceil(candidate_metrics.get("sessions", 0) * float(config.get("holdout_fraction", 0.2))))
    holdout_trends = candidate_metrics.get("trends", [])[-holdout_sessions:]
    candidate_metrics["holdout_accuracy_pct"] = _mean([row.get("next_accuracy") for row in holdout_trends if row.get("next_accuracy") is not None])
    state = _state(); prior = state["candidates"].get(key, {})
    recommendation, confidence, explanation, checks = _recommend(current_metrics, candidate_metrics, config, prior.get("shadow_status", "NOT_RUN"))
    metric_fields = [field for field in candidate_metrics if field != "trends" and not isinstance(candidate_metrics[field], (dict, list))]
    comparison = [{"metric": field, "current": current_metrics.get(field), "candidate": candidate_metrics.get(field),
                   "difference": None if current_metrics.get(field) is None or candidate_metrics.get(field) is None else candidate_metrics[field] - current_metrics[field]}
                  for field in metric_fields]
    candidate = {
        "candidate_id": uuid.uuid4().hex, "symbol": symbol, "interval_minutes": interval,
        "current_version": current_version["version"], "candidate_version": _next_version(current_version["version"]),
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "CANDIDATE",
        "commit_hash": "pending", "deployment_status": "not_deployed",
        "rollback_version": current_version.get("rollback_version"),
        "recommendation": recommendation, "confidence": confidence,
        "shadow_status": prior.get("shadow_status", "NOT_RUN"), "walk_forward_status": "PASS" if candidate_metrics["walk_forward"]["pass_pct"] >= config["production_gates"]["minimum_walk_forward_pass_pct"] else "FAIL",
        "current_thresholds": current_thresholds, "candidate_thresholds": proposed,
        "metrics": {"current": current_metrics, "candidate": candidate_metrics},
        "comparison": comparison, "explanation": explanation, "gate_checks": checks,
        "dataset": {"first_date": paths[0][0] if paths else None, "last_date": paths[-1][0] if paths else None, "sessions_discovered": len(paths), "sessions_used": candidate_metrics["sessions"]},
        "meaningful_move": meaningful, "operating_mode": config.get("operating_mode"),
        "automatic_promotion_enabled": bool(config.get("automatic_promotion_enabled", False)),
    }
    state["candidates"][key] = candidate
    state["history"].setdefault(key, []).append({"candidate_version": candidate["candidate_version"], "at": candidate["created_at"], "action": "recalculated", "recommendation": recommendation})
    _atomic_json(STATE_PATH, state); _write_artifacts(candidate)
    _audit("recalculate", symbol, interval, {"candidate_version": candidate["candidate_version"], "recommendation": recommendation})
    return candidate


def shadow_test(symbol: str, interval: int) -> dict:
    key = _key(symbol, interval); state = _state(); candidate = state["candidates"].get(key)
    if not candidate:
        raise ValueError("Recalculate a candidate before shadow testing")
    config = _config()["shadow"]
    metrics = candidate["metrics"]["candidate"]
    passed = metrics.get("sessions", 0) >= config["minimum_sessions"] and metrics.get("completed_predictions", 0) >= config["minimum_completed_timestamps"]
    candidate["shadow_status"] = "PASS" if passed else "FAIL"
    recommendation, confidence, explanation, checks = _recommend(candidate["metrics"]["current"], metrics, _config(), candidate["shadow_status"])
    candidate.update(recommendation=recommendation, confidence=confidence, explanation=explanation, gate_checks=checks)
    state["history"].setdefault(key, []).append({"candidate_version": candidate["candidate_version"], "at": datetime.now(timezone.utc).isoformat(), "action": "shadow", "status": candidate["shadow_status"]})
    _atomic_json(STATE_PATH, state); _write_artifacts(candidate)
    _audit("shadow", symbol.upper(), interval, {"status": candidate["shadow_status"]})
    return candidate


def candidate_action(symbol: str, interval: int, action: str) -> dict:
    key = _key(symbol, interval); state = _state(); candidate = state["candidates"].get(key)
    if not candidate:
        raise ValueError("No candidate exists")
    if action == "approve":
        if candidate.get("recommendation") != "GREEN" or candidate.get("shadow_status") != "PASS":
            raise ValueError("Approval blocked: candidate must be GREEN with a passing shadow test")
        candidate["status"] = "APPROVED_PENDING_DEPLOYMENT"
    elif action == "reject":
        candidate["status"] = "REJECTED"
    elif action == "rollback":
        _, versions = _version_context(symbol.upper(), interval)
        if len(versions) < 2:
            raise ValueError("Rollback blocked: no prior deployed threshold version exists")
        candidate["status"] = "ROLLBACK_REQUESTED"
    else:
        raise ValueError("Unsupported calibration action")
    candidate["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["history"].setdefault(key, []).append({"candidate_version": candidate["candidate_version"], "at": candidate["updated_at"], "action": action, "status": candidate["status"]})
    _atomic_json(STATE_PATH, state); _write_artifacts(candidate)
    _audit(action, symbol.upper(), interval, {"status": candidate["status"]})
    return candidate


def calibration_context(symbol: str, interval: int) -> dict:
    key = _key(symbol, interval); current, versions = _version_context(symbol.upper(), interval)
    state = _state(); candidate = state["candidates"].get(key)
    return {
        "symbol": symbol.upper(), "interval_minutes": interval,
        "current": current, "candidate": candidate,
        "versions": list(reversed(versions)), "workflow_history": list(reversed(state["history"].get(key, []))),
        "config": _config(), "threshold_fields": THRESHOLD_FIELDS,
    }


def artifact_path(symbol: str, interval: int, artifact: str) -> Path:
    candidate = _state().get("candidates", {}).get(_key(symbol, interval))
    if not candidate:
        raise FileNotFoundError("No candidate artifact exists")
    names = {"markdown": "REPORT.md", "csv": "comparison.csv", "calibration": "candidate.json", "audit": "audit.jsonl"}
    if artifact == "audit":
        return AUDIT_PATH
    filename = names.get(artifact)
    if not filename:
        raise FileNotFoundError("Unknown artifact")
    return REPORT_ROOT / symbol.upper() / f"{interval}m" / candidate["candidate_version"] / filename
