# Raspberry Pi APRS weather + telemetry publisher ("Veno AWS")

A standalone Python script (`aws_publisher.py`) that runs on a Raspberry Pi,
reads a BME280 sensor plus Pi health metrics, encodes them as **valid APRS
packets**, and publishes each packet to the MQTT broker that this repository's
FastAPI + aprslib + Leaflet backend subscribes to. The station then appears on
the live map and on its `/weather/a/<callsign>` charts page.

## Data path

```
BME280 (I2C 0x76/0x77)  ─┐
Pi CPU temp / load /     ├─▶ aws_publisher.py ─▶ APRS packets ─▶ MQTT publish
mem / uptime / throttle  │     (pure-stdlib         (TNC2 text)   aprs-igate/VN2EWS-1
core voltage (Vin stub) ─┘      APRS encoders)                        │
                                                                      ▼
                               web backend subscribes aprs-igate/#  ─▶ aprslib.parse()
                                                                      │
                                                        live map + /weather/a/VN2EWS-1 charts
```

Three packet kinds are published to `aprs-igate/VN2EWS-1`:

1. **Weather report** – positioned APRS WX packet (symbol `/_`, the weather-
   station icon) carrying temperature, humidity, pressure, and wind when wired.
2. **Telemetry** – a position packet carrying base91 compressed telemetry
   (sequence + 5 analog channels + 8 digital bits) for Pi health.
3. **Telemetry metadata** – `PARM` / `UNIT` / `EQNS` / `BITS` messages that tell
   the web UI the channel names, units, and scaling. Sent once at startup and
   then every N frames.

## Why the packets look the way they do (read this)

The backend parses every MQTT message with **aprslib**. A packet aprslib cannot
parse is silently dropped and never shows on the map. Two consequences:

- **Telemetry is base91-embedded in a position packet, not a `T#...` report.**
  aprslib 0.7.2 (the version in this repo) does **not** support the standalone
  `T#SSS,a1,...` telemetry-report format – it raises `UnknownFormat`, so such a
  packet would be dropped here. The base91 compressed-telemetry form carries the
  same thing (sequence, 5 analog channels, 8 digital bits) and is exactly what
  the backend already ingests (`app/mqtt_ingest.py` reads `parsed['telemetry']`).
  The analog channel mapping, units, and scaling are unchanged from the original
  design; only the on-air container differs so the packet survives aprslib.
- **The APRS encoding was verified by round-tripping the script's own output
  through aprslib** (see `tests/test_aws_publisher.py`), including feeding the
  packets through the real backend ingest pipeline.

### Callsign vs. display name

The APRS **SOURCE callsign** becomes the map station id (aprslib's
`parsed['from']`), so it must be a callsign aprslib accepts. The default is
**`VN2EWS-1`**. This is a **private MQTT system** (not RF, not public APRS-IS),
so official amateur-radio callsign regulations do not apply; `VN2EWS-1` was
confirmed to parse cleanly with `aprslib.parse()`.

The friendly name **`Veno AWS`** is **not** the callsign – it lives in the
packet **comment**. Change the callsign with `--callsign` / `CALLSIGN`.

### Position is APPROXIMATE – replace it

The default `--lat -7.767 --lon 110.375` is an **approximate** FMIPA UGM
location, Yogyakarta. **Replace it with your exact antenna coordinates.** The
easy way: in Google Maps, right-click your antenna spot and click the
latitude/longitude at the top of the menu to copy it, then pass it via
`--lat`/`--lon` (or `LAT`/`LON`).

### What is live now vs. stubbed

Live now:
- BME280 temperature, humidity, pressure (I2C).
- Pi CPU temperature (`/sys/class/thermal/thermal_zone0/temp`).
- CPU load %, memory used %, uptime hours (`os.getloadavg`, `/proc/meminfo`,
  `/proc/uptime`).
- Throttling / under-voltage flags (`vcgencmd get_throttled`) in the digital bits.
- **Vin = Pi core voltage** (`vcgencmd measure_volts`) as a **stand-in**.

Stubbed (return `None`, fields omitted / placeholder) until the hardware is wired:
- **Anemometer wind** – wind is omitted from the WX packet (sent as `.../...g...`).
- **External input voltage** – falls back to the core-voltage stand-in above.
  This is the Pi's internal core voltage, **not** your battery/solar input,
  until the real sensor is connected. See the `TODO` in `read_external_vin()`.

Each data source degrades gracefully: a missing sensor/library/file logs once
and that field is simply omitted; the loop keeps running.

## Telemetry channels and labels

The five analog channels and their metadata (sent via `PARM`/`UNIT`/`EQNS`):

| ch | PARM     | UNIT | raw sent              | EQNS (`a,b,c`) → real value |
|----|----------|------|-----------------------|-----------------------------|
| 1  | CPUTemp  | degC | `round(degC*10)`      | `0, 0.1, 0`  → `0.1*raw`     |
| 2  | Vin      | V    | `round(volts*100)`    | `0, 0.01, 0` → `0.01*raw`    |
| 3  | CPULoad  | pct  | `round(load%)`        | `0, 1, 0`    → `raw`         |
| 4  | MemUsed  | pct  | `round(mem%)`         | `0, 1, 0`    → `raw`         |
| 5  | Uptime   | hr   | `round(hours*10)`     | `0, 0.1, 0`  → `0.1*raw`     |

Digital bits (`BITS` title `RPiHealth`): bit0 under-voltage now, bit1 throttled
now, bit2 under-voltage occurred, bit3 throttling occurred.

Without the metadata the UI shows raw unlabeled values; with it, named and
unit-correct values. The metadata is addressed to the station's own callsign so
the backend attaches it to the right station.

## Enable I2C and detect the BME280 (on the Pi)

```bash
sudo raspi-config          # Interface Options -> I2C -> Enable, then reboot
sudo apt install -y i2c-tools
i2cdetect -y 1             # expect 0x76 (default) or 0x77 (alternate)
```

If your BME280 shows at `0x77`, pass `--i2c-addr 0x77`.

## Install (on the Pi)

```bash
# copy the rpi-station/ folder to the Pi, e.g. to /home/veno/rpi-station
cd ~/rpi-station
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Fill in your VPS broker host, the broker user (`kopi`), and your real
coordinates:

```bash
python aws_publisher.py \
  --broker-host YOUR.VPS.HOST \
  --username kopi \
  --password 'YOUR_BROKER_PASSWORD' \
  --topic-prefix aprs-igate \
  --callsign VN2EWS-1 \
  --lat -7.767 --lon 110.375 \
  --interval 300 \
  --verbose
```

Every flag has an environment-variable equivalent (`MQTT_BROKER_HOST`,
`MQTT_BROKER_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TOPIC_PREFIX`,
`CALLSIGN`, `LAT`, `LON`, `I2C_ADDR`, `WX_INTERVAL`, `META_EVERY`, `VERBOSE`).
`--broker-host` / `MQTT_BROKER_HOST` is required. `--verbose` logs every
published packet string. The startup banner prints the resolved config with the
password masked.

## Run as a systemd service

See `aws-publisher.service` for a ready-to-edit unit. Put secrets in
`aws-publisher.env` (an `EnvironmentFile`), then:

```bash
sudo cp aws-publisher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aws-publisher.service
journalctl -u aws-publisher -f
```

## Troubleshooting

1. **Are packets reaching the broker?** On the VPS:
   ```bash
   mosquitto_sub -t 'aprs-igate/#' -v
   ```
   You should see `aprs-igate/VN2EWS-1 VN2EWS-1>APRS:...` lines appear every
   `--interval` seconds.
2. **On the broker but not on the map?** The backend only maps packets aprslib
   can parse. Confirm the backend is subscribed to the same `aprs-igate/#`
   prefix and watch its log for `parse failure` warnings.
3. **No weather fields?** Check `i2cdetect -y 1` finds the BME280 and that the
   `--i2c-addr` matches; otherwise the script logs the BME280 warning once and
   omits weather while still sending telemetry.
4. **Then check the map** and the `/weather/a/VN2EWS-1` charts page.

## Verification status

Only the **APRS encoding** was verified here, via aprslib round-trip and the
backend ingest pipeline on the Windows dev box (`tests/test_aws_publisher.py`).
Live BME280 reads and the Pi-only `vcgencmd` / `/proc` / `/sys` paths cannot be
exercised off a Raspberry Pi and were not run.
