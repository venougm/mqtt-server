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
    telemetry_json  TEXT,
    weather_json    TEXT
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
    _migrate(conn)
    conn.commit()
    _conn = conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent in-place upgrades for DBs created by an older `_SCHEMA`
    (`CREATE TABLE IF NOT EXISTS` never adds columns to an existing table)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(packets)")}
    if "weather_json" not in columns:
        conn.execute("ALTER TABLE packets ADD COLUMN weather_json TEXT")
        logger.info("db migration: added packets.weather_json column")


def _ensure_initialized() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # Lazy fallback for callers that haven't explicitly called init_db()
        # (e.g. ad-hoc scripts/tests): initialize from the DB_PATH env var,
        # or the documented default if unset.
        init_db(os.environ.get("DB_PATH", "./data/aprs.db"))
    return _conn


# Live ingestion: every packet is the newest one heard, so it always becomes
# the station's latest.
_UPSERT_STATION_LIVE = """
    INSERT INTO stations (callsign, first_heard_at, last_heard_at, last_packet_id)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(callsign) DO UPDATE SET
        last_heard_at = excluded.last_heard_at,
        last_packet_id = excluded.last_packet_id
"""

# Backfill with an explicit historical `received_at`: the packet may be older
# than what is already stored, so `last_heard_at`/`last_packet_id` only move
# forward and `first_heard_at` keeps the older of the two. SQLite evaluates
# every SET expression against the pre-update row, so the CASE conditions all
# compare against the old `last_heard_at`.
_UPSERT_STATION_BACKFILL = """
    INSERT INTO stations (callsign, first_heard_at, last_heard_at, last_packet_id)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(callsign) DO UPDATE SET
        first_heard_at = MIN(stations.first_heard_at, excluded.first_heard_at),
        last_packet_id = CASE WHEN excluded.last_heard_at > stations.last_heard_at
                              THEN excluded.last_packet_id ELSE stations.last_packet_id END,
        last_heard_at = CASE WHEN excluded.last_heard_at > stations.last_heard_at
                             THEN excluded.last_heard_at ELSE stations.last_heard_at END
"""


def store_packet(parsed: dict[str, Any], received_at: str | None = None) -> str:
    """Insert one `packets` row and upsert the `stations` row for its callsign,
    inside a single transaction. Returns the server-receipt `received_at`
    timestamp (ISO 8601 UTC, full microsecond precision) used for the row, so
    callers (e.g. the WebSocket broadcast) can report the exact same timestamp
    without a second query.

    `received_at` defaults to now (live ingestion). Passing an explicit
    historical timestamp (backfill, see tools/import_aprsfi_raw.py) stores the
    row with that time and only advances the station's latest-packet pointer
    if the packet is newer than what is already stored."""
    conn = _ensure_initialized()
    if received_at is None:
        received_at = datetime.now(timezone.utc).isoformat()
        upsert_sql = _UPSERT_STATION_LIVE
    else:
        upsert_sql = _UPSERT_STATION_BACKFILL
    callsign = parsed["from"]
    with _db_lock:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.execute(
                """
                INSERT INTO packets
                    (callsign, received_at, raw_packet, latitude, longitude,
                     course, speed, altitude, comment, symbol, telemetry_json,
                     weather_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    parsed.get("weather_json"),
                ),
            )
            packet_id = cur.lastrowid
            cur.execute(upsert_sql, (callsign, received_at, received_at, packet_id))
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
                   p.comment, p.symbol, p.telemetry_json, p.weather_json,
                   p.raw_packet
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


# Weather keys exposed by the per-station weather endpoint (aprslib's metric
# names). Keys missing from a stored packet come back as None.
WEATHER_FIELDS = (
    "temperature",
    "humidity",
    "pressure",
    "wind_direction",
    "wind_speed",
    "wind_gust",
    "rain_1h",
    "rain_24h",
    "rain_since_midnight",
    "luminosity",
)


def get_weather_history(callsign: str, hours: int) -> list[dict[str, Any]]:
    """Weather points for `callsign` within the last `hours`, chronological
    ascending. Same cutoff convention and newest-first cap + reverse as
    `get_history()`. Each point has `received_at` plus every key in
    `WEATHER_FIELDS` (None when absent or non-numeric)."""
    import json
    from datetime import timedelta

    conn = _ensure_initialized()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _db_lock:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT received_at, weather_json
            FROM packets
            WHERE callsign = ?
              AND weather_json IS NOT NULL
              AND received_at >= ?
            ORDER BY received_at DESC, id DESC
            LIMIT 10000
            """,
            (callsign, cutoff),
        )
        rows = cur.fetchall()

    result = []
    for received_at, weather_json in reversed(rows):
        weather = json.loads(weather_json)
        point: dict[str, Any] = {"received_at": received_at}
        for key in WEATHER_FIELDS:
            value = weather.get(key)
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            point[key] = value if numeric else None
        result.append(point)
    return result


# Generic analog-channel labels used when a station has no usable telemetry
# config yet (same fallback philosophy as the map popup's raw_vals display).
TELEMETRY_FALLBACK_NAMES = ("Analog1", "Analog2", "Analog3", "Analog4", "Analog5")


def get_telemetry_history(callsign: str, hours: int) -> dict[str, Any]:
    """Analog telemetry series for `callsign` within the last `hours`,
    chronological ascending. Same cutoff convention and newest-first cap +
    reverse as `get_history()`/`get_weather_history()`.

    Returns a dict with:
      - "channels": ordered list of {"name", "unit"} describing the 5 analog
        channels (names/units from the station's EQNS/UNIT/PARM config when
        available, generic Analog1..Analog5 with empty units otherwise).
      - "points": list of {"received_at", "channels": {name: value|None},
        "raw_vals": [..]} chronological ascending.

    A packet's stored `telemetry_json` is one of two shapes (see
    mqtt_ingest._build_telemetry_json): the named shape
    `{name: {"value", "unit"}}` written when a usable config existed at ingest
    time, or the raw fallback `{"raw_seq", "raw_vals"}` written otherwise. This
    function normalizes both. For raw-shape rows it applies the station's
    CURRENT telemetry config (via apply_equations) when one is usable now, so
    EQNS/UNIT/PARM that arrived after the data was stored still label the
    history. The channel-name set is taken from the current config when usable,
    else from any named-shape rows found, else the generic fallback."""
    import json
    from datetime import timedelta

    from app.telemetry import apply_equations

    conn = _ensure_initialized()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _db_lock:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT received_at, telemetry_json
            FROM packets
            WHERE callsign = ?
              AND telemetry_json IS NOT NULL
              AND received_at >= ?
            ORDER BY received_at DESC, id DESC
            LIMIT 10000
            """,
            (callsign, cutoff),
        )
        rows = cur.fetchall()

    config = get_telemetry_config(callsign)
    usable = config is not None and _telemetry_config_usable(config)

    # First pass: parse each stored packet into a positional analog list
    # (index 0-4) plus an optional per-name dict from any named-shape row. The
    # final channel labels are decided after the scan so every point can be
    # keyed by the same ordered name set.
    parsed_rows: list[dict[str, Any]] = []
    named_labels: list[str] | None = None
    named_units: list[str] | None = None
    for received_at, telemetry_json in reversed(rows):
        try:
            telemetry = json.loads(telemetry_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(telemetry, dict):
            continue

        if "raw_vals" in telemetry:
            raw_vals = telemetry.get("raw_vals")
            vals = raw_vals if isinstance(raw_vals, list) else None
            parsed_rows.append({"received_at": received_at, "raw_vals": vals, "named": None})
        else:
            # Named shape {name: {"value", "unit"}} written at ingest time.
            if named_labels is None:
                keys = [k for k in telemetry.keys()][:5]
                if keys:
                    named_labels = keys
                    named_units = [
                        str(telemetry[k].get("unit", "")) if isinstance(telemetry.get(k), dict) else ""
                        for k in keys
                    ]
            parsed_rows.append({"received_at": received_at, "raw_vals": None, "named": telemetry})

    # Decide the ordered channel labels/units. A usable current config is
    # authoritative (it also lets us apply EQNS to raw-shape rows below);
    # otherwise use labels discovered from a named-shape row; otherwise the
    # generic Analog1..Analog5 fallback.
    if usable:
        names = [str(n) for n in config["parm_json"][:5]]
        units = [str(u) for u in config["unit_json"][:5]]
    elif named_labels is not None:
        names = named_labels + list(TELEMETRY_FALLBACK_NAMES[len(named_labels):])
        units = (named_units or []) + ["" for _ in range(5 - len(named_labels))]
    else:
        names = list(TELEMETRY_FALLBACK_NAMES)
        units = ["" for _ in range(5)]
    # Guarantee exactly 5 labels/units even if config arrays were short.
    names = (names + list(TELEMETRY_FALLBACK_NAMES))[:5]
    units = (units + ["" for _ in range(5)])[:5]

    points: list[dict[str, Any]] = []
    for row in parsed_rows:
        channel_values: dict[str, Any] = {name: None for name in names}
        raw_vals = row["raw_vals"]
        if raw_vals is not None:
            if usable and len(raw_vals) >= 5:
                named = apply_equations(raw_vals, config)
                for i, name in enumerate(names):
                    entry = named.get(config["parm_json"][i]) if i < len(config["parm_json"]) else None
                    channel_values[name] = entry["value"] if entry else None
            else:
                for i, name in enumerate(names):
                    channel_values[name] = raw_vals[i] if i < len(raw_vals) else None
        elif row["named"] is not None:
            for name in names:
                entry = row["named"].get(name)
                if isinstance(entry, dict):
                    value = entry.get("value")
                    channel_values[name] = value if _is_numeric(value) else None
        points.append(
            {
                "received_at": row["received_at"],
                "channels": channel_values,
                "raw_vals": raw_vals,
            }
        )

    return {
        "channels": [{"name": names[i], "unit": units[i]} for i in range(5)],
        "points": points,
    }


def _telemetry_config_usable(config: dict[str, Any]) -> bool:
    """Mirror of mqtt_ingest._config_usable: all three of EQNS/UNIT/PARM must
    carry at least 5 analog-channel entries before apply_equations() is safe."""
    return (
        len(config.get("eqns_json") or []) >= 5
        and len(config.get("unit_json") or []) >= 5
        and len(config.get("parm_json") or []) >= 5
    )


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def packet_exists(callsign: str, raw_packet: str, received_at: str) -> bool:
    """True if an identical packet (same callsign, raw text and receipt time)
    is already stored; used by the backfill importer to stay idempotent."""
    conn = _ensure_initialized()
    with _db_lock:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1 FROM packets
            WHERE callsign = ? AND raw_packet = ? AND received_at = ?
            LIMIT 1
            """,
            (callsign, raw_packet, received_at),
        )
        return cur.fetchone() is not None


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
