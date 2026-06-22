"""Semantic SQLite audit and safe quarantine for malformed trade rows.

SQLite's ``PRAGMA integrity_check`` validates B-trees and pages, but it does not
reject arbitrary bytes stored in TEXT columns.  This audit complements the
structural check by decoding every TEXT value in ``trades`` as UTF-8.

Default mode is read-only.  ``--quarantine`` copies malformed rows byte-for-byte
to ``corrupt_trades_quarantine`` inside one transaction before deleting them
from ``trades``.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config.labs_config import DB_PATH


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = "file:" + path.resolve().as_posix() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(str(path), timeout=30)
    # Return raw bytes for TEXT so malformed UTF-8 can be inspected safely.
    conn.text_factory = bytes
    return conn


def invalid_trade_text_rows(conn: sqlite3.Connection) -> dict[int, list[str]]:
    info = conn.execute("PRAGMA table_info(trades)").fetchall()
    text_columns = []
    for row in info:
        name = row[1].decode() if isinstance(row[1], bytes) else str(row[1])
        declared = row[2].decode() if isinstance(row[2], bytes) else str(row[2])
        if declared.upper() in {"TEXT", "VARCHAR"}:
            text_columns.append(name)

    invalid: dict[int, list[str]] = {}
    for column in text_columns:
        sql = f'SELECT rowid, CAST("{column}" AS BLOB) FROM trades WHERE "{column}" IS NOT NULL'
        for rowid, value in conn.execute(sql):
            if value is None:
                continue
            try:
                value.decode("utf-8")
            except UnicodeDecodeError:
                invalid.setdefault(int(rowid), []).append(column)
    return invalid


def audit(path: Path) -> dict:
    conn = _connect(path, readonly=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if isinstance(integrity, bytes):
            integrity = integrity.decode("utf-8", errors="replace")
        invalid = invalid_trade_text_rows(conn)
        return {
            "path": str(path),
            "integrity": integrity,
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0].decode(),
            "trade_rows": int(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]),
            "invalid_trade_rows": invalid,
        }
    finally:
        conn.close()


def quarantine(path: Path, invalid: dict[int, list[str]]) -> int:
    if not invalid:
        return 0
    conn = _connect(path, readonly=False)
    rowids = sorted(invalid)
    placeholders = ",".join("?" for _ in rowids)
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS corrupt_trades_quarantine AS "
            "SELECT rowid AS source_rowid, CAST(NULL AS TEXT) AS reason, "
            "CAST(NULL AS TEXT) AS quarantined_at, trades.* FROM trades WHERE 0"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_corrupt_trades_source_rowid "
            "ON corrupt_trades_quarantine(source_rowid)"
        )
        for rowid in rowids:
            reason = "invalid UTF-8 in: " + ", ".join(invalid[rowid])
            conn.execute(
                "INSERT OR IGNORE INTO corrupt_trades_quarantine "
                "SELECT rowid, ?, ?, trades.* FROM trades WHERE rowid=?",
                (reason, now, rowid),
            )
        copied = conn.execute(
            f"SELECT COUNT(*) FROM corrupt_trades_quarantine WHERE source_rowid IN ({placeholders})",
            rowids,
        ).fetchone()[0]
        if copied != len(rowids):
            raise RuntimeError(f"Quarantine copy mismatch: expected {len(rowids)}, copied {copied}")
        conn.execute(f"DELETE FROM trades WHERE rowid IN ({placeholders})", rowids)
        conn.commit()
        return len(rowids)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--quarantine", action="store_true")
    args = parser.parse_args()

    result = audit(args.db)
    print(f"DB: {result['path']}")
    print(f"integrity={result['integrity']} journal={result['journal_mode']}")
    print(
        f"trades={result['trade_rows']} "
        f"invalid_trade_rows={len(result['invalid_trade_rows'])}"
    )
    for rowid, columns in result["invalid_trade_rows"].items():
        print(f"  rowid={rowid} invalid_text={','.join(columns)}")

    if args.quarantine:
        count = quarantine(args.db, result["invalid_trade_rows"])
        print(f"quarantined={count}")
        after = audit(args.db)
        if after["integrity"] != "ok" or after["invalid_trade_rows"]:
            raise RuntimeError(f"Post-quarantine audit failed: {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
