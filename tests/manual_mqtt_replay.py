"""Optional, environment-dependent manual script.

Publishes each tests/sample_packets/*.txt fixture to a local Mosquitto broker
on the configured topic prefix, for a true end-to-end manual check if you have
a local Mosquitto available (e.g. via WSL, or a Windows Mosquitto install).

Not part of the automated test suite -- run manually:

    python tests/manual_mqtt_replay.py --host localhost --port 1883 --prefix aprs

Requires MQTT_BROKER_HOST / MQTT_TOPIC_PREFIX to be resolvable the same way as
the real app (CLI args below are provided for convenience so you don't need a
full .env for a quick manual check).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import paho.mqtt.client as mqtt

FIXTURES_DIR = Path(__file__).parent / "sample_packets"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--prefix", default="aprs")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between publishes")
    args = parser.parse_args()

    client = mqtt.Client()
    client.connect(args.host, args.port)
    client.loop_start()

    try:
        for fixture_path in sorted(FIXTURES_DIR.glob("*.txt")):
            raw = fixture_path.read_bytes()
            if not raw:
                continue
            # Topic suffix is cosmetic -- the app always trusts parsed['from']
            # over the topic, so any suffix under <prefix>/# is received.
            topic = f"{args.prefix}/{fixture_path.stem}"
            print(f"publishing {fixture_path.name} -> {topic}")
            client.publish(topic, raw, qos=1)
            time.sleep(args.delay)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
