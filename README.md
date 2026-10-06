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
- A station's track polyline reflects the lookback window at toggle time, not
  continuously refreshed.
- The Direwolf -> MQTT bridge (for the Raspberry Pi + Direwolf hardware path)
  is a documented future task, not implemented here -- see
  `deploy/README.md`'s "Direwolf -> MQTT bridge" section for the recommended
  default design.
