# aprs.fi-style per-station weather charts with raw-packet backfill

The change adds a weather history page at `/weather/a/<callsign>`, modeled on aprs.fi's weather view. It is backed by a new `GET /api/stations/{callsign}/weather?hours=N` endpoint and linked from the map popup. Local data only covers what the server has heard, so a CLI importer (`tools/import_aprsfi_raw.py`) backfills history from lines copied by hand off aprs.fi's raw-packets page. Those lines go through the same aprslib parse and `process_parsed_packet` path as live MQTT, carrying their historical receive time. All spec points are implemented and the design contracts hold: `with _db_lock:`, plain-`def` handlers, textContent-only rendering, the page route before the StaticFiles mount, forward-only backfill, idempotent re-import of position/weather packets, and no scraping.

Watch for: telemetry-metadata lines in a backfill overwrite the station's current telemetry config with whatever older version the paste contains (confirmed, non-blocking). A packet heard both live and on aprs.fi is stored twice, because the dedup key includes the receive time (confirmed, matches the spec). Callsign matching on the page is case-sensitive, unlike aprs.fi (likely, non-blocking).

**Verdict**: APPROVED

## High-level view

`store_packet(parsed, received_at=None)` keeps the live upsert unchanged when no time is given. With an explicit time it switches to a backfill upsert: `first_heard_at` takes the MIN, and `last_heard_at`/`last_packet_id` only move when the incoming time is newer. An old import can't drag a station's map marker back in time.

The weather endpoint copies `get_history()`: the same Python-side ISO cutoff, the same newest-first `LIMIT 10000` followed by a reverse, a fixed 11-key shape with nulls, and `ge=1, le=720`. The page route is a fixed-path `FileResponse`, so the callsign in the URL never touches the filesystem.

The page loads Chart.js 4.4.1 from jsDelivr with an SRI hash. The hash was spot-checked: the published file hashes to the pinned value. Charts are drawn only for series that have data, on an epoch-ms linear axis. Live points arrive over `/ws/live`, and the page does a full refetch on reconnect. The error path and the callsign handling have small UX gaps.

The importer is safe to re-run for position/weather packets. Telemetry-metadata messages are not deduplicated and not time-ordered, so they are the one place where a backfill can change "current" state.

Like the rest of the API, the new endpoint and page are unauthenticated. The app has no auth by design, and the data exposed is weather that is already public on APRS-IS.

<details>
<summary>Issues (5)</summary>

1. **Backfilled telemetry config overwrites current config** (confirmed): `import_lines` sends `telemetry-message` lines to `upsert_telemetry_config`, which unconditionally replaces the stored EQNS/UNIT/PARM, so an older metadata set in the paste wins over the newer live one. Either skip telemetry-message lines during backfill or apply them only when no config exists. At minimum, document the behavior.
2. **Live-plus-imported duplicates** (confirmed, per spec): the dedup key is (callsign, raw, received_at). A packet received live and then imported from aprs.fi is stored twice, a few seconds apart. Add a README line telling users to import only periods the server was offline.
3. **Case-sensitive callsign URL** (likely): `/weather/a/yg2ufh-10` shows the empty state because the lookups match exactly. Upper-case the callsign in `callsignFromPath()`.
4. **Misleading fetch-error text** (confirmed): the status says "Retrying when the live connection reconnects", but with a healthy WebSocket nothing ever retries. Add a timed retry, or change the text to ask for a reload or range click.
5. **Silent 10000-row truncation at 30 d** (confirmed in code, impact possible): a station reporting more often than about every 4.3 minutes loses the oldest part of the 30 d range, and the page gives no indication. Note the truncation in the status line when `data.length === 10000`.

</details>

<details>
<summary>Details</summary>

### Importer idempotency and its two gaps

The backfill upsert relies on SQLite evaluating every `SET` expression against the pre-update row, so both `CASE` branches compare against the old `last_heard_at`. The inline comment documents this, which protects against a future "fix" to the column order. The tests cover both directions: an older backfill leaves the latest pointer alone and lowers `first_heard_at`, and a newer backfill advances the pointer.

Dedup only runs for non-telemetry-message packets:

```python
is_position = parsed.get("format") != "telemetry-message"
if is_position and db.packet_exists(parsed["from"], parsed.get("raw"), received_at):
```

Telemetry-metadata lines (`:YG2UFH-10:PARM....`) therefore reach `upsert_telemetry_config` on every run (confirmed). That function overwrites each provided field and stamps `updated_at = now`, with no notion of the packet's historical time. A backfill containing an older PARM/UNIT/EQNS set replaces the live config, and every later position packet is labeled with the stale equations. Re-runs also count these lines as "imported" again. The firmware's metadata is probably stable, so this isn't blocking. It is still the one exception to "backfill never changes current state".

The second gap follows from the spec. Because `received_at` is part of the dedup key, overlap between live-received and aprs.fi-imported packets produces near-duplicate rows (confirmed).

### Page error state and callsign handling

Range fetches are sequence-tagged, and live points that arrive mid-load are buffered, so a switch from 30 d to 24 h can neither show stale data nor drop a report.

When a fetch fails, the status reads "Could not load weather data. Retrying when the live connection reconnects." (confirmed). The refetch only fires on a socket reopen, so with a healthy WebSocket the page stays in the error state until the user clicks a range button.

`callsignFromPath()` passes the decoded segment through unchanged, and both the weather and stations lookups match exactly (likely). A hand-typed lowercase URL renders "No weather reports received from this station yet." even though the station exists. The route also returns 404 for a trailing slash, while the JS regex accepts one, so the two disagree.

### Test coverage

The 13 new tests cover the endpoint contract (window, order, exclusions, null shape, 422 bounds, unknown callsign), the page route, forward-only upsert, the live path, zone parsing, and real-line import with duplicate skip. The coder recorded 34/34 passing plus HTTP checks against the running server. The only check re-run here was the Chart.js SRI hash, because the page was never loaded in a browser and a wrong hash would silently disable every chart. It matches.

Not tested: telemetry-message lines in `import_lines`, the WebSocket live-append and refetch-on-reconnect paths in `weather.js`, the `LIMIT 10000` truncation, and browser rendering of the charts (disclosed by the coder).

</details>

<details>
<summary>File map</summary>

- `app/db.py`: optional `received_at` on `store_packet`, live/backfill upsert SQL, `WEATHER_FIELDS`, `get_weather_history`, `packet_exists`
- `app/mqtt_ingest.py`: `received_at` passthrough on `process_parsed_packet`
- `app/schemas.py`: `WeatherPointOut`
- `app/routers/stations.py`: `GET /stations/{callsign}/weather`
- `app/main.py`: `/weather/a/{callsign}` FileResponse route before the static mount
- `app/static/weather.html`, `js/weather.js`, `css/weather.css`: charts page
- `app/static/js/app.js`, `css/app.css`, `index.html`: popup charts link, `?v=3`
- `tools/import_aprsfi_raw.py`: aprs.fi raw-line backfill CLI
- `tests/test_weather_charts.py`: 13 tests
- `README.md`: weather charts and importer docs, updated limitations

Full diff: `git diff HEAD` plus the untracked files above.

</details>
