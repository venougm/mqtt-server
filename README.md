# LoRa APRS Live Map

A standalone web app that shows live positions of amateur radio LoRa APRS
stations on a map, inspired by aprs.fi. Positions arrive over MQTT (from a
LILYGO T-Beam running the CA2RXU/richonguzman `LoRa_APRS_iGate` firmware, or
any other source publishing the same TNC2-over-MQTT convention), get parsed
with `aprslib`, stored in SQLite for history, and pushed to the browser in
real time over WebSocket.

Single FastAPI process: a background MQTT client ingests packets, a REST API
answers "what do we already know," and a WebSocket pushes "what's new right
now." The frontend is plain HTML/CSS/JS with Leaflet.js and OpenStreetMap
tiles -- no build toolchain, no API key.

APRS weather (WX) reports are supported: whatever `aprslib` parses into
`weather` (temperature °C, humidity %, pressure mbar, wind, rain, luminosity)
is stored as JSON in `packets.weather_json`, returned as `weather` by
`GET /api/stations` and the WebSocket feed, and shown in a "Weather" section
of the station popup. Existing databases gain the `weather_json` column
automatically on startup.

## Setup

Install Python 3.11+ (on Windows, if you don't already have a working
`python`/`pip`, run `winget install Python.Python.3.11` and open a fresh
terminal afterward):

```powershell
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
```

## Configuration

```powershell
copy config.yaml.example config.yaml
copy .env.example .env
```

Edit `.env` with your real Mosquitto broker host/port/credentials and your
MQTT topic prefix (e.g. `aprs`; the backend subscribes to `<prefix>/#`).
`MQTT_BROKER_HOST` and `MQTT_TOPIC_PREFIX` are required -- the app will refuse
to start without them. Edit `config.yaml` for non-secret settings (history
lookback window, log level, bind host/port, MQTT client ID).

## Running

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/` in a browser.

## Weather charts

Each weather station has an aprs.fi-style charts page at
`/weather/a/<callsign>` (e.g. `http://localhost:8000/weather/a/YG2UFH-10`),
linked from the "Show weather charts" link in the map popup. It shows current
conditions, min/max/latest per field, and line charts (temperature, humidity,
pressure, wind, rain, luminosity -- only fields the station reports) for the
last 24 h / 48 h / 7 d / 30 d, and appends new reports live. Chart.js 4.4.1 is
loaded from the jsDelivr CDN. The data comes from
`GET /api/stations/{callsign}/weather?hours=N` (1-720, default 48).

### Importing history from aprs.fi

Charts only cover packets this server has received. To backfill your own
station's earlier reports, open its raw-packets page on aprs.fi in a browser,
copy the lines into a text file (one per line, as shown there), e.g.

```
2026-10-06 04:24:10 WIB: YG2UFH-10>APLRG1,TCPIP*,qAC,T2CSNGRAD:=LRDS_jEH8_ !G.../...g...t082h63b09855IGate LilyGo TBeam Lora
```

and run, from the project root:

```powershell
.venv\Scripts\python.exe tools\import_aprsfi_raw.py packets.txt
# or from stdin:
Get-Content packets.txt | .venv\Scripts\python.exe tools\import_aprsfi_raw.py
```

The leading timestamp is the packet's receive time (timezones WIB, WITA, WIT,
UTC/GMT/Z; other abbreviations are rejected). Packets go into the database set
by `DB_PATH` in your config, through the same parser as live MQTT packets.
Re-importing the same lines skips exact duplicates, and older packets never
replace a station's latest position on the map. Blank lines and lines starting
with `#` are ignored. The tool prints imported / duplicates skipped / parse
failures and exits with code 1 if any line failed. It never downloads anything
from aprs.fi; copy the lines by hand.

## Running tests

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

This runs the parser unit tests, telemetry-equation unit tests, and the
pipeline-injection integration test -- all without needing a real MQTT
broker. `tests/manual_mqtt_replay.py` is an optional manual script for a true
end-to-end check if you have a local Mosquitto broker available; it is not
part of the automated suite.

## Hosting: VPS/VM required, not static/serverless hosting

This app needs a host that keeps a persistent process running: a background
MQTT client with a long-lived outbound connection to your broker, plus an
ASGI server handling REST requests and inbound WebSocket connections. Your
own Ubuntu 24.04 / Proxmox VM is an appropriate target. Typical static-site
hosting or serverless/FaaS platforms will not work, since they don't keep a
process running continuously. See `deploy/README.md` for step-by-step
deployment instructions (Mosquitto install, venv setup, systemd unit) --
those steps are documentation only and are not executed by this build.

## Known v1 limitations

- No authentication -- the live map is a single shared public view.
- Telemetry for a station with no `EQNS`/`UNIT`/`PARM` config received yet is
  shown as unlabeled raw values until a config message arrives (no
  retroactive relabeling of already-stored packets).
- Weather charts only cover packets stored locally (live or imported with
  `tools/import_aprsfi_raw.py`). Only a small set of APRS symbols have dedicated icons
  (overlay symbols such as `L_` render as the overlay letter on a circle).
- A station's track polyline reflects the lookback window at toggle time, not
  continuously refreshed.
- The Direwolf -> MQTT bridge (for the Raspberry Pi + Direwolf hardware path)
  is a documented future task, not implemented here -- see
  `deploy/README.md`'s "Direwolf -> MQTT bridge" section for the recommended
  default design.
