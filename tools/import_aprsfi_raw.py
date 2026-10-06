"""Backfill station history from lines copied off aprs.fi's raw-packets page.

Each input line looks like:

    2026-10-06 04:24:10 WIB: YG2UFH-10>APLRG1,TCPIP*,qAC,T2CSNGRAD:=LRDS_jEH8_ !G...

The leading local timestamp + timezone abbreviation becomes the packet's
historical `received_at` (stored as UTC ISO 8601 with microseconds, the same
format `db.store_packet()` writes). The packet text goes through the same
`parse_packet()` + `process_parsed_packet()` path as live MQTT ingestion.
Re-importing the same lines is a no-op (exact duplicates are skipped).

This tool only reads text you paste yourself; it never fetches anything
from aprs.fi.

Usage (from the project root):

    .venv\\Scripts\\python.exe tools\\import_aprsfi_raw.py packets.txt
    Get-Content packets.txt | .venv\\Scripts\\python.exe tools\\import_aprsfi_raw.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.mqtt_ingest import parse_packet, process_parsed_packet  # noqa: E402

TZ_OFFSETS_HOURS = {
    "WIB": 7,
    "WITA": 8,
    "WIT": 9,
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
}

_LINE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+([A-Za-z]+):\s*(\S.*?)\s*$"
)


def parse_line(line: str) -> tuple[str, str]:
    """Split one aprs.fi raw line into (received_at UTC ISO string, packet).
    Raises ValueError with a readable reason if the line can't be used."""
    match = _LINE_RE.match(line)
    if not match:
        raise ValueError("expected 'YYYY-MM-DD HH:MM:SS TZ: <packet>'")
    date_part, time_part, tz_abbr, packet = match.groups()

    offset = TZ_OFFSETS_HOURS.get(tz_abbr.upper())
    if offset is None:
        known = ", ".join(TZ_OFFSETS_HOURS)
        raise ValueError(f"unknown timezone abbreviation {tz_abbr!r} (supported: {known})")

    try:
        local = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError as e:
        raise ValueError(f"invalid date/time: {e}") from None

    aware = local.replace(tzinfo=timezone(timedelta(hours=offset)))
    received_at = aware.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return received_at, packet


def import_lines(lines: Iterable[str], out=sys.stdout) -> dict[str, int]:
    """Import every usable line; blank lines and lines starting with '#' are
    ignored. Returns counts: imported / duplicates / failures."""
    summary = {"imported": 0, "duplicates": 0, "failures": 0}

    for lineno, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        try:
            received_at, packet = parse_line(line)
        except ValueError as e:
            print(f"line {lineno}: skipped, {e}", file=out)
            summary["failures"] += 1
            continue

        parsed = parse_packet(packet)
        if parsed is None:
            print(f"line {lineno}: skipped, APRS packet could not be parsed", file=out)
            summary["failures"] += 1
            continue

        is_position = parsed.get("format") != "telemetry-message"
        if is_position and db.packet_exists(parsed["from"], parsed.get("raw"), received_at):
            summary["duplicates"] += 1
            continue

        try:
            process_parsed_packet(parsed, lambda payload: None, received_at=received_at)
        except Exception as e:
            print(f"line {lineno}: skipped, storing failed: {e}", file=out)
            summary["failures"] += 1
            continue
        summary["imported"] += 1

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import packets copied from aprs.fi's raw-packets page into the local DB."
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="text file with one aprs.fi raw line per line (reads stdin if omitted)",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.file).resolve() if args.file else None

    # Load .env/config.yaml (and resolve a relative DB_PATH) from the project
    # root, exactly like the running server does.
    os.chdir(ROOT)
    from app.config import get_settings

    settings = get_settings()
    db.init_db(settings.db_path)
    print(f"database: {settings.db_path}")

    if input_path is not None:
        with input_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            summary = import_lines(f)
    else:
        summary = import_lines(sys.stdin)

    print(
        f"imported: {summary['imported']}, "
        f"duplicates skipped: {summary['duplicates']}, "
        f"parse failures: {summary['failures']}"
    )
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
