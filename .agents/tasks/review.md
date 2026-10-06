# LoRa APRS live map: fixes from the first review pass verified, no new findings

This is the second review pass. The first pass (verdict `CHANGES_REQUESTED`, preserved in `.agents/tasks/2026-10-05-140346-review-pass1.md`) flagged two confirmed issues: a history-endpoint time-window bug caused by comparing an ISO `received_at` string against SQLite's space-separated `datetime('now', ...)` output, and a frontend marker-icon renderer that only ever drew the generic fallback dot instead of the `(table, code)` lookup table the design specified. `verification.md` records both fixes and a regression run (17/17 tests still passing). This pass re-reads the actual code for both fixes, spot-checks the history boundary directly against the shipped `db.py` (not the test suite, which never constructs back-dated rows), and re-checks the full set of design contracts — SQLite lock discipline, the two-tier MQTT exception handling, the always-present `telemetry` key, the WebSocket reconnect/resync sequence, required config, sync route handlers, and the mandatory feature set — against the current source. Watch for: nothing blocking (confirmed, by direct code reading and a targeted spot-check). The Direwolf bridge remains correctly out of scope with no code written for it.

**Verdict**: APPROVED

## High-level view

The history time-window fix replaced the SQLite-side `datetime('now', '-N hours')` filter with a Python-computed cutoff (`datetime.now(timezone.utc) - timedelta(hours=hours)`, same `.isoformat()` convention `store_packet()` uses to write `received_at`), so both sides of the `WHERE received_at >= ?` comparison are now in the same string format. A direct spot-check — inserting a row 6 hours old and querying with `hours=1` — returns zero rows, and `hours=24` returns the row, confirming the fix works on the actual boundary the original bug missed.

The frontend icon fix adds a real `ICON_TABLE` keyed by `(table, code)` 2-char symbol strings, covering car, house/fixed-station, jeep, truck, bike, weather station, balloon, digipeater, repeater, person, and boat, with the generic colored-dot fallback retained for any unrecognized pair — matching the design's "lookup table with generic fallback" shape rather than the fallback-only behavior from the first pass. Only the primary symbol table (`/`) is covered; alternate-table (`\`) symbols still fall through to the generic dot, which is within the design's own fallback intent.

The remaining design contracts — SQLite lock discipline, the two-tier MQTT exception handling, the always-present `telemetry` key, the WebSocket reconnect/resync sequence, required config validation, and sync route handlers — were re-read in full against the current source and found unchanged from the first pass, which already confirmed them.

<details>
<summary>Issues (0)</summary>

No open issues from this pass.

</details>

<details>
<summary>Details</summary>

### History endpoint time-window fix, verified against the boundary

`get_history()` now computes `cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()` and binds it directly as the SQL parameter, replacing the earlier `datetime('now', '-N hours')` call. Spot-checked directly against the shipped `db.py` (not through the test suite, since none of the three test files construct a back-dated row — every test inserts at "now," which trivially satisfies any window regardless of the original bug):

```
db.init_db(":memory:")
# insert one row with received_at = now - 6 hours
db.get_history("TEST-1", 1)   -> []            (correctly excluded, 6h > 1h window)
db.get_history("TEST-1", 24)  -> 1 row         (correctly included, 6h < 24h window)
```

This confirms the lexical-comparison mismatch from the first pass (`'T'` vs `' '` sorting differently once the date portion matched) no longer exists on either side of the comparison — both are produced by the same `.isoformat()` call path. The `ORDER BY ... DESC` + `LIMIT 10000` + Python-side `.reverse()` cap-direction fix from the design review was untouched by this change and still correct.

### Frontend marker icons: real lookup table in place

```javascript
var ICON_TABLE = {
  "/>": { glyph: "...", color: "#2a7de1" }, // car
  "/-": { glyph: "...", color: "#2a7de1" }, // house (fixed station)
  ...
};
var FALLBACK_ICON = { glyph: null, color: "#2a7de1" };

function iconFor(symbol) {
  var def = (symbol && ICON_TABLE[symbol]) || FALLBACK_ICON;
  ...
}
```

Eleven `(table, code)` pairs relevant to LoRa APRS/iGate deployments are mapped to distinguishable glyph+color combinations; anything else falls through to the generic dot. This matches the design's "icon-lookup table maps known pairs to an icon image, with a generic fallback pin for any pair not in the lookup table" — the gap from the first pass (fallback-only, no table at all) is closed. Only the primary symbol table (`/`) is covered; alternate-table (`\`) symbols fall back to the generic dot, which is consistent with the design's own fallback intent and not something either review pass required to be exhaustive.

### Contracts re-checked against current source (no regressions found)

Read `db.py`, `mqtt_ingest.py`, `telemetry.py`, `ws_manager.py`, `config.py`, `main.py`, `routers/stations.py`, `routers/ws.py`, `schemas.py`, and `app.js` in full. All of the following match the design verbatim: the `_db_lock` acquired via `with` (never bare acquire/release) around every public `db.py` function with a fresh per-call cursor; the `try/except Exception: rollback(); raise` wrapper on both write functions; `PRAGMA journal_mode=WAL` / `PRAGMA foreign_keys=ON` at startup; raw `msg.payload` bytes passed straight to `aprslib.parse()` with no pre-decoding; `suppress_exceptions = True` set immediately after client construction; the two nested `except Exception as e:` blocks in `on_message_impl` with distinct WARNING/ERROR logging and no counter bump on the storage-path exception; the telemetry EQNS/UNIT/PARM read-modify-write merge in `upsert_telemetry_config`, plus the `_config_usable()` completeness gate (not spelled out verbatim in the design but a reasonable extension of its "metadata sometimes split across messages" intent, already noted favorably in the first pass); the `telemetry` key always present (object or `null`) in both the WebSocket payload and `StationOut`; sync (`def`, not `async def`) route handlers in `routers/stations.py`; the required `MQTT_BROKER_HOST`/`MQTT_TOPIC_PREFIX` validation raising `RuntimeError`, with every other key type/range-checked and falling back to its documented default with a WARNING; and the WebSocket open→buffer→fetch→flush→live sequence re-run on every reconnect in `app.js`, with the raw packet and comment fields rendered via `textContent` (never `innerHTML`) to keep MQTT-sourced attacker-influenceable text out of the DOM as markup.

Spot-checked the REST history endpoint's query validation directly: `hours=0` and `hours=800` both return `422` (bounds `[1, 720]` enforced by `Query(ge=1, le=720)`), an omitted `hours` falls back to the configured default and returns `200`, and an unknown callsign returns `200 []` rather than `404` — all matching the documented contract.

### Test coverage

17 tests across parser, telemetry, and pipeline-injection fixtures (confirmed by direct inspection of the three test files, matching `verification.md`'s count exactly: 7 + 3 + 7). The history time-window boundary still isn't covered by an automated test — the fix was verified by this pass's own direct spot-check against `db.py`, and by the manual repro recorded in `verification.md`, but no `tests/test_db.py` or similar exists to pin the regression going forward. This is worth adding at some point but isn't a blocking gap given the fix is now independently confirmed correct.

### Direwolf bridge

No bridge code exists anywhere in the tree. `README.md` and `deploy/README.md` both document it as a future task with a recommended default design, matching the "OPEN DECISION, not part of this build" scoping in `design.md` and the "Out of Scope" list in `requirements.md`.

</details>

<details>
<summary>File map</summary>

- `app/db.py` — history time-window fix (`get_history()` now uses a Python-computed `.isoformat()` cutoff instead of SQLite's `datetime('now', ...)`); everything else unchanged from the first pass.
- `app/static/js/app.js` — `iconFor()` now backed by a real `ICON_TABLE` of 11 `(table, code)` → glyph/color pairs with fallback retained; everything else unchanged.
- `app/config.py`, `app/mqtt_ingest.py`, `app/telemetry.py`, `app/ws_manager.py`, `app/main.py`, `app/schemas.py`, `app/routers/stations.py`, `app/routers/ws.py`, `app/static/index.html`, `app/static/css/app.css` — unchanged since the first pass; re-read in full for this review and found to match the design's documented contracts.
- `tests/test_aprs_parse.py`, `tests/test_telemetry.py`, `tests/test_ingest_pipeline.py`, `tests/sample_packets/*.txt` — unchanged, 17 tests, passing per `verification.md`.
- `deploy/README.md`, `deploy/mosquitto/mosquitto.conf`, `deploy/systemd/aprs-web.service`, `deploy/env.example`, `README.md`, `config.yaml.example`, `.env.example`, `requirements.txt` — unchanged, documentation/config only.

No `git` repository exists in this project, so this review was produced by reading the full source tree directly rather than a diff, same as the first pass.

</details>
