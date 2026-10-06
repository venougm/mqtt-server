"""Weather charts feature: GET /api/stations/{callsign}/weather, the
/weather/a/{callsign} page route, and the aprs.fi raw-line backfill importer
(historical received_at, forward-only stations upsert, duplicate skipping)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

REAL_LINE = (
    "2026-10-06 04:24:10 WIB: YG2UFH-10>APLRG1,TCPIP*,qAC,T2CSNGRAD:"
    "=LRDS_jEH8_ !G.../...g...t082h63b09855IGate LilyGo TBeam Lora"
)


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


def _store(db, callsign, received_at, weather=None, raw=None):
    db.store_packet(
        {
            "from": callsign,
            "raw_packet": raw or f"{callsign}>APRS:{received_at}",
            "weather_json": json.dumps(weather) if weather is not None else None,
        },
        received_at=received_at,
    )


# ---- weather endpoint -------------------------------------------------------

def test_weather_endpoint_window_order_and_nulls(fresh_db, client):
    _store(fresh_db, "WX-1", _ago(50), {"temperature": 20.0})
    _store(fresh_db, "WX-1", _ago(1), {"temperature": 25.5, "humidity": 60})
    _store(fresh_db, "WX-1", _ago(2))  # position only, no weather
    _store(fresh_db, "WX-1", _ago(10), {"temperature": 22.0, "pressure": 1001.2, "wind_speed": 3.1})
    _store(fresh_db, "OTHER", _ago(1), {"temperature": 99.0})

    res = client.get("/api/stations/WX-1/weather")  # default 48 h
    assert res.status_code == 200
    body = res.json()
    assert [p["temperature"] for p in body] == [22.0, 25.5]  # ascending, 50 h-old row excluded
    assert body[0]["pressure"] == 1001.2
    assert body[0]["wind_speed"] == 3.1
    assert body[0]["humidity"] is None
    assert body[1]["humidity"] == 60
    expected_keys = {
        "received_at", "temperature", "humidity", "pressure", "wind_direction",
        "wind_speed", "wind_gust", "rain_1h", "rain_24h", "rain_since_midnight", "luminosity",
    }
    assert all(set(p) == expected_keys for p in body)
    assert body[1]["luminosity"] is None and body[1]["rain_1h"] is None

    wider = client.get("/api/stations/WX-1/weather", params={"hours": 72}).json()
    assert [p["temperature"] for p in wider] == [20.0, 22.0, 25.5]


def test_weather_endpoint_unknown_callsign_returns_empty(client):
    res = client.get("/api/stations/NOPE-1/weather")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.parametrize("hours", [0, 721, -5])
def test_weather_endpoint_rejects_out_of_range_hours(client, hours):
    res = client.get("/api/stations/WX-1/weather", params={"hours": hours})
    assert res.status_code == 422


# ---- page route -----------------------------------------------------------------

def test_weather_page_route_serves_html(client):
    res = client.get("/weather/a/YG2UFH-10")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "/js/weather.js" in res.text
    assert "chart.js@4.4.1" in res.text

    assert client.get("/").status_code == 200
    assert client.get("/js/weather.js").status_code == 200


# ---- backfill: stations upsert is forward-only ---------------------------------

def _station_row(db, callsign):
    cur = db._conn.execute(
        "SELECT first_heard_at, last_heard_at, last_packet_id FROM stations WHERE callsign = ?",
        (callsign,),
    )
    return cur.fetchone()


def test_backfill_older_packet_does_not_move_last_heard_back(fresh_db):
    fresh_db.store_packet({"from": "BF-1", "raw_packet": "BF-1>APRS:live"})  # live, now
    first, last, last_id = _station_row(fresh_db, "BF-1")

    older = _ago(5)
    _store(fresh_db, "BF-1", older, {"temperature": 21.0}, raw="BF-1>APRS:old")
    first2, last2, last_id2 = _station_row(fresh_db, "BF-1")
    assert (last2, last_id2) == (last, last_id)
    assert first2 == older

    [station] = fresh_db.get_stations()
    assert station["raw_packet"] == "BF-1>APRS:live"

    # A backfilled packet newer than the stored latest does advance it.
    newer = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="microseconds")
    _store(fresh_db, "BF-1", newer, raw="BF-1>APRS:newer")
    first3, last3, last_id3 = _station_row(fresh_db, "BF-1")
    assert first3 == older
    assert last3 == newer
    assert last_id3 != last_id


def test_live_ingestion_still_updates_latest(fresh_db):
    fresh_db.store_packet({"from": "LV-1", "raw_packet": "LV-1>APRS:a"})
    fresh_db.store_packet({"from": "LV-1", "raw_packet": "LV-1>APRS:b"})
    [station] = fresh_db.get_stations()
    assert station["raw_packet"] == "LV-1>APRS:b"


# ---- importer -------------------------------------------------------------------

def test_parse_line_wib_and_other_zones():
    from tools.import_aprsfi_raw import parse_line

    received_at, packet = parse_line(REAL_LINE)
    assert received_at == "2026-10-05T21:24:10.000000+00:00"
    assert packet.startswith("YG2UFH-10>APLRG1,TCPIP*,qAC,T2CSNGRAD:=LRDS_jEH8_")
    assert packet.endswith("IGate LilyGo TBeam Lora")

    assert parse_line("2026-10-06 04:24:10 WITA: X>Y:>a")[0] == "2026-10-05T20:24:10.000000+00:00"
    assert parse_line("2026-10-06 04:24:10 WIT: X>Y:>a")[0] == "2026-10-05T19:24:10.000000+00:00"
    assert parse_line("2026-10-06 04:24:10 UTC: X>Y:>a")[0] == "2026-10-06T04:24:10.000000+00:00"
    assert parse_line("2026-10-06 04:24:10 GMT: X>Y:>a")[0] == "2026-10-06T04:24:10.000000+00:00"
    assert parse_line("2026-10-06 04:24:10 Z: X>Y:>a")[0] == "2026-10-06T04:24:10.000000+00:00"


@pytest.mark.parametrize(
    "line, reason",
    [
        ("2026-10-06 04:24:10 PST: X>Y:>a", "unknown timezone abbreviation 'PST'"),
        ("YG2UFH-10>APLRG1:>no timestamp", "expected"),
        ("2026-13-40 04:24:10 WIB: X>Y:>a", "invalid date/time"),
    ],
)
def test_parse_line_rejects_bad_lines(line, reason):
    from tools.import_aprsfi_raw import parse_line

    with pytest.raises(ValueError, match=reason):
        parse_line(line)


def test_import_lines_stores_history_and_skips_duplicates(fresh_db):
    from tools.import_aprsfi_raw import import_lines

    lines = [
        "# comment line\n",
        REAL_LINE + "\n",
        "\n",
        "2026-10-06 04:24:10 PST: YG2UFH-10>APLRG1:>bad zone\n",
    ]
    out = io.StringIO()
    summary = import_lines(lines, out=out)
    assert summary == {"imported": 1, "duplicates": 0, "failures": 1}
    assert "unknown timezone abbreviation 'PST'" in out.getvalue()

    again = import_lines([REAL_LINE + "\n"], out=io.StringIO())
    assert again == {"imported": 0, "duplicates": 1, "failures": 0}

    rows = fresh_db._conn.execute(
        "SELECT callsign, received_at, weather_json FROM packets"
    ).fetchall()
    assert len(rows) == 1
    callsign, received_at, weather_json = rows[0]
    assert callsign == "YG2UFH-10"
    assert received_at == "2026-10-05T21:24:10.000000+00:00"
    weather = json.loads(weather_json)
    assert weather["temperature"] == pytest.approx(27.8, abs=0.05)
    assert weather["humidity"] == 63
    assert weather["pressure"] == pytest.approx(985.5)

    [station] = fresh_db.get_stations()
    assert station["received_at"] == "2026-10-05T21:24:10.000000+00:00"
