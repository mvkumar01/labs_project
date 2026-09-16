"""Bounded backfill for the SENSEX Proposer paper book.

Two stages. First the Market Predictor rows are seeded into labs storage - for
history that means the capture taken from the Pramanaa pages and stored in the
alphaIMB research folder; live sessions will be written by the Predictor service
instead. Then each session with both Predictor rows and SENSEX quotes is
replayed. A session missing either input is recorded as unavailable rather than
traded on invented inputs.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR
from labs.engine.proposer_sensex_tracker import (
    IST,
    ProposerInputError,
    SYMBOL,
    _ensure_tables,
    record_unavailable,
    run_day,
    seed_predictor_rows,
)
from market_data.shared_store import resolve_options_source
from storage.db import get_conn

DEFAULT_START = "2026-06-22"
CAPTURE_PATHS = (
    Path.home() / "alphaIMB" / "research" / "experiments"
    / "2026-09-09_proposer_v1_reverse_engineering" / "source" / "predictor_full.tsv",
    Path("/home/mvkumar01/alphaIMB/research/experiments"
         "/2026-09-09_proposer_v1_reverse_engineering/source/predictor_full.tsv"),
)
KIND_MAP = {"5": "5class", "d": "drift", "regime": "regime", "event": "event"}


def load_capture(path: Path | None = None) -> pd.DataFrame:
    """Parse the captured Predictor history into the storage schema."""
    candidates = (path,) if path is not None else CAPTURE_PATHS
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            source = Path(candidate)
            break
    else:
        raise ProposerInputError(
            "Predictor capture not found; searched: "
            + ", ".join(str(c) for c in candidates if c))
    columns = ["date", "time", "kind", "label", "conf", "dist", "extra", "outcome"]
    raw = pd.read_csv(source, sep="\t", names=columns, dtype=str, quoting=3,
                      keep_default_na=False)
    raw = raw[raw["kind"].isin(KIND_MAP)].copy()
    raw["ts"] = pd.to_datetime(raw["date"] + " " + raw["time"], errors="coerce")
    raw = raw.dropna(subset=["ts"])
    is_five = raw["kind"] == "5"
    return pd.DataFrame({
        "trade_date": raw["date"],
        "ts": raw["ts"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": raw["kind"].map(KIND_MAP),
        "label": raw["label"].str.strip(),
        "conf": pd.to_numeric(raw["conf"].str.extract(r"(\d+(?:\.\d+)?)")[0], errors="coerce"),
        "microtrend": raw["extra"].str[6:7].where(is_five, ""),
        "mom5": raw["extra"].str[1:2].where(is_five, ""),
    })


def _default_end_date() -> str:
    now = datetime.now(IST)
    session = now.date()
    if session.weekday() < 5 and now.time() < time(15, 30):
        session -= timedelta(days=1)
    return session.isoformat()


def _sessions_with_quotes(start_date: str, end_date: str) -> list[str]:
    candidates = set()
    for root in (SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR):
        if not root.exists():
            continue
        for path in root.iterdir():
            if not path.is_dir():
                continue
            try:
                session = datetime.strptime(path.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if session.weekday() < 5 and start_date <= session.isoformat() <= end_date:
                candidates.add(session.isoformat())
    available = []
    for session in sorted(candidates):
        try:
            resolve_options_source(SYMBOL, session, live_root=SHARED_LIVE_DIR,
                                   archive_root=SHARED_ARCHIVE_DIR)
            available.append(session)
        except FileNotFoundError:
            pass
    return available


def run_backfill(*, start_date: str = DEFAULT_START, end_date: str | None = None,
                 limit: int = 5, rebuild: bool = False,
                 capture_path: Path | None = None) -> dict:
    end_date = end_date or _default_end_date()
    conn = get_conn()
    _ensure_tables(conn)
    seeded = 0
    try:
        try:
            capture = load_capture(capture_path)
            capture = capture[(capture["trade_date"] >= start_date)
                              & (capture["trade_date"] <= end_date)]
            seeded = seed_predictor_rows(capture, connection=conn)
        except ProposerInputError:
            seeded = 0            # live sessions may already be in storage
        with_rows = {row[0] for row in conn.execute(
            "SELECT DISTINCT trade_date FROM proposer_predictor_rows "
            "WHERE trade_date>=? AND trade_date<=?", (start_date, end_date))}
        done = {row[0] for row in conn.execute(
            "SELECT trade_date FROM proposer_daily WHERE trade_date>=? AND trade_date<=?",
            (start_date, end_date))}
    finally:
        conn.close()

    sessions = [s for s in _sessions_with_quotes(start_date, end_date) if s in with_rows]
    pending = sessions if rebuild else [s for s in sessions if s not in done]
    completed, unavailable, errors = [], [], {}
    for session in pending[:max(1, min(int(limit), 20))]:
        try:
            result = run_day(session)
            completed.append({"date": session, "trades": result["n_trades"],
                              "net_rs": result["net_rs"], "gross_ltp_rs": result["gross_ltp_rs"]})
        except ProposerInputError as exc:
            record_unavailable(session, str(exc))
            unavailable.append({"date": session, "reason": str(exc)})
        except Exception as exc:                                  # noqa: BLE001
            errors[session] = f"{type(exc).__name__}: {exc}"
    remaining = max(0, len(pending) - len(completed) - len(unavailable) - len(errors))
    return {"seeded_predictor_rows": seeded, "done": completed,
            "unavailable": unavailable, "errors": errors, "remaining": remaining}


if __name__ == "__main__":
    import json
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print(json.dumps(run_backfill(limit=limit), indent=2, default=str))
