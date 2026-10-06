"""SQLite storage layer.

Owns one module-level connection created with `check_same_thread=False`, plus one
module-level `threading.Lock` (`_db_lock`). Every public function below acquires
`_db_lock` for the entire duration of its SQL work and creates a fresh
`conn.cursor()` inside that locked section -- never a shared or module-level
cursor, never a cursor held across calls.

This is deliberately not justified by "SQLite's own locking serializes conflicting
access": concurrent use of the *same connection object* from multiple threads is
only safe when the underlying SQLite library was compiled in "serialized"
threading mode; in "multi-thread" mode it is explicitly not safe, and which mode
is active is a property of how SQLite was compiled on whatever machine runs this
-- it can differ between the Windows build environment and the Ubuntu 24.04
deployment target. The explicit `_db_lock` removes that dependency entirely:
FastAPI's threadpool can run several read requests concurrently while the MQTT
ingest thread writes on its own schedule, and with every public function
serialized through `_db_lock`, only one thread ever touches the connection or
issues SQL at a time, regardless of the host's SQLite compile-time threading mode.

`_db_lock` is always acquired via `with _db_lock:` (never bare `.acquire()`/
`.release()`), so the lock is guaranteed to be released even if the SQL work
raises partway through -- a transient DB error degrades to "this one call failed"
rather than permanently deadlocking every subsequent call that needs `_db_lock`.
Each write function additionally wraps its SQL in `try/except Exception:
_conn.rollback(); raise` inside the `with _db_lock:` block, so a failure partway
through a multi-statement write leaves no half-committed transaction behind.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_db_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign        TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    raw_packet      TEXT NOT NULL,
    latitude        REAL,
    longitude       REAL,
    course          REAL,
    speed           REAL,
    altitude        REAL,
    comment         TEXT,
    symbol          TEXT,
    telemetry_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_packets_callsign_time ON packets (callsign, received_at, id);

CREATE TABLE IF NOT EXISTS stations (
    callsign        TEXT PRIMARY KEY,
    first_heard_at  TEXT NOT NULL,
    last_heard_at   TEXT NOT NULL,
    last_packet_id  INTEGER NOT NULL REFERENCES packets(id)
);

CREATE TABLE IF NOT EXISTS telemetry_config (
    callsign    TEXT PRIMARY KEY,
    eqns_json   TEXT NOT NULL,
    unit_json   TEXT NOT NULL,
    parm_json   TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def init_db(db_path: str) -> None:
    """(Re)initialize the module-level connection and schema. Safe to call once
    at startup; also used by tests to point at a fresh/in-memory DB."""
    global _conn
    if db_path != ":memory:":
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

    # isolation_level is left at its default: each write function below issues
    # its own explicit `cur.execute("BEGIN")`, so sqlite3's implicit
    # transaction-before-DML logic never triggers (it only fires when no
    # transaction is already open).
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    _conn = conn


def _ensure_initialized() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # Lazy fallback for callers that haven't explicitly called init_db()
        # (e.g. ad-hoc scripts/tests): initialize from the DB_PATH env var,
        # or the documented default if unset.
        init_db(os.environ.get("DB_PATH", "./data/aprs.db"))
    return _conn


def store_packet(parsed: dict[str, Any]) -> str:
    """Insert one `packets` row and upsert the `stations` row for its callsign,
    inside a single transaction. Returns the server-receipt `received_at`
    timestamp (ISO 8601 UTC, full microsecond precision) used for the row, so
    callers (e.g. the WebSocket broadcast) can report the exact same timestamp
    without a second query."""
    conn = _ensure_initialized()
    received_at = datetime.now(timezone.utc).isoformat()
    callsign = parsed["from"]
    with _db_lock:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.execute(
                """
                INSERT INTO packets
                    (callsign, received_at, raw_packet, latitude, longitude,
                     course, speed, altitude, comment, symbol, telemetry_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    callsign,
                    received_at,
                    parsed.get("raw_packet"),
                    parsed.get("latitude"),
                    parsed.get("longitude"),
                    parsed.get("course"),
                    parsed.get("speed"),
                    parsed.get("altitude"),
                    parsed.get("comment"),
                    parsed.get("symbol"),
                    parsed.get("telemetry_json"),
                ),
            )
            packet_id = cur.lastrowid
            cur.execute(
                """
                INSERT INTO stations (callsign, first_heard_at, last_heard_at, last_packet_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(callsign) DO UPDATE SET
                    last_heard_at = excluded.last_heard_at,
                    last_packet_id = excluded.last_packet_id
                """,
                (callsign, received_at, received_at, packet_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return received_at


def get_stations() -> list[dict[str, Any]]:
    """Latest known position/telemetry per station, via the `stations` index
    joined to each station's latest `packets` row."""
    conn = _ensure_initialized()
    with _db_lock:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.callsign,
                   s.last_heard_at AS received_at,
                   p.latitude, p.longitude, p.course, p.speed, p.altitude,
                   p.comment, p.symbol, p.telemetry_json, p.raw_packet
            FROM stations s
            JOIN packets p ON p.id = s.last_packet_id
            ORDER BY s.callsign ASC
            """
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def get_history(callsign: str, hours: int) -> list[dict[str, Any]]:
    """Packet rows for `callsign` within the last `hours`, chronological ascending
    (oldest first). Queried `received_at DESC, id DESC` with `LIMIT 10000` so a
    bound cap drops the oldest rows rather than the newest, then reversed in
    Python before returning.

    The cutoff is computed in Python with `datetime.now(timezone.utc) -
    timedelta(hours=hours)` and formatted via `.isoformat()` -- the exact same
    convention used to write `received_at` in `store_packet()` -- rather than
    relying on SQLite's `datetime('now', ...)`, which emits a space-separated,
    timezone-less string (`YYYY-MM-DD HH:MM:SS`). Comparing that against the
    ISO `received_at` values (`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`) lexically
    misjudges the boundary: `'T'` (0x54) sorts after `' '` (0x20), so once the
    date portion matches, `received_at >= cutoff` was true for any time of day
    on the cutoff's calendar date, silently admitting up to ~24h more history
    than requested. Formatting both sides with the same `.isoformat()`
    convention keeps the string comparison correct."""
    from datetime import timedelta

    conn = _ensure_initialized()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _db_lock:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT received_at, latitude, longitude, speed, course, altitude
            FROM packets
            WHERE callsign = ?
              AND received_at >= ?
            ORDER BY received_at DESC, id DESC
            LIMIT 10000
            """,
            (callsign, cutoff),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    result = [dict(zip(columns, row)) for row in rows]
    result.reverse()
    return result


def upsert_telemetry_config(
    callsign: str,
    eqns: list | None,
    unit: list | None,
    parm: list | None,
) -> None:
    """Read-modify-write merge: fields not provided this call (None) preserve the
    existing stored value for that field, so metadata split across multiple
    EQNS/UNIT/PARM messages accumulates rather than overwriting with blanks."""
    import json

    conn = _ensure_initialized()
    updated_at = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.execute(
                "SELECT eqns_json, unit_json, parm_json FROM telemetry_config WHERE callsign = ?",
                (callsign,),
            )
            existing = cur.fetchone()
            if existing is not None:
                existing_eqns, existing_unit, existing_parm = existing
            else:
                existing_eqns = existing_unit = existing_parm = None

            # eqns_json/unit_json/parm_json are NOT NULL columns; APRS telemetry
            # metadata is sometimes split across multiple EQNS/UNIT/PARM
            # messages, so the first-ever message for a callsign may only
            # supply one of the three. Default any field with neither a new
            # value nor an existing stored value to an empty JSON array rather
            # than leaving it NULL -- callers treat an incomplete (< 5 element)
            # array the same as "no config yet" (see mqtt_ingest._build_telemetry_json).
            eqns_json = json.dumps(eqns) if eqns is not None else (existing_eqns if existing_eqns is not None else "[]")
            unit_json = json.dumps(unit) if unit is not None else (existing_unit if existing_unit is not None else "[]")
            parm_json = json.dumps(parm) if parm is not None else (existing_parm if existing_parm is not None else "[]")

            cur.execute(
                """
                INSERT INTO telemetry_config (callsign, eqns_json, unit_json, parm_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(callsign) DO UPDATE SET
                    eqns_json = excluded.eqns_json,
                    unit_json = excluded.unit_json,
                    parm_json = excluded.parm_json,
                    updated_at = excluded.updated_at
                """,
                (callsign, eqns_json, unit_json, parm_json, updated_at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def get_telemetry_config(callsign: str) -> dict[str, Any] | None:
    """Fetch a station's stored telemetry calibration, or None if none exists yet."""
    import json

    conn = _ensure_initialized()
    with _db_lock:
        cur = conn.cursor()
        cur.execute(
            "SELECT eqns_json, unit_json, parm_json FROM telemetry_config WHERE callsign = ?",
            (callsign,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    eqns_json, unit_json, parm_json = row
    return {
        "eqns_json": json.loads(eqns_json),
        "unit_json": json.loads(unit_json),
        "parm_json": json.loads(parm_json),
    }
