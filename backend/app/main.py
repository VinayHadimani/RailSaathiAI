"""RailRoute AI - FastAPI application entry point.

Phase 1 (feasibility): prove the RailCore integration works with a real
train, real schedule and real segment-wise availability checks.

    POST /api/v1/test/segment-availability

Phase 2 (same-train combinations): find trains whose complete journey can
be covered by multiple consecutive segments on the SAME train, every
segment explicitly confirmed available.

    POST /api/v1/journeys/same-train/search

Phase 3 (train-change connections): find complete journeys that require
changing trains at intermediate stations (any number of changes), every
segment explicitly confirmed available; unverified journeys are reported
separately and never labelled confirmed.

    POST /api/v1/journeys/train-change/search

Phase 4 (mixed journeys): combine same-train seat changes and train
changes into one complete journey (any number of either kind), every
individual ticket segment explicitly confirmed available; unverified
journeys are reported separately and never labelled confirmed.

    POST /api/v1/journeys/mixed/search

Run from the backend/ directory:

    uvicorn app.main:app --reload

Swagger UI: http://127.0.0.1:8000/docs
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from .api.routes import router
from .core.config import get_settings
from .core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(
    title="RailRoute AI - RailCore Feasibility API",
    version=settings.app_version,
    description=(
        "Backend component that proves RailCore provides the data RailRoute AI "
        "needs: real train schedules and arbitrary station-to-station seat "
        "availability, including two independent segments of the SAME train "
        "(phase 1), a same-train segment-combination search engine whose "
        "results contain only explicitly confirmed segments (phase 2), a "
        "train-change connection engine that finds multi-train journeys "
        "with every segment explicitly confirmed available (phase 3), and "
        "a mixed journey engine that combines same-train seat changes and "
        "train changes into one complete journey with every individual "
        "ticket segment explicitly confirmed available (phase 4).\n\n"
        "**This service never books tickets.** Availability results are "
        "point-in-time checks, not bookings, and separate-ticket combinations "
        "are reported as requiring railway-rule verification."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

# Single-file frontend (repo root index.html). Served by the API server for
# the one-command local demo (uvicorn only); on hosted deployments (Vercel)
# the platform serves the static file and these routes are never hit.
_FRONTEND_FILES = [Path(__file__).resolve().parents[2] / "index.html", Path(__file__).resolve().parents[1] / "index.html"]


def _frontend_file() -> Path | None:
    for candidate in _FRONTEND_FILES:
        if candidate.is_file():
            return candidate
    return None


@app.get("/", include_in_schema=False, response_model=None)
def frontend_root():
    f = _frontend_file()
    if f is not None:
        return FileResponse(f, media_type="text/html")
    return RedirectResponse(url="/docs")


@app.get("/index.html", include_in_schema=False, response_model=None)
def frontend_index():
    return frontend_root()
