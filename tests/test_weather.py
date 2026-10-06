"""APRS weather (WX) support: ingest -> storage -> API/broadcast, plus the
in-place `packets.weather_json` migration for DBs created by the old schema."""

from __future__ import annotations

import json
import sqlite3

import pytest

# Reconstructed from YG2UFH-10's aprs.fi page (position, 36.7 °C, 37 %,
# 982.3 mbar, comment), sent by the project's LoRa iGate firmware.
YG2UFH_PACKET = (
    b"YG2UFH-10>APLRG1,TCPIP*,qAC,T2CSNGRAD:"
    b"!0742.47SL11024.60E_.../...g...t098h37b09823IGate LilyGo TBeam Lora"
)

# Real raw packet for YG2UFH-10 as shown on aprs.fi (compressed position +
# weather, symbol table 'L' overlay on '_').
YG2UFH_REAL_PACKET = (
    b"YG2UFH-10>APLRG1,TCPIP*,qAC,T2CSNGRAD:"
    b"=LRDS_jEH8_ !G.../...g...t082h63b09855IGate LilyGo TBeam Lora"
)


@pytest.fixture()
def fresh_db():
    import app.db as db

    db.init_db(":memory:")
    yield db


def test_weather_packet_stored_returned_and_broadcast(fresh_db):
    from app.mqtt_ingest import on_message_impl

    broadcasts = []
    on_message_impl("aprs-igate/YG2UFH-10", YG2UFH_PACKET, broadcasts.append)

    assert len(broadcasts) == 1
    payload = broadcasts[0]
    assert payload["callsign"] == "YG2UFH-10"
    assert payload["symbol"] == "L_"
    assert payload["comment"] == "IGate LilyGo TBeam Lora"
    assert payload["telemetry"] is None
    weather = payload["weather"]
    assert weather["temperature"] == pytest.approx(36.7, abs=0.05)
    assert weather["humidity"] == 37
    assert weather["pressure"] == pytest.approx(982.3)

    stations = fresh_db.get_stations()
    assert len(stations) == 1
    row = stations[0]
    assert row["callsign"] == "YG2UFH-10"
    assert row["latitude"] == pytest.approx(-7.7078, abs=1e-3)
    assert row["longitude"] == pytest.approx(110.41, abs=1e-3)
    assert json.loads(row["weather_json"]) == weather


def test_real_compressed_weather_packet(fresh_db):
    from app.mqtt_ingest import on_message_impl

    broadcasts = []
    on_message_impl("aprs-igate/YG2UFH-10", YG2UFH_REAL_PACKET, broadcasts.append)

    assert len(broadcasts) == 1
    payload = broadcasts[0]
    assert payload["symbol"] == "L_"
    assert payload["comment"] == "IGate LilyGo TBeam Lora"
    assert payload["course"] is None and payload["speed"] is None
    assert payload["weather"]["temperature"] == pytest.approx(27.8, abs=0.05)
    assert payload["weather"]["humidity"] == 63
    assert payload["weather"]["pressure"] == pytest.approx(985.5)

    [row] = fresh_db.get_stations()
    assert row["callsign"] == "YG2UFH-10"
    assert row["latitude"] == pytest.approx(-7.70776, abs=1e-4)
    assert row["longitude"] == pytest.approx(110.41006, abs=1e-4)
    assert json.loads(row["weather_json"]) == payload["weather"]


def test_stations_api_returns_weather(fresh_db):
    from app.mqtt_ingest import on_message_impl
    from app.routers.stations import get_stations_route

    on_message_impl("aprs-igate/YG2UFH-10", YG2UFH_PACKET, lambda p: None)

    [station] = get_stations_route()
    assert station.weather is not None
    assert station.weather["humidity"] == 37


def test_init_db_migrates_old_schema_without_weather_json(tmp_path):
    import app.db as db

    db_path = str(tmp_path / "old.db")
    old = sqlite3.connect(db_path)
    old.executescript(
        """
        CREATE TABLE packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            callsign TEXT NOT NULL, received_at TEXT NOT NULL,
            raw_packet TEXT NOT NULL, latitude REAL, longitude REAL,
            course REAL, speed REAL, altitude REAL, comment TEXT,
            symbol TEXT, telemetry_json TEXT
        );
        INSERT INTO packets (callsign, received_at, raw_packet)
        VALUES ('OLD-1', '2024-01-01T00:00:00+00:00', 'OLD-1>APRS:>x');
        """
    )
    old.commit()
    old.close()

    db.init_db(db_path)
    db._conn.close()
    db.init_db(db_path)  # second run must be a no-op, not a duplicate-column error

    check = sqlite3.connect(db_path)
    columns = {r[1] for r in check.execute("PRAGMA table_info(packets)")}
    rows = check.execute("SELECT callsign, weather_json FROM packets").fetchall()
    check.close()
    db._conn.close()
    db.init_db(":memory:")

    assert "weather_json" in columns
    assert rows == [("OLD-1", None)]
