# Requirements: LoRa APRS Live Map (aprs.fi-like)

## Summary
A standalone web application that shows live positions of amateur radio LoRa APRS stations on a map, inspired by aprs.fi. Positions arrive over MQTT from a LILYGO T-Beam running the CA2RXU/richonguzman `LoRa_APRS_iGate` firmware, parsed from TNC2-format APRS packets, stored for history, and pushed to the browser in real time over WebSocket. Built and tested entirely locally/standalone — the real Mosquitto broker lives on the user's own Ubuntu 24.04 Proxmox VM, which this build environment cannot reach. Deployment to that VM is a manual step the user performs later using provided scripts/docs, not something this build executes.

## Assumptions (stated explicitly, since the user did not confirm every detail)
- **Feature scope for v1** is: live map, track history, station list with search, extra telemetry display. Multi-user/login is explicitly OUT for v1 (user flagged it as not mandatory; default to excluding it to keep v1 simple).
- **MQTT topic structure**: `<mqtt_topic_prefix>/<SENDER_CALLSIGN>`, prefix is configurable, payload is plain-text TNC2 APRS, per the firmware source the user confirmed (`src/mqtt_utils.cpp`).
- **Direwolf integration** (Raspberry Pi, no native MQTT) is an open decision point, not required for v1 core app. Recommended default: a small separate Python bridge script that reads Direwolf's AGW TCP port (or its APRS-IS feed) and republishes parsed packets onto the same MQTT topic convention as the iGate, so the core app only ever needs to understand one input format. This bridge is a separate, later task pending user confirmation — it must not block or complicate the core backend design.
- **Track history lookback** defaults to 24 hours, configurable.
- **No authentication** in v1; the live map is a single shared public view. Auth is deferred to a later addition behind FastAPI middleware, not designed now.
- **Storage** is SQLite, chosen per the user's explicit request between SQLAlchemy and plain sqlite3 — left as a design-time choice, not a requirements-time choice.

## Functional Requirements
1. Backend connects to an MQTT broker (host/port/username/password configurable) using `paho-mqtt`, subscribing to `<mqtt_topic_prefix>/#` where the prefix is externally configurable (never hardcoded).
2. Backend parses each received payload as a TNC2 APRS packet using `aprslib.parse()`.
3. On successful parse, the backend extracts callsign, timestamp, latitude/longitude, and when present: course, speed, altitude, comment, and telemetry (e.g. battery voltage).
4. On parse failure, the backend logs the error and the raw payload, and continues listening — it must never crash or stop the MQTT loop.
5. Every successfully parsed position is persisted (raw packet text, parsed fields, callsign, timestamp) to SQLite, enabling full history replay, not just "latest position."
6. The backend exposes a FastAPI WebSocket endpoint that broadcasts each new position to connected clients as it arrives (no polling from the frontend).
7. The backend exposes REST endpoint(s) sufficient for the frontend to: load the current known stations and their last position on initial page load, and load a station's position history for a configurable lookback window (default 24h).
8. The frontend renders a Leaflet.js map with an OpenStreetMap tile layer (no API key) showing a marker per known station at its last known position.
9. The frontend updates markers live as new positions arrive via the WebSocket connection, without a page reload.
10. Clicking/tapping a station marker shows a popup with: callsign, last-heard time, lat/lon, course/speed/altitude if present, comment, and the raw packet text.
11. The frontend provides a sidebar listing all known stations with a search/filter box by callsign.
12. The frontend can display a station's track history as a polyline on the map, toggle-able per station, for the configured lookback window.
13. The frontend displays available extra telemetry (battery voltage, speed, altitude, course) per station when present in the parsed data.
14. The MQTT client auto-reconnects on disconnect without requiring an app restart.
15. All broker connection details and the topic prefix are set via `.env` or `config.yaml`, not hardcoded, since the real values only exist on the user's remote VM.

## Non-Functional Requirements
- **Local-only verification**: this build environment has no network/SSH access to the user's Ubuntu/Proxmox VM or a real Mosquitto broker. All functional verification must be done against a local/standalone MQTT broker or simulated/replayed packets — never against a real remote host.
- **Configurability**: broker host, port, username, password, and MQTT topic prefix must all be externally configurable and documented, with no secrets or environment-specific values committed to source.
- **Resilience**: MQTT parse failures and disconnects must degrade gracefully (log + continue / auto-reconnect), never crash the backend process.
- **No frontend build toolchain**: frontend is plain HTML/CSS/JS served as static files by FastAPI; no bundler/transpiler step.
- **Deployment artifacts, not deployment actions**: a `deploy/` folder with a README and systemd unit must be produced so the user can self-deploy on their VM later; this build must not attempt to execute, SSH into, or otherwise reach that VM.

## Acceptance Criteria
1. Given a locally-run MQTT broker publishing a valid TNC2 packet to `<prefix>/<CALLSIGN>`, the backend parses it, stores it in SQLite, and broadcasts it over the WebSocket endpoint.
2. Given a malformed/non-APRS payload published to the subscribed topic, the backend logs the failure and the MQTT listener continues running (verified by a subsequent valid packet still being processed).
3. Given the backend is configured via `.env`/`config.yaml` with different broker host/port/topic-prefix values, no code changes are needed to point it at a different broker.
4. Given the MQTT broker connection drops and comes back, the backend reconnects automatically without manual restart.
5. Opening the frontend in a browser shows a Leaflet/OSM map with markers for all stations that have at least one stored position.
6. While the frontend is open, a new MQTT position for a known or new station appears/moves on the map without a page reload.
7. Clicking a station marker shows a popup containing callsign, last-heard time, coordinates, and (when present) course/speed/altitude, comment, and raw packet text.
8. Typing a callsign fragment into the sidebar search filters the station list accordingly.
9. Toggling "show track" for a station draws a polyline of its stored positions within the configured lookback window; toggling off removes it.
10. No login/auth is required to view the map, and no auth UI is present in v1.
11. The `deploy/` folder contains a README describing Mosquitto install + minimal secure config, Python3/venv/app-dependency install, and a systemd unit file for running the app via uvicorn — none of these steps are executed by the build itself.
12. The repository/document set includes an explicit note that the Direwolf→MQTT bridge is an open, separate task with a recommended default design, not implemented as part of this v1 core app.

## Out of Scope
- Multi-user accounts / login / authentication (v1).
- Implementing or testing the Direwolf-to-MQTT bridge (documented as a future task with a recommended default only).
- Any live connection, SSH session, or deployment action against the user's actual Ubuntu/Proxmox VM or real Mosquitto broker.
- Mobile app or native client; this is a web-only frontend.
- Historical data import/migration from any existing aprs.fi-style data source.
