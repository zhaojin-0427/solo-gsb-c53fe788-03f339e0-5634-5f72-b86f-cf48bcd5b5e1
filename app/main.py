"""FastAPI application: vibration spike alert HTTP API."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import service
from .db import init_schema
from .models import (
    AlertsResponse,
    ChangesResponse,
    ReadingsRequest,
    ReadingsResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_schema()
    yield


app = FastAPI(
    title="Vibration Spike Alert API",
    version="1.0.0",
    description="Batch vibration readings with median baselines and merged spike alerts.",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    # Never echo raw request input back: non-finite amplitudes (Infinity, NaN)
    # are not JSON-serialisable and would break the error response itself.
    detail = [
        {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/readings", response_model=ReadingsResponse)
def post_readings(req: ReadingsRequest) -> ReadingsResponse:
    """Ingest a batch of readings for one device.

    - higher revision wins; older revisions are reported ``ignored``;
    - equal revision with different content returns HTTP 409;
    - each accepted reading is judged against the exact median of the prior
      60 minutes (strict left boundary, same-second excluded);
    - alerts and change records are committed in the same transaction, and
      retrying the same batch produces no duplicate changes.
    """
    results = service.ingest(req.device_id, req.readings)
    return ReadingsResponse(device_id=req.device_id, results=results)


@app.get("/alerts", response_model=AlertsResponse)
def get_alerts(
    device_id: str | None = Query(default=None, description="filter by device"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> AlertsResponse:
    """Current (active) merged alerts."""
    return AlertsResponse(alerts=service.list_alerts(device_id, limit))


@app.get("/changes", response_model=ChangesResponse)
def get_changes(
    cursor: int = Query(default=0, ge=0, description="last seen seq (0 = from start)"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> ChangesResponse:
    """Resume change feed: created / updated / revoked alerts after cursor."""
    changes, next_cursor = service.list_changes(cursor, limit)
    return ChangesResponse(changes=changes, next_cursor=next_cursor)
