"""paho-mqtt client, aprslib parsing, store+broadcast.

A single `paho.mqtt.client.Client` instance runs in a background thread started
from FastAPI's `lifespan` context. Immediately after constructing the `Client`
instance, `client.suppress_exceptions = True` is set -- defense-in-depth, because
paho-mqtt 1.6.1 defaults this flag to False, and `_handle_on_message` logs then
re-raises any exception that escapes `on_message`, which kills the client's
background network thread (not the process) with no auto-restart. Setting
`suppress_exceptions = True` makes paho log and swallow such an exception
instead, so even an unanticipated gap in the application's own exception
handling degrades to "one packet was dropped and logged" rather than
"ingestion is dead until a manual restart."

`on_message` is structured as two nested try/except blocks with distinct
logging, so a storage/broadcast failure is never misreported as a parse
failure. Both blocks use an unqualified `except Exception as e:`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aprslib
import paho.mqtt.client as mqtt

from app import db, ws_manager
from app.telemetry import apply_equations

logger = logging.getLogger(__name__)

# Simple counter object so tests/metrics can observe parse-failure counts
# without needing a real metrics backend.
class _Counter:
    def __init__(self) -> None:
        self.count = 0

    def increment(self) -> None:
        self.count += 1


parse_failures_counter = _Counter()


def parse_packet(raw: bytes | str) -> dict[str, Any] | None:
    """Wraps `aprslib.parse()`. Accepts `bytes` (matching the real `on_message`
    call site, `aprslib.parse(msg.payload)`) but also `str` so unit tests can pass
    plain string fixtures directly. Never raises: returns `None` and logs on any
    exception, so "never crash the loop" is testable without a live MQTT client."""
    try:
        return aprslib.parse(raw)
    except Exception as e:
        payload_repr = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        logger.warning("parse failure: payload=%r error=%s", payload_repr, e)
        parse_failures_counter.increment()
        return None


def _owner_callsign(parsed: dict[str, Any]) -> str:
    """APRS telemetry-metadata messages are conventionally self-addressed; prefer
    the message target (addresse/to) when present, falling back to the sender."""
    return parsed.get("addresse") or parsed.get("to") or parsed["from"]


def _build_telemetry_json(parsed: dict[str, Any]) -> str | None:
    """Resolve the `telemetry_json` value for a position+telemetry packet:
    named/unit-correct shape if a telemetry_config row exists for the sender,
    else the raw_seq/raw_vals fallback. Returns None if the packet carries no
    telemetry block at all."""
    telemetry = parsed.get("telemetry")
    if not telemetry:
        return None

    vals = telemetry.get("vals")
    seq = telemetry.get("seq")

    config = db.get_telemetry_config(parsed["from"])
    if config is not None and _config_usable(config):
        named = apply_equations(vals, config)
        return json.dumps(named)

    return json.dumps({"raw_seq": seq, "raw_vals": vals})


def _config_usable(config: dict[str, Any]) -> bool:
    """A telemetry_config row is only usable for apply_equations() once all
    three of EQNS/UNIT/PARM have arrived with at least 5 analog-channel
    entries each -- APRS metadata sometimes arrives split across multiple
    messages, so a row may exist with one or two fields still empty."""
    return (
        len(config.get("eqns_json") or []) >= 5
        and len(config.get("unit_json") or []) >= 5
        and len(config.get("parm_json") or []) >= 5
    )


def _symbol(parsed: dict[str, Any]) -> str | None:
    """Concatenates symbol_table + symbol (table character first) per the
    documented icon-lookup convention."""
    table = parsed.get("symbol_table")
    code = parsed.get("symbol")
    if table is None or code is None:
        return None
    return f"{table}{code}"


def process_parsed_packet(
    parsed: dict[str, Any],
    broadcast_fn,
) -> None:
    """Branches on telemetry-message vs. position/status, builds the normalized
    dict, stores it, and broadcasts it. `broadcast_fn(payload: dict)` is called
    exactly once per stored position packet; never called for telemetry-message
    (config) packets. This is the piece extracted out of `on_message` so it can
    be unit-tested (and used by the pipeline-injection test) without a live
    MQTT client or asyncio event loop."""
    if parsed.get("format") == "telemetry-message":
        owner = _owner_callsign(parsed)
        eqns = parsed.get("tEQNS")
        unit = parsed.get("tUNIT")
        parm = parsed.get("tPARM")
        if eqns is None and unit is None and parm is None:
            # BITS-only or otherwise irrelevant telemetry-message; nothing to store.
            return
        db.upsert_telemetry_config(owner, eqns, unit, parm)
        return

    telemetry_json = _build_telemetry_json(parsed)
    # aprslib returns weather already unit-converted (temperature in °C,
    # pressure in mbar, ...) as a dict; stored generically so any key it
    # emits (wind/rain/luminosity) survives without a schema change.
    weather = parsed.get("weather")
    weather_json = json.dumps(weather) if isinstance(weather, dict) and weather else None
    normalized = {
        "from": parsed["from"],
        "raw_packet": parsed.get("raw"),
        "latitude": parsed.get("latitude"),
        "longitude": parsed.get("longitude"),
        "course": parsed.get("course"),
        "speed": parsed.get("speed"),
        "altitude": parsed.get("altitude"),
        "comment": parsed.get("comment"),
        "symbol": _symbol(parsed),
        "telemetry_json": telemetry_json,
        "weather_json": weather_json,
    }
    received_at = db.store_packet(normalized)

    payload = {
        "type": "position",
        "callsign": normalized["from"],
        "received_at": received_at,
        "latitude": normalized["latitude"],
        "longitude": normalized["longitude"],
        "course": normalized["course"],
        "speed": normalized["speed"],
        "altitude": normalized["altitude"],
        "comment": normalized["comment"],
        "symbol": normalized["symbol"],
        "telemetry": json.loads(telemetry_json) if telemetry_json is not None else None,
        "weather": json.loads(weather_json) if weather_json is not None else None,
        "raw_packet": normalized["raw_packet"],
    }
    broadcast_fn(payload)


def on_message_impl(msg_topic: str, msg_payload: bytes, broadcast_fn) -> None:
    """The actual logic behind paho's `on_message` callback, factored out so it
    is directly unit-testable without a real `mqtt.MQTTMessage`."""
    try:
        parsed = aprslib.parse(msg_payload)
    except Exception as e:
        logger.warning(
            "parse failure: topic=%s payload=%r error=%s",
            msg_topic,
            msg_payload.decode("utf-8", errors="replace"),
            e,
        )
        parse_failures_counter.increment()
        return

    try:
        process_parsed_packet(parsed, broadcast_fn)
    except Exception as e:
        logger.error(
            "ingestion pipeline failure after successful parse: callsign=%s error=%s",
            parsed.get("from"),
            e,
            exc_info=True,
        )
        return


def build_client(settings, main_loop) -> mqtt.Client:
    """Constructs and configures the paho Client per the design: suppress_exceptions,
    on_connect subscribes to `<prefix>/#` at QoS 1, reconnect_delay_set, on_disconnect
    logs a WARNING."""
    client = mqtt.Client(client_id=settings.mqtt_client_id)
    client.suppress_exceptions = True

    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password)

    def on_connect(client, userdata, flags, rc):
        topic = f"{settings.mqtt_topic_prefix}/#"
        client.subscribe(topic, qos=1)
        logger.info("MQTT connected, subscribed to %s", topic)

    def on_disconnect(client, userdata, rc):
        logger.warning("MQTT disconnected: rc=%s", rc)

    def on_message(client, userdata, msg):
        def broadcast(payload):
            ws_manager.manager.broadcast_json_threadsafe(payload, main_loop)

        on_message_impl(msg.topic, msg.payload, broadcast)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    return client
