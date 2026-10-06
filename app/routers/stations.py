"""REST endpoints: GET /api/stations, GET /api/stations/{callsign}/history.

Both route handlers are declared as plain `def`, not `async def`: db.py's
sqlite3 connection is a blocking call, and the same asyncio event loop that
dispatches these routes also dispatches WebSocket broadcasts via
`asyncio.run_coroutine_threadsafe`. Declaring the handlers as plain `def` makes
FastAPI dispatch each call through its own threadpool automatically, keeping
the event loop free regardless of query latency.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query

from app import db
from app.config import get_settings
from app.schemas import HistoryPointOut, StationOut

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/stations", response_model=list[StationOut])
def get_stations_route():
    try:
        rows = db.get_stations()
    except Exception as e:
        logger.error("GET /api/stations failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="internal error")

    results = []
    for row in rows:
        telemetry_json = row.get("telemetry_json")
        telemetry = json.loads(telemetry_json) if telemetry_json is not None else None
        weather_json = row.get("weather_json")
        weather = json.loads(weather_json) if weather_json is not None else None
        results.append(
            StationOut(
                callsign=row["callsign"],
                received_at=row["received_at"],
                latitude=row.get("latitude"),
                longitude=row.get("longitude"),
                course=row.get("course"),
                speed=row.get("speed"),
                altitude=row.get("altitude"),
                comment=row.get("comment"),
                symbol=row.get("symbol"),
                telemetry=telemetry,
                weather=weather,
            )
        )
    return results


@router.get("/stations/{callsign}/history", response_model=list[HistoryPointOut])
def get_station_history_route(
    callsign: str,
    hours: int = Query(default=None, ge=1, le=720),
):
    if hours is None:
        hours = get_settings().history_lookback_hours

    try:
        rows = db.get_history(callsign, hours)
    except Exception as e:
        logger.error("GET /api/stations/%s/history failed: %s", callsign, e, exc_info=True)
        raise HTTPException(status_code=500, detail="internal error")

    return [HistoryPointOut(**row) for row in rows]
