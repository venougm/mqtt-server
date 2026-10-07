"""Telemetry charts feature: GET /api/stations/{callsign}/telemetry and the
/telemetry/a/{callsign} page route.

Fixtures are built by feeding telemetry packets (and PARM/UNIT/EQNS metadata)
through the real ingest pipeline (app.mqtt_ingest.process_parsed_packet) into an
in-memory DB, the same approach as tests/test_telemetry.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import aprslib
import pytest
from fastapi.testclient import TestClient

from app.mqtt_ingest import process_parsed_packet


@pytest.fixture()
def fresh_db():
    import app.db as db

    db.init_db(":memory:")
    yield db


@pytest.fixture()
def client(fresh_db):
    from app.main import app

    # No `with` block: the lifespan (real MQTT connect, configured DB) is not run.
    return TestClient(app)


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="microseconds")


def _noop_broadcast(_payload):
    pass


def _register_config(callsign: str, eqns: str, unit: str, parm: str) -> None:
    """Feed EQNS/UNIT/PARM telemetry-message packets through the ingest pipeline
    so a telemetry_config row is created exactly as live ingestion would."""
    addr = callsign.ljust(9)
    for body in (f"EQNS.{eqns}", f"UNIT.{unit}", f"PARM.{parm}"):
        raw = f"{callsign}>APRS,TCPIP*::{addr}:{body}".encode()
        process_parsed_packet(aprslib.parse(raw), _noop_broadcast)


def _store_raw_telemetry(db, callsign: str, received_at: str, vals: list[int], seq: int = 1) -> None:
    """Store a position+telemetry packet's telemetry_json via the ingest path's
    _build_telemetry_json, then write the row with an explicit historical time.

    aprslib's telemetry encoding is awkward to synthesize by hand, so this
    builds the normalized telemetry_json the same way mqtt_ingest does
    (named shape if a usable config exists for the callsign now, raw fallback
    otherwise) and stores it with the given received_at."""
    from app.mqtt_ingest import _build_telemetry_json

    parsed = {
        "from": callsign,
        "telemetry": {"seq": seq, "vals": vals, "bits": "00000000"},
    }
    telemetry_json = _build_telemetry_json(parsed)
    db.store_packet(
        {
            "from": callsign,
            "raw_packet": f"{callsign}>APRS:tlm{seq}",
            "telemetry_json": telemetry_json,
        },
        received_at=received_at,
    )


# ---- telemetry endpoint: window + ordering ----------------------------------

def test_telemetry_endpoint_window_order_and_fallback(fresh_db, client):
    # No config -> raw fallback, generic Analog1..5 names, raw values returned.
    _store_raw_telemetry(fresh_db, "TLM-1", _ago(50), [1, 2, 3, 4, 5])  # outside 48h
    _store_raw_telemetry(fresh_db, "TLM-1", _ago(10), [10, 20, 30, 40, 50], seq=2)
    _store_raw_telemetry(fresh_db, "TLM-1", _ago(1), [11, 21, 31, 41, 51], seq=3)
    _store_raw_telemetry(fresh_db, "OTHER", _ago(1), [99, 99, 99, 99, 99])

    res = client.get("/api/stations/TLM-1/telemetry")  # default 48 h
    assert res.status_code == 200
    body = res.json()

    names = [c["name"] for c in body["channels"]]
    assert names == ["Analog1", "Analog2", "Analog3", "Analog4", "Analog5"]
    assert all(c["unit"] == "" for c in body["channels"])

    pts = body["points"]
    # 50h-old row excluded; ascending order (10h then 1h).
    assert [p["channels"]["Analog1"] for p in pts] == [10, 11]
    assert pts[0]["channels"]["Analog5"] == 50
    assert pts[0]["raw_vals"] == [10, 20, 30, 40, 50]

    wider = client.get("/api/stations/TLM-1/telemetry", params={"hours": 72}).json()
    assert [p["channels"]["Analog1"] for p in wider["points"]] == [1, 10, 11]


# ---- telemetry endpoint: EQNS applied, named + unit-correct -----------------

def test_telemetry_endpoint_applies_eqns_named_and_units(fresh_db, client):
    # ch1 CPUTemp = 0.5*x (eqn 0,0.5,0); ch2 Vin = 0.01*x; rest identity.
    _register_config(
        "VN2EWS-1",
        eqns="0,0.5,0,0,0.01,0,0,1,0,0,1,0,0,1,0",
        unit="degC,V,pct,pct,hr",
        parm="CPUTemp,Vin,CPULoad,MemUsed,Uptime",
    )
    # Config exists now, so ingest writes the named shape; apply_equations at
    # ingest time gives CPUTemp=0.5*94=47.0, Vin=0.01*508=5.08.
    _store_raw_telemetry(fresh_db, "VN2EWS-1", _ago(2), [94, 508, 12, 34, 120], seq=1)
    _store_raw_telemetry(fresh_db, "VN2EWS-1", _ago(1), [96, 510, 15, 36, 121], seq=2)

    body = client.get("/api/stations/VN2EWS-1/telemetry").json()
    chans = {c["name"]: c["unit"] for c in body["channels"]}
    assert chans == {"CPUTemp": "degC", "Vin": "V", "CPULoad": "pct", "MemUsed": "pct", "Uptime": "hr"}

    pts = body["points"]
    assert [p["channels"]["CPUTemp"] for p in pts] == pytest.approx([47.0, 48.0])
    assert pts[0]["channels"]["Vin"] == pytest.approx(5.08)
    assert pts[1]["channels"]["Uptime"] == pytest.approx(121.0)


def test_telemetry_endpoint_eqns_applied_to_raw_stored_before_config(fresh_db, client):
    """A raw-shape row stored before the config arrives must still be labelled
    and EQNS-applied by the endpoint using the station's CURRENT config."""
    # Store raw first (no config yet -> raw fallback).
    _store_raw_telemetry(fresh_db, "LATE-1", _ago(3), [40, 500, 10, 20, 100], seq=1)

    # Config arrives afterwards.
    _register_config(
        "LATE-1",
        eqns="0,0.5,0,0,0.01,0,0,1,0,0,1,0,0,1,0",
        unit="degC,V,pct,pct,hr",
        parm="CPUTemp,Vin,CPULoad,MemUsed,Uptime",
    )

    body = client.get("/api/stations/LATE-1/telemetry").json()
    chans = {c["name"]: c["unit"] for c in body["channels"]}
    assert chans["CPUTemp"] == "degC"
    pt = body["points"][0]
    assert pt["channels"]["CPUTemp"] == pytest.approx(20.0)  # 0.5*40
    assert pt["channels"]["Vin"] == pytest.approx(5.0)  # 0.01*500
    assert pt["raw_vals"] == [40, 500, 10, 20, 100]


# ---- edge cases -------------------------------------------------------------

def test_telemetry_endpoint_unknown_callsign_returns_empty(client):
    res = client.get("/api/stations/NOPE-1/telemetry")
    assert res.status_code == 200
    body = res.json()
    assert body["points"] == []
    assert [c["name"] for c in body["channels"]] == [
        "Analog1", "Analog2", "Analog3", "Analog4", "Analog5",
    ]


@pytest.mark.parametrize("hours", [0, 721, -5])
def test_telemetry_endpoint_rejects_out_of_range_hours(client, hours):
    res = client.get("/api/stations/TLM-1/telemetry", params={"hours": hours})
    assert res.status_code == 422


# ---- page route -------------------------------------------------------------

def test_telemetry_page_route_serves_html(client):
    res = client.get("/telemetry/a/VN2EWS-1")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "/js/telemetry.js" in res.text
    assert "chart.js@4.4.1" in res.text

    assert client.get("/").status_code == 200
    assert client.get("/js/telemetry.js").status_code == 200
