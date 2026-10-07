"""FastAPI app assembly: lifespan, router includes, static mount.

On startup: capture `main_loop = asyncio.get_running_loop()`, initialize the
DB, construct and configure the paho Client from mqtt_ingest, call
`client.loop_start()`. On shutdown: `client.loop_stop()` + `client.disconnect()`.

Routers are included under /api and /ws first, then the /weather/a/{callsign}
page route; StaticFiles is mounted at "/" last, so all of those take precedence.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import db, mqtt_ingest
from app.config import get_settings
from app.routers import stations, ws

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level)

    db.init_db(settings.db_path)

    main_loop = asyncio.get_running_loop()
    client = mqtt_ingest.build_client(settings, main_loop)

    try:
        client.connect(settings.mqtt_broker_host, settings.mqtt_broker_port)
    except Exception as e:
        # Mirrors the design's resilience requirement: a broker that is
        # unreachable at startup must not crash the process -- paho's own
        # reconnect logic (loop_start + reconnect_delay_set) keeps retrying.
        logger.warning("initial MQTT connect failed, will keep retrying: %s", e)

    client.loop_start()
    app.state.mqtt_client = client

    yield

    client.loop_stop()
    client.disconnect()


app = FastAPI(lifespan=lifespan)

app.include_router(stations.router, prefix="/api")
app.include_router(ws.router, prefix="/ws")

_WEATHER_PAGE = Path(__file__).parent / "static" / "weather.html"
_TELEMETRY_PAGE = Path(__file__).parent / "static" / "telemetry.html"


@app.get("/weather/a/{callsign}", include_in_schema=False)
def weather_page(callsign: str):
    """Per-station weather charts page (same URL shape as aprs.fi). The page is
    static; its JS reads the callsign from the URL path."""
    return FileResponse(_WEATHER_PAGE, media_type="text/html")


@app.get("/telemetry/a/{callsign}", include_in_schema=False)
def telemetry_page(callsign: str):
    """Per-station telemetry charts page (same URL shape as the weather page).
    The page is static; its JS reads the callsign from the URL path."""
    return FileResponse(_TELEMETRY_PAGE, media_type="text/html")


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
