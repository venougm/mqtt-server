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


class HistoryPointOut(BaseModel):
    received_at: str
    latitude: float | None = None
    longitude: float | None = None
    speed: float | None = None
    course: float | None = None
    altitude: float | None = None
