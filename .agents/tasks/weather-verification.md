# Weather charts: verification (iteration 1)

`weather-review.json` did not exist, so this was the first iteration (built from scratch). No commit, no push.

## Changed / added files
- `app/db.py`: `store_packet(parsed, received_at=None)`. With None, it stamps now and runs the unchanged live upsert. With an explicit time it runs the backfill upsert: `first_heard_at = MIN(...)`, and `last_packet_id`/`last_heard_at` only change when the incoming time is newer. Also new: `WEATHER_FIELDS`, `get_weather_history(callsign, hours)` (under `_db_lock`, Python ISO cutoff, `weather_json IS NOT NULL`, `ORDER BY received_at DESC, id DESC LIMIT 10000` then reversed, missing or non-numeric keys become None), and `packet_exists(callsign, raw, received_at)`.
- `app/mqtt_ingest.py`: `process_parsed_packet(..., received_at=None)` passes the value through to `store_packet`. Live callers are unchanged.
- `app/schemas.py`: `WeatherPointOut`.
- `app/routers/stations.py`: `GET /api/stations/{callsign}/weather?hours=` (plain `def`, `Query(default=48, ge=1, le=720)`).
- `app/main.py`: `GET /weather/a/{callsign}` returns `FileResponse(app/static/weather.html)`. It is registered before the StaticFiles mount.
- `app/static/weather.html`, `app/static/js/weather.js`, `app/static/css/weather.css`: the page has a header (callsign, comment, last report in local time, back link), current conditions, 24 h/48 h/7 d/30 d buttons (`aria-pressed`), a min/max/latest table, and per-field charts that are only drawn when the field has data. Wind direction is drawn as unconnected points on a 0-360° right axis. Chart.js 4.4.1 comes from jsDelivr with SRI `sha384-dug+JxfBvklEQdJ4AYuBBAIScUz0bVN73xpy273gcAwHjb3qI0fXmuYNaNfdyYJG`. I computed that hash from the downloaded file. The x axis is a linear epoch-ms axis with local-time ticks, so there is no date adapter. Live points come over `/ws/live` with app.js-style backoff (1 s up to 30 s), and the page refetches after a reconnect. All packet text is set with textContent.
- `app/static/js/app.js`: the popup weather section now has a "Show weather charts" link to `/weather/a/<encodeURIComponent(callsign)>`. It only appears when weather exists. `index.html` now uses `?v=3`. `app.css` has a small `.popup-weather-link` rule.
- `tools/import_aprsfi_raw.py`: new backfill importer (file argument or stdin). It runs `os.chdir(ROOT)`, then `get_settings()` and `db.init_db(settings.db_path)`. Each line goes through `parse_packet` and `process_parsed_packet(..., received_at=...)`.
- `tests/test_weather_charts.py`: 13 new tests. `README.md`: new "Weather charts" and "Importing history from aprs.fi" sections, and the limitation note is updated.

## Tests
`.venv\Scripts\python.exe -m pytest tests/ -v` gives 34 passed, 1 warning (a starlette/anyio deprecation that was already there). That is the 21 existing tests plus 13 new ones:
- weather endpoint: window (a 50 h-old row is excluded by the default 48 h and included with `hours=72`), ascending order, a non-weather packet is excluded, other callsigns are excluded, all 11 keys are present with nulls
- unknown callsign returns 200 `[]`
- `hours` = 0, 721, -5 each return 422
- the page route returns 200 text/html referencing `/js/weather.js` and `chart.js@4.4.1`; `/` and `/js/weather.js` return 200
- backfill of an older packet leaves `last_heard_at`/`last_packet_id` alone and sets `first_heard_at` to the older time; a newer backfilled packet does advance it
- live ingestion still updates the latest packet
- `parse_line`: WIB `2026-10-06 04:24:10` becomes `2026-10-05T21:24:10.000000+00:00`; WITA, WIT, UTC, GMT and Z are covered; PST, a line with no timestamp, and an invalid date each raise `ValueError` with a reason
- `import_lines` with the real YG2UFH-10 line stores 27.8 °C / 63 % / 985.5 mbar at the historical time; a second import counts 1 duplicate and leaves 1 row; the bad-zone line is counted as a failure with a clear message

Both JS files (`weather.js`, `app.js`) were syntax-checked with esprima 4.0.1. It was installed to a temp `--target` folder, not into the venv, and removed afterwards. Both parse OK. Node is not installed.

## Running server (http://127.0.0.1:8000, real DB `C:/Users/venou/AppData/Local/aprs-web/aprs.db`)
I restarted it with Stop-Process / Start-Process (`--reload`, hidden window). It is left running.

| Request | Status |
|---|---|
| `/weather/a/YG2UFH-10` | 200 `text/html; charset=utf-8` (has `<title>Weather charts</title>`, the chart.js@4.4.1 script, `/js/weather.js?v=1`) |
| `/api/stations/YG2UFH-10/weather?hours=720` | 200, body below |
| `/api/stations/YG2UFH-10/weather` (48 h default) | 200, same single point |
| `/api/stations/NOPE-1/weather` | 200 `[]` |
| `/api/stations/YG2UFH-10/weather?hours=0` / `?hours=721` | 422 / 422 |
| `/` | 200; HTML references `css/app.css?v=3` and `js/app.js?v=3` |
| `/js/weather.js?v=1`, `/css/weather.css?v=1`, `/js/app.js?v=3`, `/css/app.css?v=3` | 200 each; app.js contains `"/weather/a/" + encodeURIComponent(callsign)` |
| `/api/stations` | 200, same shape as before (below) |
| `/weather/a/yg2ufh-10/` (trailing slash) | 404 (no trailing-slash route; not in the spec) |

Weather body (hours=720):
```json
[{"received_at":"2026-10-06T06:56:06.091429+00:00","temperature":27.77777777777778,"humidity":63.0,"pressure":985.5,"wind_direction":null,"wind_speed":null,"wind_gust":null,"rain_1h":null,"rain_24h":null,"rain_since_midnight":null,"luminosity":null}]
```
`/api/stations` body:
```json
[{"callsign":"YG2UFH-10","received_at":"2026-10-06T06:56:06.091429+00:00","latitude":-7.707759512346229,"longitude":110.41005864656125,"course":null,"speed":null,"altitude":null,"comment":"IGate LilyGo TBeam Lora","symbol":"L_","telemetry":null,"weather":{"temperature":27.77777777777778,"humidity":63,"pressure":985.5}}]
```

## Importer CLI (run against a temp DB, not the real one)
I set `$env:DB_PATH` to a temp file (an environment variable overrides config.yaml), wrote the real YG2UFH-10 line plus a PST line to a file, and ran:
1. `tools\import_aprsfi_raw.py lines.txt` printed `line 2: skipped, unknown timezone abbreviation 'PST' (supported: WIB, WITA, WIT, UTC, GMT, Z)` and `imported: 1, duplicates skipped: 0, parse failures: 1`, exit 1.
2. Running it again printed `imported: 0, duplicates skipped: 1, parse failures: 1`, exit 1.
3. Feeding the first line on stdin printed `imported: 0, duplicates skipped: 1, parse failures: 0`, exit 0.
4. The temp DB then held 1 packet `('YG2UFH-10','2026-10-05T21:24:10.000000+00:00', {"temperature": 27.77…, "humidity": 63, "pressure": 985.5})` and a station row with first and last set to that time. I deleted the temp folder afterwards.

Nothing was inserted into the user's real DB. YG2UFH-10 still has its single stored weather packet.

## Not verified
- The charts were not rendered or checked in a real browser (no browser tool available). That covers the Chart.js layout, local-time ticks, the SRI/CDN load in a browser, range button behaviour, and live WebSocket appends on the page. The backend and HTML/JS delivery are verified as above.
