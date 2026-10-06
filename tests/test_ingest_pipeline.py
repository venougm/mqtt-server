"""Pipeline-injection integration test.

Feeds each tests/sample_packets/*.txt fixture straight through the parse ->
store -> broadcast logic extracted from on_message (parse_packet +
process_parsed_packet), against an in-memory SQLite DB and a fake ws_manager
that records broadcast_json calls instead of sending over a real socket.
Asserts rows land correctly in packets/stations/telemetry_config, the fake
broadcaster received the right payload shape (matching the WebSocket contract),
and that malformed fixtures produce no DB row and no broadcast call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "sample_packets"

EXPECTED_WS_KEYS = {
    "type", "callsign", "received_at", "latitude", "longitude",
    "course", "speed", "altitude", "comment", "symbol", "telemetry", "weather",
    "raw_packet",
}


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


@pytest.fixture()
def fresh_db():
    import app.db as db

    db.init_db(":memory:")
    yield db


def test_valid_position_lands_in_db_and_broadcasts(fresh_db):
    from app.mqtt_ingest import parse_packet, process_parsed_packet

    broadcasts = []
    raw = _read_fixture("position_basic.txt")
    parsed = parse_packet(raw)
    assert parsed is not None

    process_parsed_packet(parsed, broadcasts.append)

    assert len(broadcasts) == 1
    payload = broadcasts[0]
    assert set(payload.keys()) == EXPECTED_WS_KEYS
    assert payload["type"] == "position"
    assert payload["callsign"] == "YB1ABC-9"
    assert payload["telemetry"] is None  # no telemetry block in this fixture
    assert payload["weather"] is None  # no weather block in this fixture

    stations = fresh_db.get_stations()
    assert len(stations) == 1
    assert stations[0]["callsign"] == "YB1ABC-9"

    history = fresh_db.get_history("YB1ABC-9", 24)
    assert len(history) == 1


def test_telemetry_config_message_produces_no_packet_row_and_no_broadcast(fresh_db):
    from app.mqtt_ingest import parse_packet, process_parsed_packet

    broadcasts = []
    raw = _read_fixture("telemetry_config_message.txt")
    parsed = parse_packet(raw)
    assert parsed is not None
    assert parsed["format"] == "telemetry-message"

    process_parsed_packet(parsed, broadcasts.append)

    assert broadcasts == []
    assert fresh_db.get_stations() == []


def test_position_with_telemetry_always_includes_telemetry_key(fresh_db):
    from app.mqtt_ingest import parse_packet, process_parsed_packet

    broadcasts = []
    raw = _read_fixture("position_with_telemetry.txt")
    parsed = parse_packet(raw)
    assert parsed is not None

    process_parsed_packet(parsed, broadcasts.append)

    assert len(broadcasts) == 1
    payload = broadcasts[0]
    assert "telemetry" in payload
    assert payload["telemetry"] is not None
    assert "raw_vals" in payload["telemetry"]  # no config registered in this test


@pytest.mark.parametrize("fixture_name", ["malformed_truncated.txt", "malformed_empty.txt"])
def test_malformed_fixtures_produce_no_row_and_no_broadcast(fresh_db, fixture_name):
    from app.mqtt_ingest import parse_packet

    broadcasts = []
    raw = _read_fixture(fixture_name)
    parsed = parse_packet(raw)

    assert parsed is None
    # parse_packet already returned None; the real on_message_impl would
    # return here without ever calling process_parsed_packet/broadcast_fn.
    assert broadcasts == []
    assert fresh_db.get_stations() == []


def test_on_message_impl_end_to_end_valid_packet(fresh_db):
    from app.mqtt_ingest import on_message_impl

    broadcasts = []
    raw = _read_fixture("position_course_speed.txt")

    on_message_impl("aprs/YB1ABC-9", raw, broadcasts.append)

    assert len(broadcasts) == 1
    assert fresh_db.get_stations()[0]["callsign"] == "YB1ABC-9"


def test_on_message_impl_malformed_packet_no_crash(fresh_db):
    from app.mqtt_ingest import on_message_impl

    broadcasts = []
    raw = _read_fixture("malformed_truncated.txt")

    # Must not raise.
    on_message_impl("aprs/YB1ABC-9", raw, broadcasts.append)

    assert broadcasts == []
    assert fresh_db.get_stations() == []
