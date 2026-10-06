"""Pydantic response models for the REST API.

`telemetry: dict | None` is typed to always be present in the serialized output
(never omitted) -- matching the WebSocket broadcast contract's rule: the key is
always present, value is `null` when the station's latest packet carried no
telemetry block.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class StationOut(BaseModel):
    callsign: str
    received_at: str
    latitude: float | None = None
    longitude: float | None = None
    course: float | None = None
    speed: float | None = None
    altitude: float | None = None
    comment: str | None = None
    symbol: str | None = None
    telemetry: dict[str, Any] | None = None
    weather: dict[str, Any] | None = None


class WeatherPointOut(BaseModel):
    """One weather report; units as produced by aprslib (°C, %, mbar, degrees,
    m/s, mm, W/m²). Every key is always present, null when not reported."""

    received_at: str
    temperature: float | None = None
    humidity: float | None = None
    pressure: float | None = None
    wind_direction: float | None = None
    wind_speed: float | None = None
    wind_gust: float | None = None
    rain_1h: float | None = None
    rain_24h: float | None = None
    rain_since_midnight: float | None = None
    luminosity: float | None = None


class HistoryPointOut(BaseModel):
    received_at: str
    latitude: float | None = None
    longitude: float | None = None
    speed: float | None = None
    course: float | None = None
    altitude: float | None = None
