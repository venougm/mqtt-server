# Verification report

This records exactly what was run and observed on this Windows build machine,
per design.md's "Local verification strategy" / "Testability summary".

## 1. Python toolchain

- `python --version` initially failed: `python.exe` resolved via `Get-Command`
  but was a non-functional Windows "App Execution Alias" stub (confirmed live,
  matching the design's own documented finding for this machine).
- `winget --version` succeeded (`v1.29.380`) -- functional, not just
  name-resolvable.
- Ran: `winget install --silent --accept-package-agreements
  --accept-source-agreements Python.Python.3.11` -> succeeded, installed
  Python 3.11.9 to `%LOCALAPPDATA%\Programs\Python\Python311`.
- The `python` App Execution Alias stub still shadows bare `python` on PATH in
  interactive PowerShell sessions opened by this tool (a PATH-ordering/alias
  quirk, not a missing install) -- confirmed Python itself is fully functional
  by invoking the installer's absolute path directly
  (`%LOCALAPPDATA%\Programs\Python\Python311\python.exe --version` ->
  `Python 3.11.9`). All subsequent commands used this interpreter (directly,
  or via the project's `.venv`, whose `.venv\Scripts\python.exe` resolves
  correctly without PATH ambiguity) -- this does not affect anything the
  design's verification plan actually calls for, since every prescribed verify
  command in `plan.md` already targets `.venv\Scripts\python.exe` explicitly.
- The WSL2 fallback branch was never reached (winget succeeded first), but was
  also probed for completeness: `Get-Command wsl` resolves, but `wsl --status`
  exits 50 ("not installed") -- present-on-PATH-only, non-functional, exactly
  as the design's own authoring-machine notes predicted.

## 2. Dependencies

- Created `.venv` via `python -m venv .venv`.
- Ran `.venv\Scripts\pip.exe install -r requirements.txt` -- exit code 0, all
  pinned packages (fastapi==0.115.0, uvicorn[standard]==0.32.0,
  paho-mqtt==1.6.1, aprslib==0.7.2, python-dotenv==1.0.1, PyYAML==6.0.2,
  pytest==8.3.3, httpx==0.27.2) installed successfully with no errors.

## 3. Unit / integration tests

Ran `.venv\Scripts\python.exe -m pytest tests/ -v` from the project root:

```
17 passed in 0.61s
```

Breakdown:
- `tests/test_aprs_parse.py` -- 7 passed (basic position, course/speed+altitude,
  comment, telemetry vals, telemetry-config-message, two malformed fixtures
  returning None with no exception). Includes the speed-unit regression
  assertion against aprslib's own unmodified `parsed['speed']`.
- `tests/test_telemetry.py` -- 3 passed: `apply_equations()` against a known
  [a,b,c] triple; a full EQNS+UNIT+PARM-then-position sequence through
  `process_parsed_packet` against an in-memory DB producing the named/
  unit-correct telemetry shape; a position-with-telemetry packet for a
  callsign with no prior config producing the `raw_seq`/`raw_vals` fallback.
- `tests/test_ingest_pipeline.py` -- 7 passed: valid position packet lands in
  `packets`+`stations` and produces exactly one broadcast call with the full
  WebSocket contract key set (`type, callsign, received_at, latitude,
  longitude, course, speed, altitude, comment, symbol, telemetry, raw_packet`);
  a telemetry-config-message packet produces zero `packets`/`stations` rows and
  zero broadcasts; a telemetry position packet's broadcast payload always
  includes the `telemetry` key (fallback shape, since no config was
  registered in that test); both malformed fixtures produce zero rows and zero
  broadcasts; the full `on_message_impl` wrapper (topic + raw bytes in) was
  exercised directly for both a valid and a malformed packet with no exception
  propagating in either case.

Each test file was also run individually during development (same results) to
confirm no cross-file ordering dependency.

## 4. FastAPI app: root route + WebSocket handshake

With `MQTT_BROKER_HOST=localhost`, `MQTT_TOPIC_PREFIX=aprs`, `DB_PATH=:memory:`:

- `fastapi.testclient.TestClient(app)` inside a `with` block (so the
  `lifespan` startup/shutdown actually runs): `GET /api/stations` returned
  `200 []`; `websocket_connect('/ws/live')` completed the upgrade handshake and
  closed cleanly with no exception. Startup logged the expected graceful
  degradation warning ("initial MQTT connect failed, will keep retrying:
  [WinError 10061] ... actively refused") since no broker is reachable here --
  this confirms the app does **not** crash when the broker is unreachable at
  startup, per the resilience requirement.
- Separately started a real `uvicorn app.main:app --port 8123` background
  process and hit it with `Invoke-WebRequest`:
  - `GET /` -> 200, 883 bytes (index.html)
  - `GET /css/app.css` -> 200, 1431 bytes
  - `GET /js/app.js` -> 200, 10959 bytes
  - `GET /api/stations` -> 200, `[]`
  Process logs confirmed clean startup (`Application startup complete`,
  `Uvicorn running on http://127.0.0.1:8123`) and the same graceful
  MQTT-unreachable warning as above. Process was stopped afterward; the
  temporary DB file and log files created for this check were deleted.

## 5. Sample packet injected through the ingestion pipeline

No real Mosquitto broker is reachable from this build environment, so per the
design's own documented strategy, injection was done directly against the
extracted pipeline logic (`parse_packet` / `process_parsed_packet` /
`on_message_impl`) rather than through a live MQTT client -- this is exactly
what `tests/test_ingest_pipeline.py` automates (see section 3), and was also
exercised standalone during `app/db.py`'s own step-3 verification:

```
db.store_packet({'from':'YB1ABC-9','latitude':-7.1,'longitude':110.1,'raw_packet':'test'})
-> db.get_stations() returned one row for YB1ABC-9
-> db.get_history('YB1ABC-9', 24) returned one history point
```

Confirms: a packet lands in SQLite (both `packets` and the `stations` index),
and (per the pipeline test) the same injection path produces exactly one
`broadcast_json`-shaped call carrying the full WebSocket contract, which is
what the real `ws_manager.broadcast_json_threadsafe` would deliver to any
connected browser.

## What was NOT verified (and why)

- **No real Mosquitto broker was reachable** from this Windows build
  environment (no Docker, no WSL2 Mosquitto install, no network path to the
  user's VM). Consequently, the live `paho-mqtt` `Client`'s actual
  `on_connect`/`on_message`/reconnect behavior against a running broker,
  including the `reconnect_delay_set` auto-reconnect behavior, was **not**
  exercised end-to-end with real network traffic. This matches the design's
  own documented scope ("Manual/optional ... covered by the optional
  manual_mqtt_replay.py script ... explicitly out of this build's
  automated-verification scope"). `tests/manual_mqtt_replay.py` is provided
  and was confirmed syntax-valid (`py_compile` succeeded) but not executed
  against a broker.
- **No browser was used.** Interactive/visual verification (map renders,
  markers move live, popups render correctly, sidebar filter works visually)
  was not performed -- only HTTP-level smoke checks (status codes, non-empty
  bodies, correct content) were run against the static files and REST/WS
  endpoints. This is the user's manual follow-up once the app is deployed, per
  their own stated plan ("kita review jika sudah up di website").
- **No deployment action was taken** against the user's actual Ubuntu
  24.04/Proxmox VM or a real Mosquitto broker there -- `deploy/` artifacts
  were authored and reviewed by inspection only, consistent with the
  requirement that this build never attempt a live connection to that VM.
- **systemd unit and mosquitto.conf were not validated by any systemd/
  mosquitto tooling** (none available on this Windows build machine) --
  confirmed only by manual review that each required section/field is present.

## Review fix iteration (review.json findings)

Two `CHANGES_REQUESTED` findings from `review.md` were addressed:

1. **History endpoint time-window bug** (`app/db.py::get_history`): the SQL
   bound was changed from SQLite's `datetime('now', '-N hours')` (space-
   separated, no timezone) to a Python-computed cutoff --
   `(datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()` -- the
   same convention `store_packet()` uses to write `received_at`, so the string
   comparison in `WHERE received_at >= ?` is apples-to-apples. Verified by a
   standalone repro: inserted a row with `received_at` at `00:01 UTC` of the
   current day, then called `get_history(callsign, hours=1)` several hours
   later -- before the fix this incorrectly returned the row; after the fix it
   correctly returns zero rows. Re-ran the full suite afterward:
   `.venv\Scripts\python.exe -m pytest tests/ -v` -> `17 passed` (unchanged,
   confirming no regression; the existing suite doesn't probe this boundary
   since every test inserts rows at "now").
2. **No symbol icon lookup in the frontend** (`app/static/js/app.js::iconFor`):
   added a `(table, code) -> {glyph, color}` lookup table covering the symbol
   pairs most relevant to LoRa APRS/iGate deployments (car, house/fixed
   station, jeep, truck, bike, weather station, balloon, digipeater, repeater,
   person, boat), with the previous generic colored-dot rendering kept as the
   fallback for any `(table, code)` pair not in the table -- matching
   design.md's "an icon-lookup table maps known (table, code) pairs to an icon
   image, with a generic fallback pin for any pair not in the lookup table."
   This is a pure frontend rendering change (no API/contract change); verified
   by inspection only, since interactive browser rendering was out of this
   environment's verification scope in the original pass (see "What was NOT
   verified" above) and remains so here.

## Direwolf -> MQTT bridge

Not implemented, as explicitly out of scope per requirements.md and design.md
("Direwolf -> MQTT bridge — OPEN DECISION, not part of this build"). The
recommended-default documentation note is present in `deploy/README.md` and
the root `README.md`'s "Known v1 limitations" section; no bridge code was
written.
