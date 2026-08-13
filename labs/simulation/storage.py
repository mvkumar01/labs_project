"""Rollback-journal SQLite persistence for simulation sessions."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
import uuid

from labs.simulation.config import SIMULATION_DB_PATH
from labs.simulation.engine import SimulationEngine


def get_simulation_conn() -> sqlite3.Connection:
    SIMULATION_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SIMULATION_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_simulation_db() -> None:
    with get_simulation_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS simulation_sessions ("
            "session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )


class SimulationStore:
    def create(self, starting_capital: float = 1_000_000.0) -> tuple[str, dict]:
        session_id = uuid.uuid4().hex
        state = SimulationEngine.new_state(starting_capital)
        now = datetime.now(timezone.utc).isoformat()
        with get_simulation_conn() as conn:
            conn.execute(
                "INSERT INTO simulation_sessions "
                "(session_id, state_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, json.dumps(state), now, now),
            )
        return session_id, state

    def load(self, session_id: str) -> dict:
        with get_simulation_conn() as conn:
            row = conn.execute(
                "SELECT state_json FROM simulation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError("Simulation session not found")
        return json.loads(row["state_json"])

    def save(self, session_id: str, state: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_simulation_conn() as conn:
            cursor = conn.execute(
                "UPDATE simulation_sessions SET state_json = ?, updated_at = ? "
                "WHERE session_id = ?",
                (json.dumps(state), now, session_id),
            )
        if cursor.rowcount != 1:
            raise KeyError("Simulation session not found")

    def delete(self, session_id: str) -> None:
        with get_simulation_conn() as conn:
            conn.execute(
                "DELETE FROM simulation_sessions WHERE session_id = ?",
                (session_id,),
            )
