"""One-off manual script: inject a single sample APRS packet directly through
the app's real parsing/storage pipeline (same code path as a live MQTT
message), so the local web UI has something to show without a real broker.

Run with the project's venv:
    .venv\\Scripts\\python.exe inject_sample.py

Safe to delete after use -- not part of the application or test suite.
"""
from app.mqtt_ingest import on_message_impl

SAMPLE_PACKET = b"YB1ABC-9>APRS,TCPIP*:!0746.00S/11022.00E&iGate test comment here"
TOPIC = "aprs-igate/YB1ABC-9"


def fake_broadcast(payload):
    print("Would broadcast over WebSocket:", payload)


if __name__ == "__main__":
    on_message_impl(TOPIC, SAMPLE_PACKET, fake_broadcast)
    print("\nInjected sample packet for YB1ABC-9. Refresh http://localhost:8000/ to see it on the map.")
