"""Unit tests for app.mqtt_ingest.parse_packet() against sample_packets fixtures.

Each valid fixture is asserted to return the expected fields; malformed fixtures
must return None with no exception propagating. Includes the explicit
speed-unit regression assertion: parse_packet()'s speed equals aprslib's own
unmodified parsed['speed'] -- no re-scaling.
"""

from __future__ import annotations

from pathlib import Path

import aprslib

from app.mqtt_ingest import parse_packet

FIXTURES_DIR = Path(__file__).parent / "sample_packets"


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def test_position_basic():
    raw = _read_fixture("position_basic.txt")
    result = parse_packet(raw)
    assert result is not None
    assert result["from"] == "YB1ABC-9"
    assert result["latitude"] == aprslib.parse(raw)["latitude"]
    assert result["longitude"] == aprslib.parse(raw)["longitude"]
    assert result["comment"] == "test comment"


def test_position_course_speed():
    raw = _read_fixture("position_course_speed.txt")
    result = parse_packet(raw)
    assert result is not None
    assert result["course"] == 180
    assert "speed" in result
    assert "altitude" in result
    # Regression guard: parse_packet must not re-scale aprslib's own speed value
    # (aprslib already converts knots -> km/h internally).
    assert result["speed"] == aprslib.parse(raw)["speed"]


def test_position_with_comment():
    raw = _read_fixture("position_with_comment.txt")
    result = parse_packet(raw)
    assert result is not None
    assert result["comment"] == "iGate test comment here"


def test_position_with_telemetry():
    raw = _read_fixture("position_with_telemetry.txt")
    result = parse_packet(raw)
    assert result is not None
    assert "telemetry" in result
    assert result["telemetry"]["vals"] == [10, 20, 30, 40, 50]


def test_telemetry_config_message():
    raw = _read_fixture("telemetry_config_message.txt")
    result = parse_packet(raw)
    assert result is not None
    assert result["format"] == "telemetry-message"
    assert "tEQNS" in result


def test_malformed_truncated_returns_none():
    raw = _read_fixture("malformed_truncated.txt")
    result = parse_packet(raw)
    assert result is None


def test_malformed_empty_returns_none():
    raw = _read_fixture("malformed_empty.txt")
    result = parse_packet(raw)
    assert result is None
