"""Pydantic request/response models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Event time may be sent either as UTC unix whole seconds or an ISO-8601
# string; both are normalised to an integer second count.
ReadingKind = Literal["accepted", "ignored", "conflict"]


class ReadingIn(BaseModel):
    sample_id: str = Field(min_length=1, max_length=200)
    ts: int = Field(ge=0, description="event time, UTC unix whole seconds")
    rev: int = Field(ge=1, description="revision number, higher wins")
    amplitude: float = Field(description="finite non-negative amplitude")

    @field_validator("amplitude")
    @classmethod
    def _finite_non_negative(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("amplitude must be finite")
        if v < 0:
            raise ValueError("amplitude must be non-negative")
        return v


class ReadingResult(BaseModel):
    sample_id: str
    rev: int
    status: ReadingKind
    ts: int | None = None
    amplitude: float | None = None
    baseline: float | None = None
    window_count: int = 0
    hit: bool | None = None


class ReadingsRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)
    readings: list[ReadingIn] = Field(min_length=1, max_length=1000)


class ReadingsResponse(BaseModel):
    device_id: str
    results: list[ReadingResult]


class Alert(BaseModel):
    alert_id: str
    device_id: str
    start_ts: int
    end_ts: int
    peak_ts: int
    peak_amplitude: float
    hit_count: int
    sample_ids: list[str]


class AlertsResponse(BaseModel):
    alerts: list[Alert]


class Change(BaseModel):
    seq: int
    device_id: str
    alert_id: str
    kind: Literal["created", "updated", "revoked"]
    alert: Alert | None
    changed_at: str


class ChangesResponse(BaseModel):
    changes: list[Change]
    next_cursor: int | None
