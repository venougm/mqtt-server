"""Unit test for app.telemetry.apply_equations() plus the config-then-position
integration sequence (telemetry-message followed by a position+telemetry
packet for the same callsign, driven through mqtt_ingest's on_message-equivalent
logic against an in-memory DB)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.telemetry import apply_equations

FIXTURES_DIR = Path(__file__).parent / "sample_packets"


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def test_apply_equations_known_triple():
    config = {
        "eqns_json": [[0, 0.01, 0]] * 5,
        "unit_json": ["V", "C", "m", "%", "deg"] + [""] * 8,
        "parm_json": ["V_Batt", "Temp", "Alt", "Hum", "Dir"] + [""] * 8,
    }
    result = apply_equations([400, 0, 0, 0, 0], config)
    assert result["V_Batt"] == {"value": 4.0, "unit": "V"}


@pytest.fixture()
def fresh_db(monkeypatch):
    import app.db as db

    db.init_db(":memory:")
    yield db


def test_config_then_position_named_shape(fresh_db):
    import aprslib
    from app.mqtt_ingest import process_parsed_packet

    broadcasts = []

    def fake_broadcast(payload):
        broadcasts.append(payload)

    config_raw = _read_fixture("telemetry_config_message.txt")
    config_parsed = aprslib.parse(config_raw)
    process_parsed_packet(config_parsed, fake_broadcast)

    # UNIT/PARM also need to be registered for the named shape to resolve;
    # feed them in too (read-modify-write merge in db.upsert_telemetry_config).
    addr = "YB1ABC-9".ljust(9)
    unit_raw = f"YB1ABC-9>APRS,TCPIP*::{addr}:UNIT.V,C,m,%,deg".encode()
    parm_raw = f"YB1ABC-9>APRS,TCPIP*::{addr}:PARM.V_Batt,Temp,Alt,Hum,Dir".encode()
    process_parsed_packet(aprslib.parse(unit_raw), fake_broadcast)
    process_parsed_packet(aprslib.parse(parm_raw), fake_broadcast)

    # telemetry-message packets must never produce a broadcast or a packets row.
    assert broadcasts == []

    position_raw = _read_fixture("position_with_telemetry.txt")
    position_parsed = aprslib.parse(position_raw)
    process_parsed_packet(position_parsed, fake_broadcast)

    assert len(broadcasts) == 1
    telemetry = broadcasts[0]["telemetry"]
    assert telemetry is not None
    assert "V_Batt" in telemetry
    assert telemetry["V_Batt"]["unit"] == "V"
    # vals[0] == 10 per the fixture; eqn [0, 0.01, 0] -> 0.1
    assert telemetry["V_Batt"]["value"] == pytest.approx(0.1)


def test_position_with_telemetry_no_prior_config_uses_fallback(fresh_db):
    import aprslib
    from app.mqtt_ingest import process_parsed_packet

    broadcasts = []

    def fake_broadcast(payload):
        broadcasts.append(payload)

    position_raw = _read_fixture("position_with_telemetry.txt")
    position_parsed = aprslib.parse(position_raw)
    process_parsed_packet(position_parsed, fake_broadcast)

    assert len(broadcasts) == 1
    telemetry = broadcasts[0]["telemetry"]
    assert telemetry is not None
    assert "raw_seq" in telemetry
    assert "raw_vals" in telemetry
    assert telemetry["raw_vals"] == [10, 20, 30, 40, 50]
