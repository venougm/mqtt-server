"""Round-trip verification for the Raspberry Pi APRS publisher.

The whole point of these tests: prove that the packets rpi-station/aws_publisher.py
emits actually parse with the SAME library the backend uses (aprslib), because a
packet aprslib cannot parse is silently dropped by app/mqtt_ingest.py and never
reaches the map. We build packets from known sensor inputs and assert aprslib
returns the weather/telemetry/metadata fields back (within rounding).

aws_publisher.py lives in the sibling `rpi-station/` directory (not a package, and
the folder name has a hyphen), so it is loaded here by file path via importlib.
The module imports cleanly off-Pi because its hardware reads guard their imports.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import aprslib
import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "rpi-station" / "aws_publisher.py"


def _load_publisher():
    spec = importlib.util.spec_from_file_location("aws_publisher", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Register before exec so dataclasses can resolve the module's annotations.
    sys.modules["aws_publisher"] = module
    spec.loader.exec_module(module)
    return module


pub = _load_publisher()


def test_module_imports_off_pi():
    # Loading the module (done above) must not require board/busio/adafruit/smbus.
    assert hasattr(pub, "build_wx_packet")
    assert pub.DEFAULT_CALLSIGN == "VN2EWS-1"


def test_wx_packet_roundtrips_through_aprslib():
    # Known inputs: 27.8 degC, 63 %RH, 985.5 mbar.
    packet = pub.build_wx_packet(
        pub.DEFAULT_CALLSIGN, pub.DEFAULT_LAT, pub.DEFAULT_LON,
        temp_c=27.8, humidity=63, pressure_mbar=985.5,
    )
    parsed = aprslib.parse(packet)

    assert parsed["from"] == "VN2EWS-1"
    weather = parsed["weather"]
    # F whole-degree rounding costs up to ~0.6 degC; allow +/-1 degC.
    assert weather["temperature"] == pytest.approx(27.8, abs=1.0)
    assert weather["humidity"] == 63
    assert weather["pressure"] == pytest.approx(985.5, abs=0.05)
    # Weather-station symbol: table '/', code '_'.
    assert parsed["symbol_table"] == "/"
    assert parsed["symbol"] == "_"


def test_wx_packet_with_wind_roundtrips():
    packet = pub.build_wx_packet(
        pub.DEFAULT_CALLSIGN, pub.DEFAULT_LAT, pub.DEFAULT_LON,
        temp_c=20.0, humidity=80, pressure_mbar=1013.2,
        wind_dir_deg=180, wind_speed_mph=10, wind_gust_mph=15,
    )
    parsed = aprslib.parse(packet)
    weather = parsed["weather"]
    assert weather["temperature"] == pytest.approx(20.0, abs=1.0)
    assert weather["pressure"] == pytest.approx(1013.2, abs=0.05)
    # aprslib reports wind in m/s; 15 mph ~= 6.7 m/s.
    assert weather["wind_gust"] == pytest.approx(15 * 0.44704, abs=0.5)


def test_wx_packet_omits_missing_fields():
    # No sensor: temperature/humidity/pressure all None -> packet still parses.
    packet = pub.build_wx_packet(
        pub.DEFAULT_CALLSIGN, pub.DEFAULT_LAT, pub.DEFAULT_LON,
        temp_c=None, humidity=None, pressure_mbar=None,
    )
    parsed = aprslib.parse(packet)
    assert parsed["from"] == "VN2EWS-1"


def test_telemetry_values_roundtrip_through_aprslib():
    analog = [550, 87, 34, 45, 123]  # raw scaled ints for the 5 analog channels
    packet = pub.build_telemetry_packet(
        pub.DEFAULT_CALLSIGN, pub.DEFAULT_LAT, pub.DEFAULT_LON,
        seq=7, analog_raw=analog, bits="10110000",
    )
    parsed = aprslib.parse(packet)
    telemetry = parsed["telemetry"]
    assert telemetry is not None
    assert telemetry["seq"] == 7
    assert telemetry["vals"] == analog
    assert telemetry["bits"] == "10110000"


def test_telemetry_requires_five_channels():
    with pytest.raises(ValueError):
        pub.encode_telemetry_values(1, [1, 2, 3])


def test_parm_unit_eqns_parse_as_telemetry_messages():
    parm = aprslib.parse(pub.build_parm_packet(pub.DEFAULT_CALLSIGN))
    unit = aprslib.parse(pub.build_unit_packet(pub.DEFAULT_CALLSIGN))
    eqns = aprslib.parse(pub.build_eqns_packet(pub.DEFAULT_CALLSIGN))
    bits = aprslib.parse(pub.build_bits_packet(pub.DEFAULT_CALLSIGN))

    assert parm["format"] == "telemetry-message"
    assert unit["format"] == "telemetry-message"
    assert eqns["format"] == "telemetry-message"
    assert bits["format"] == "telemetry-message"

    assert parm["tPARM"][:5] == pub.TELEMETRY_PARM
    assert unit["tUNIT"][:5] == pub.TELEMETRY_UNIT
    assert eqns["tEQNS"] == [[0, 0.1, 0], [0, 0.01, 0], [0, 1, 0], [0, 1, 0], [0, 0.1, 0]]


def test_metadata_addressed_to_own_callsign():
    # Backend's _owner_callsign() keys config on the message addressee, so the
    # metadata must be self-addressed to VN2EWS-1 for the labels to attach.
    parm = aprslib.parse(pub.build_parm_packet(pub.DEFAULT_CALLSIGN))
    assert parm["addresse"].strip() == "VN2EWS-1"


def test_source_callsign_and_display_name_placement():
    wx = pub.build_wx_packet(
        pub.DEFAULT_CALLSIGN, pub.DEFAULT_LAT, pub.DEFAULT_LON,
        temp_c=25.0, humidity=50, pressure_mbar=1000.0,
    )
    parsed = aprslib.parse(wx)
    assert parsed["from"] == "VN2EWS-1"
    # 'Veno AWS' is the display name and must live in the comment, not the call.
    assert "Veno AWS" in parsed["comment"]


def test_end_to_end_through_backend_ingest():
    """Feed the publisher's own packets through the backend ingest pipeline and
    confirm weather + named telemetry come out the far end on the map broadcast."""
    import app.db as db
    from app.mqtt_ingest import process_parsed_packet

    db.init_db(":memory:")

    # Register metadata (PARM/UNIT/EQNS) so telemetry resolves to named/units.
    for builder in (pub.build_parm_packet, pub.build_unit_packet, pub.build_eqns_packet):
        process_parsed_packet(aprslib.parse(builder(pub.DEFAULT_CALLSIGN)), lambda p: None)

    broadcasts = []

    # Weather frame.
    wx = pub.build_wx_packet(
        pub.DEFAULT_CALLSIGN, pub.DEFAULT_LAT, pub.DEFAULT_LON,
        temp_c=27.8, humidity=63, pressure_mbar=985.5,
    )
    process_parsed_packet(aprslib.parse(wx), broadcasts.append)

    # Telemetry frame: CPUTemp 55.0 degC (raw 550), Vin 0.87 V (raw 87),
    # load 34 %, mem 45 %, uptime 12.3 h (raw 123).
    tel = pub.build_telemetry_packet(
        pub.DEFAULT_CALLSIGN, pub.DEFAULT_LAT, pub.DEFAULT_LON,
        seq=1, analog_raw=[550, 87, 34, 45, 123], bits="00000000",
    )
    process_parsed_packet(aprslib.parse(tel), broadcasts.append)

    assert len(broadcasts) == 2
    assert broadcasts[0]["weather"]["humidity"] == 63
    named = broadcasts[1]["telemetry"]
    assert named["CPUTemp"]["value"] == pytest.approx(55.0, abs=0.1)
    assert named["CPUTemp"]["unit"] == "degC"
    assert named["Vin"]["value"] == pytest.approx(0.87, abs=0.01)
    assert named["Uptime"]["value"] == pytest.approx(12.3, abs=0.1)

    db.init_db(":memory:")  # reset shared module-level connection
