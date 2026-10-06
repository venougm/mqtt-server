"""One-off manual script: replace the local test data with the real
YG2UFH-10 station by injecting its APRS weather packet directly through the
app's real parsing/storage pipeline (same code path as a live MQTT message),
so the local web UI has something to show without a real broker.

The packet is YG2UFH-10's real raw packet as shown on aprs.fi (compressed
position + weather), sent by the project's LoRa iGate firmware.

Run with the project's venv:
    .venv\\Scripts\\python.exe inject_sample.py

Safe to delete after use -- not part of the application or test suite.
"""
from app import db
from app.config import get_settings
from app.mqtt_ingest import on_message_impl

SAMPLE_PACKET = (
    b"YG2UFH-10>APLRG1,TCPIP*,qAC,T2CSNGRAD:"
    b"=LRDS_jEH8_ !G.../...g...t082h63b09855IGate LilyGo TBeam Lora"
)
TOPIC = "aprs-igate/YG2UFH-10"
OLD_TEST_CALLSIGN = "YB1ABC-9"


def fake_broadcast(payload):
    print("Would broadcast over WebSocket:", payload)


def remove_station(callsign: str) -> None:
    """Delete a station and all its packets (stations row first: it holds the
    FK reference to packets)."""
    conn = db._ensure_initialized()
    with db._db_lock:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.execute("DELETE FROM stations WHERE callsign = ?", (callsign,))
            cur.execute("DELETE FROM packets WHERE callsign = ?", (callsign,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    # Use the configured DB_PATH (config.yaml/.env), not db.py's ./data fallback.
    db_path = get_settings().db_path
    db.init_db(db_path)
    print("Using DB:", db_path)

    remove_station(OLD_TEST_CALLSIGN)
    print("Removed test station", OLD_TEST_CALLSIGN)

    on_message_impl(TOPIC, SAMPLE_PACKET, fake_broadcast)
    print("\nInjected packet for YG2UFH-10. Refresh http://localhost:8000/ to see it on the map.")
