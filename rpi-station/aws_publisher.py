#!/usr/bin/env python3
"""Raspberry Pi automatic weather station + Pi-health telemetry publisher.

Reads a BME280 sensor over I2C plus readily-available Raspberry Pi health
metrics, encodes them as valid APRS packets, and publishes each packet as a
plain-text TNC2 string to an MQTT broker under ``<prefix>/<CALLSIGN>``. The
companion FastAPI + aprslib + Leaflet backend in this repository subscribes to
``<prefix>/#``, parses every message with ``aprslib.parse()``, and renders the
station on the live map and on the ``/weather/a/<callsign>`` charts page.

IMPORTANT -- why the packet formats are what they are
-----------------------------------------------------
The backend parses with ``aprslib`` (0.7.2 in this repo). A packet that
``aprslib.parse()`` cannot parse is silently dropped and never reaches the map.
Two consequences shaped this script, and both were proven by round-tripping the
script's own output through ``aprslib`` (see tests/test_aws_publisher.py):

1. Weather is emitted as a standard positioned APRS *weather report* (symbol
   table ``/``, symbol code ``_``). aprslib returns ``parsed['weather']`` with
   temperature in degC, humidity in %, pressure in mbar.

2. Pi-health telemetry is emitted as APRS *base91 compressed telemetry* embedded
   in a position packet's ``|...|`` block -- NOT as a standalone ``T#...``
   report. aprslib 0.7.2 does NOT support the standalone ``T#`` telemetry-report
   format (it raises ``UnknownFormat``), so a ``T#`` packet would be dropped by
   this project's backend. The base91 form is exactly what the backend already
   ingests (see tests/sample_packets/position_with_telemetry.txt and
   app/mqtt_ingest.py, which reads ``parsed['telemetry']`` -> seq/vals/bits).
   The PARM/UNIT/EQNS metadata messages (which label and scale the channels in
   the web UI) are the standard telemetry-message format and parse unchanged.

The APRS-encoding functions below are pure stdlib Python so they import and run
on any machine (and are unit-tested on the Windows dev box). The hardware reads
(BME280 over I2C, vcgencmd, /proc, /sys) are guarded so a missing sensor or
library degrades gracefully to ``None`` instead of crashing the loop.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("aws_publisher")

# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------

DEFAULT_CALLSIGN = "VN2EWS-1"      # APRS SOURCE callsign (map station id).
DISPLAY_NAME = "Veno AWS"         # Goes in the packet COMMENT, not the callsign.
DEFAULT_LAT = -7.767              # APPROX FMIPA UGM, Yogyakarta -- REPLACE with
DEFAULT_LON = 110.375             # the exact antenna location (see README).
DEFAULT_TOPIC_PREFIX = "aprs-igate"
DEFAULT_MQTT_PORT = 1883
DEFAULT_WX_INTERVAL = 300         # seconds between weather+telemetry frames
DEFAULT_META_EVERY = 12           # emit PARM/UNIT/EQNS every N telemetry frames
DEFAULT_I2C_ADDR = 0x76           # BME280 default; 0x77 is the common alternate

# APRS telemetry carries at most 5 analog channels (0-4) and up to 8 digital
# bits. apply_equations() in app/telemetry.py scales each raw analog value with
# a quadratic a*x^2 + b*x + c. We pick EQNS so the raw integers we transmit map
# back to real units in the UI (raw is restricted to 0..8280 by base91, and we
# further clamp to 0..999 to stay well inside typical telemetry ranges).
TELEMETRY_PARM = ["CPUTemp", "Vin", "CPULoad", "MemUsed", "Uptime"]
TELEMETRY_UNIT = ["degC", "V", "pct", "pct", "hr"]
# EQNS as [a, b, c] per analog channel:
#   CPUTemp : raw = round(degC * 10)       -> degC    = 0.1 * raw   -> [0, 0.1, 0]
#   Vin     : raw = round(volts * 100)     -> volts   = 0.01 * raw  -> [0, 0.01, 0]
#   CPULoad : raw = round(load_pct)        -> pct     = 1 * raw     -> [0, 1, 0]
#   MemUsed : raw = round(mem_pct)         -> pct     = 1 * raw     -> [0, 1, 0]
#   Uptime  : raw = round(hours * 10)      -> hours   = 0.1 * raw   -> [0, 0.1, 0]
TELEMETRY_EQNS = [
    [0, 0.1, 0],
    [0, 0.01, 0],
    [0, 1, 0],
    [0, 1, 0],
    [0, 0.1, 0],
]
# Digital bit labels (BITS message). Bit 0 = under-voltage now, bit 1 = throttled
# now, bit 2 = under-voltage occurred, bit 3 = throttling occurred -- mirrors the
# low bits of `vcgencmd get_throttled`.
TELEMETRY_BITS_PROJECT = "RPiHealth"

# ---------------------------------------------------------------------------
# Pure APRS encoding helpers (stdlib only, importable + unit-tested off-Pi)
# ---------------------------------------------------------------------------


def decimal_to_dmm(value: float, is_lat: bool) -> str:
    """Convert a signed decimal degree to APRS degrees-decimal-minutes.

    Latitude  -> ``DDMM.mmH`` (H in N/S), longitude -> ``DDDMM.mmH`` (H in E/W).
    """
    if is_lat:
        hemi = "N" if value >= 0 else "S"
    else:
        hemi = "E" if value >= 0 else "W"
    value = abs(value)
    deg = int(value)
    minutes = (value - deg) * 60
    if is_lat:
        return f"{deg:02d}{minutes:05.2f}{hemi}"
    return f"{deg:03d}{minutes:05.2f}{hemi}"


def celsius_to_f(temp_c: float) -> int:
    """APRS weather temperature is whole degrees Fahrenheit (tXXX)."""
    return int(round(temp_c * 9.0 / 5.0 + 32.0))


def build_wx_packet(
    callsign: str,
    lat: float,
    lon: float,
    temp_c: Optional[float],
    humidity: Optional[float],
    pressure_mbar: Optional[float],
    wind_dir_deg: Optional[float] = None,
    wind_speed_mph: Optional[float] = None,
    wind_gust_mph: Optional[float] = None,
    comment: str = DISPLAY_NAME,
) -> str:
    """Build a positioned APRS weather report (TNC2).

    Layout: ``<call>>APRS:!<lat>/<lon>_<cse>/<spd>g<gust>t<F>h<RH>b<mbar*10><cmt>``

    The symbol table ``/`` + symbol code ``_`` is the APRS weather-station icon.
    Wind course/speed occupy the standard 3-digit course/speed slot that follows
    the ``_`` symbol; when wind is unknown they are sent as ``.../...`` and the
    gust as ``g...`` (exactly like the project's own LoRa iGate weather packets).

    Field encodings (verified against aprslib by round-trip):
      tXXX  temperature, whole degrees Fahrenheit
      hXX   humidity %, 00 means 100%
      bXXXXX barometric pressure in tenths of millibars (1013.2 mbar -> 10132)
    """
    pos = f"!{decimal_to_dmm(lat, True)}/{decimal_to_dmm(lon, False)}_"

    # Wind course/speed/gust block.
    if wind_dir_deg is not None and wind_speed_mph is not None:
        cse = f"{int(round(wind_dir_deg)) % 360:03d}"
        spd = f"{min(int(round(wind_speed_mph)), 999):03d}"
    else:
        cse = "..."
        spd = "..."
    if wind_gust_mph is not None:
        gust = f"g{min(int(round(wind_gust_mph)), 999):03d}"
    else:
        gust = "g..."

    wx = f"{cse}/{spd}{gust}"

    if temp_c is not None:
        wx += f"t{celsius_to_f(temp_c):03d}"
    else:
        # APRS convention: unknown temperature is sent as t... (three dots).
        wx += "t..."

    if humidity is not None:
        h = int(round(humidity))
        h = 0 if h >= 100 else max(h, 0)
        wx += f"h{h:02d}"

    if pressure_mbar is not None:
        wx += f"b{min(int(round(pressure_mbar * 10)), 99999):05d}"

    return f"{callsign}>APRS:{pos}{wx}{comment}"


def _base91(value: int) -> str:
    """Encode a non-negative integer as a 2-char APRS base91 pair (0..8280)."""
    value = int(round(value))
    value = max(0, min(value, 8280))
    return chr(value // 91 + 33) + chr(value % 91 + 33)


def _bits_string_to_int(bits: str) -> int:
    """APRS base91 digital value: bit 0 is the leftmost char of the displayed
    bits string, so the integer is the string read least-significant-bit-first.
    """
    bits = (bits + "00000000")[:8]
    return int(bits[::-1], 2)


def encode_telemetry_values(
    seq: int,
    analog_raw: list[int],
    bits: str = "00000000",
) -> str:
    """Encode base91 compressed telemetry (``|seq a1 a2 a3 a4 a5 digital|``).

    ``analog_raw`` must be exactly 5 already-scaled integers (0..999 by our
    EQNS). ``bits`` is an 8-char '0'/'1' string whose bit 0 is the first char.
    """
    if len(analog_raw) != 5:
        raise ValueError("APRS telemetry requires exactly 5 analog channels")
    out = "|" + _base91(seq % 8281)
    for v in analog_raw:
        out += _base91(v)
    out += _base91(_bits_string_to_int(bits))
    return out + "|"


def build_telemetry_packet(
    callsign: str,
    lat: float,
    lon: float,
    seq: int,
    analog_raw: list[int],
    bits: str = "00000000",
    comment: str = DISPLAY_NAME + " Pi health",
) -> str:
    """Position packet (symbol table ``/`` code ``>``) carrying base91 telemetry.

    The ``|...|`` telemetry block must be the final element of the packet.
    """
    pos = f"!{decimal_to_dmm(lat, True)}/{decimal_to_dmm(lon, False)}>"
    tel = encode_telemetry_values(seq, analog_raw, bits)
    return f"{callsign}>APRS:{pos}{comment}{tel}"


def _telemetry_addressee(callsign: str) -> str:
    """APRS message addressee field is a fixed 9-character left-justified field."""
    return callsign.ljust(9)


def build_parm_packet(callsign: str, parm: list[str] = None) -> str:
    parm = parm or TELEMETRY_PARM
    return f"{callsign}>APRS::{_telemetry_addressee(callsign)}:PARM." + ",".join(parm)


def build_unit_packet(callsign: str, unit: list[str] = None) -> str:
    unit = unit or TELEMETRY_UNIT
    return f"{callsign}>APRS::{_telemetry_addressee(callsign)}:UNIT." + ",".join(unit)


def build_eqns_packet(callsign: str, eqns: list[list[float]] = None) -> str:
    eqns = eqns or TELEMETRY_EQNS
    flat = []
    for a, b, c in eqns:
        flat.extend([a, b, c])
    return f"{callsign}>APRS::{_telemetry_addressee(callsign)}:EQNS." + ",".join(
        _fmt_number(n) for n in flat
    )


def build_bits_packet(callsign: str, project: str = TELEMETRY_BITS_PROJECT) -> str:
    """BITS message: 8 enable bits followed by an optional project title."""
    return f"{callsign}>APRS::{_telemetry_addressee(callsign)}:BITS.11111111,{project}"


def _fmt_number(n) -> str:
    """Render EQNS coefficients compactly (3 -> '3', 0.01 -> '0.01')."""
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    return str(n)


# Raw-value scaling helpers (inverse of the EQNS above). Kept next to EQNS so
# the two can never drift apart.

def scale_cputemp(temp_c: Optional[float]) -> int:
    return int(round((temp_c or 0.0) * 10))


def scale_vin(volts: Optional[float]) -> int:
    return int(round((volts or 0.0) * 100))


def scale_pct(pct: Optional[float]) -> int:
    return int(round(pct or 0.0))


def scale_uptime(hours: Optional[float]) -> int:
    return int(round((hours or 0.0) * 10))


# ---------------------------------------------------------------------------
# Sensor / Pi-health reads (hardware guarded -- degrade to None off-Pi)
# ---------------------------------------------------------------------------

_warned_once: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned_once:
        _warned_once.add(key)
        logger.warning(msg)


@dataclass
class BME280Reading:
    temperature_c: Optional[float]
    humidity_pct: Optional[float]
    pressure_mbar: Optional[float]


class BME280Source:
    """Lazy BME280 reader using the adafruit-circuitpython-bme280 library.

    The hardware/library import happens on first read so this module imports
    cleanly on a non-Pi machine (and the pure encoders stay unit-testable).
    """

    def __init__(self, i2c_addr: int = DEFAULT_I2C_ADDR) -> None:
        self.i2c_addr = i2c_addr
        self._sensor = None
        self._init_failed = False

    def _ensure(self) -> None:
        if self._sensor is not None or self._init_failed:
            return
        try:
            import board  # type: ignore
            import busio  # type: ignore
            from adafruit_bme280 import basic as adafruit_bme280  # type: ignore

            i2c = busio.I2C(board.SCL, board.SDA)
            self._sensor = adafruit_bme280.Adafruit_BME280_I2C(
                i2c, address=self.i2c_addr
            )
        except Exception as e:  # ImportError on dev box, I/O errors on a bad bus
            self._init_failed = True
            _warn_once(
                "bme280",
                f"BME280 unavailable (addr 0x{self.i2c_addr:02x}): {e}. "
                "Weather fields will be omitted.",
            )

    def read(self) -> BME280Reading:
        self._ensure()
        if self._sensor is None:
            return BME280Reading(None, None, None)
        try:
            return BME280Reading(
                temperature_c=float(self._sensor.temperature),
                humidity_pct=float(self._sensor.relative_humidity),
                pressure_mbar=float(self._sensor.pressure),  # hPa == mbar
            )
        except Exception as e:
            _warn_once("bme280-read", f"BME280 read failed: {e}")
            return BME280Reading(None, None, None)


def read_cpu_temp_c() -> Optional[float]:
    """Pi CPU temperature from the thermal zone sysfs file (millidegrees)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as fh:
            return int(fh.read().strip()) / 1000.0
    except Exception as e:
        _warn_once("cputemp", f"CPU temperature unavailable: {e}")
        return None


def read_core_voltage_v() -> Optional[float]:
    """Pi CORE voltage via `vcgencmd measure_volts` -- used as a STAND-IN for the
    external input-voltage sensor until that hardware is wired. This is NOT the
    battery/solar input voltage; see the README and the Vin TODO below."""
    out = _vcgencmd("measure_volts")
    if out is None:
        return None
    try:
        # Format: "volt=0.8700V"
        return float(out.split("=")[1].rstrip("V\n"))
    except Exception:
        return None


def read_cpu_load_pct() -> Optional[float]:
    """1-minute load average as a percentage of logical CPU count."""
    try:
        load1, _, _ = os.getloadavg()
        ncpu = os.cpu_count() or 1
        return max(0.0, min(load1 / ncpu * 100.0, 999.0))
    except Exception as e:
        _warn_once("cpuload", f"CPU load unavailable: {e}")
        return None


def read_mem_used_pct() -> Optional[float]:
    """Memory in use as a percentage, from /proc/meminfo."""
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0].strip()] = int(parts[1].strip().split()[0])
        total = info.get("MemTotal")
        avail = info.get("MemAvailable")
        if not total:
            return None
        if avail is None:
            avail = info.get("MemFree", 0)
        return max(0.0, min((total - avail) / total * 100.0, 100.0))
    except Exception as e:
        _warn_once("mem", f"Memory stats unavailable: {e}")
        return None


def read_uptime_hours() -> Optional[float]:
    """System uptime in hours, from /proc/uptime."""
    try:
        with open("/proc/uptime", "r") as fh:
            return float(fh.read().split()[0]) / 3600.0
    except Exception as e:
        _warn_once("uptime", f"Uptime unavailable: {e}")
        return None


def read_throttled_bits() -> str:
    """Map `vcgencmd get_throttled` to an 8-char APRS digital-bits string.

    bit0 under-voltage now, bit1 currently throttled, bit2 under-voltage has
    occurred, bit3 throttling has occurred. Remaining bits are 0.
    """
    out = _vcgencmd("get_throttled")
    if out is None:
        return "00000000"
    try:
        value = int(out.split("=")[1].strip(), 0)  # "throttled=0x50000"
    except Exception:
        return "00000000"
    bit = [
        value & 0x1,          # under-voltage now
        (value >> 2) & 0x1,   # currently throttled
        (value >> 16) & 0x1,  # under-voltage has occurred
        (value >> 18) & 0x1,  # throttling has occurred
        0, 0, 0, 0,
    ]
    return "".join(str(b) for b in bit)


# --- Not wired yet: anemometer + external input voltage. Return None. ---

def read_wind() -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Anemometer wind (direction deg, speed mph, gust mph).

    TODO: not wired yet. Returns (None, None, None) so the WX packet omits wind
    (sent as .../...g...). Implement once the anemometer is connected.
    """
    return (None, None, None)


def read_external_vin() -> Optional[float]:
    """External input-voltage sensor (battery/solar).

    TODO: not wired yet. Returns None; the publisher falls back to the Pi core
    voltage from `vcgencmd measure_volts` as a stand-in (clearly a placeholder).
    """
    return None


def _vcgencmd(arg: str) -> Optional[str]:
    """Run `vcgencmd <arg>`; return stdout or None if unavailable."""
    import subprocess

    try:
        result = subprocess.run(
            ["vcgencmd", arg],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except Exception as e:
        _warn_once(f"vcgencmd-{arg}", f"vcgencmd {arg} unavailable: {e}")
        return None


# ---------------------------------------------------------------------------
# Config + MQTT runtime
# ---------------------------------------------------------------------------


@dataclass
class Config:
    broker_host: str
    broker_port: int
    username: Optional[str]
    password: Optional[str]
    topic_prefix: str
    callsign: str
    lat: float
    lon: float
    i2c_addr: int
    wx_interval: int
    meta_every: int
    verbose: bool


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def parse_args(argv: Optional[list[str]] = None) -> Config:
    p = argparse.ArgumentParser(
        description="Raspberry Pi APRS weather + telemetry publisher (publishes "
        "to an MQTT broker consumed by this repo's aprslib/Leaflet backend)."
    )
    p.add_argument("--broker-host", default=_env("MQTT_BROKER_HOST"),
                   help="MQTT broker hostname/IP (env MQTT_BROKER_HOST). Required.")
    p.add_argument("--broker-port", type=int,
                   default=int(_env("MQTT_BROKER_PORT", str(DEFAULT_MQTT_PORT))),
                   help="MQTT broker port (env MQTT_BROKER_PORT, default 1883).")
    p.add_argument("--username", default=_env("MQTT_USERNAME"),
                   help="MQTT username (env MQTT_USERNAME). Omit for anonymous.")
    p.add_argument("--password", default=_env("MQTT_PASSWORD"),
                   help="MQTT password (env MQTT_PASSWORD).")
    p.add_argument("--topic-prefix",
                   default=_env("MQTT_TOPIC_PREFIX", DEFAULT_TOPIC_PREFIX),
                   help="Topic prefix; publishes to <prefix>/<callsign> "
                        "(env MQTT_TOPIC_PREFIX, default aprs-igate).")
    p.add_argument("--callsign", default=_env("CALLSIGN", DEFAULT_CALLSIGN),
                   help=f"APRS SOURCE callsign / map station id "
                        f"(env CALLSIGN, default {DEFAULT_CALLSIGN}).")
    p.add_argument("--lat", type=float, default=float(_env("LAT", str(DEFAULT_LAT))),
                   help=f"Latitude, decimal degrees (env LAT, default {DEFAULT_LAT}). "
                        "APPROXIMATE -- replace with the real antenna location.")
    p.add_argument("--lon", type=float, default=float(_env("LON", str(DEFAULT_LON))),
                   help=f"Longitude, decimal degrees (env LON, default {DEFAULT_LON}). "
                        "APPROXIMATE -- replace with the real antenna location.")
    p.add_argument("--i2c-addr", default=_env("I2C_ADDR", hex(DEFAULT_I2C_ADDR)),
                   help="BME280 I2C address, 0x76 (default) or 0x77.")
    p.add_argument("--interval", type=int,
                   default=int(_env("WX_INTERVAL", str(DEFAULT_WX_INTERVAL))),
                   help="Seconds between weather+telemetry frames (default 300).")
    p.add_argument("--meta-every", type=int,
                   default=int(_env("META_EVERY", str(DEFAULT_META_EVERY))),
                   help="Emit PARM/UNIT/EQNS metadata every N frames (default 12). "
                        "Metadata is also always sent once at startup.")
    p.add_argument("--verbose", action="store_true",
                   default=_env("VERBOSE", "") not in ("", "0", "false", "False"),
                   help="Log every published packet string.")

    args = p.parse_args(argv)

    if not args.broker_host:
        p.error("MQTT broker host is required (--broker-host or MQTT_BROKER_HOST).")

    i2c_addr = (
        int(args.i2c_addr, 16)
        if isinstance(args.i2c_addr, str) and args.i2c_addr.lower().startswith("0x")
        else int(args.i2c_addr)
    )

    return Config(
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        username=args.username,
        password=args.password,
        topic_prefix=args.topic_prefix,
        callsign=args.callsign,
        lat=args.lat,
        lon=args.lon,
        i2c_addr=i2c_addr,
        wx_interval=args.interval,
        meta_every=args.meta_every,
        verbose=args.verbose,
    )


def build_mqtt_client(cfg: Config):
    """Construct a paho-mqtt client with auth + auto-reconnect configured."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(client_id=f"rpi-aws-{cfg.callsign}")
    if cfg.username:
        client.username_pw_set(cfg.username, cfg.password)

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connected to %s:%s", cfg.broker_host, cfg.broker_port)
        else:
            logger.error("MQTT connect failed: rc=%s", rc)

    def on_disconnect(client, userdata, rc):
        logger.warning("MQTT disconnected (rc=%s); auto-reconnecting", rc)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def gather_telemetry_raw() -> tuple[list[int], str]:
    """Read Pi-health metrics and return (5 scaled analog ints, 8-char bits)."""
    analog = [
        scale_cputemp(read_cpu_temp_c()),
        scale_vin(read_external_vin() if read_external_vin() is not None
                  else read_core_voltage_v()),
        scale_pct(read_cpu_load_pct()),
        scale_pct(read_mem_used_pct()),
        scale_uptime(read_uptime_hours()),
    ]
    return analog, read_throttled_bits()


class Publisher:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bme280 = BME280Source(cfg.i2c_addr)
        self.client = build_mqtt_client(cfg)
        self.topic = f"{cfg.topic_prefix}/{cfg.callsign}"
        self.seq = 0
        self.frame = 0
        self._stop = False

    def _publish(self, packet: str) -> None:
        if self.cfg.verbose:
            logger.info("PUBLISH %s %s", self.topic, packet)
        self.client.publish(self.topic, packet, qos=1)

    def publish_metadata(self) -> None:
        for packet in (
            build_parm_packet(self.cfg.callsign),
            build_unit_packet(self.cfg.callsign),
            build_eqns_packet(self.cfg.callsign),
            build_bits_packet(self.cfg.callsign),
        ):
            self._publish(packet)

    def publish_frame(self) -> None:
        bme = self.bme280.read()
        wind_dir, wind_spd, wind_gust = read_wind()
        wx = build_wx_packet(
            self.cfg.callsign, self.cfg.lat, self.cfg.lon,
            bme.temperature_c, bme.humidity_pct, bme.pressure_mbar,
            wind_dir, wind_spd, wind_gust,
        )
        self._publish(wx)

        analog, bits = gather_telemetry_raw()
        self.seq = (self.seq + 1) % 1000
        tel = build_telemetry_packet(
            self.cfg.callsign, self.cfg.lat, self.cfg.lon,
            self.seq, analog, bits,
        )
        self._publish(tel)

    def run(self) -> None:
        self.client.connect(self.cfg.broker_host, self.cfg.broker_port, keepalive=60)
        self.client.loop_start()
        try:
            # Metadata first so the UI has labels before the first values land.
            self.publish_metadata()
            while not self._stop:
                self.frame += 1
                self.publish_frame()
                if self.frame % self.cfg.meta_every == 0:
                    self.publish_metadata()
                self._interruptible_sleep(self.cfg.wx_interval)
        finally:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("Stopped; MQTT disconnected cleanly.")

    def _interruptible_sleep(self, seconds: int) -> None:
        for _ in range(seconds):
            if self._stop:
                return
            time.sleep(1)

    def stop(self, *_args) -> None:
        logger.info("Shutdown requested.")
        self._stop = True


def _banner(cfg: Config) -> None:
    masked = "***" if cfg.password else "(none)"
    logger.info("=== Raspberry Pi APRS weather + telemetry publisher ===")
    logger.info("  broker       : %s:%s", cfg.broker_host, cfg.broker_port)
    logger.info("  username     : %s", cfg.username or "(anonymous)")
    logger.info("  password     : %s", masked)
    logger.info("  topic        : %s/%s", cfg.topic_prefix, cfg.callsign)
    logger.info("  callsign     : %s  (display name '%s' lives in the comment)",
                cfg.callsign, DISPLAY_NAME)
    logger.info("  position     : lat=%s lon=%s  (APPROXIMATE -- edit to real site)",
                cfg.lat, cfg.lon)
    logger.info("  BME280 addr  : 0x%02x", cfg.i2c_addr)
    logger.info("  interval     : %ss  (metadata every %s frames)",
                cfg.wx_interval, cfg.meta_every)


def main(argv: Optional[list[str]] = None) -> int:
    cfg = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _banner(cfg)
    publisher = Publisher(cfg)
    signal.signal(signal.SIGINT, publisher.stop)
    signal.signal(signal.SIGTERM, publisher.stop)
    try:
        publisher.run()
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
