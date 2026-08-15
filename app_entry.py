"""Production ASGI entrypoint with platform-facing metadata endpoints.

The actual application remains in api_server.py. This thin entrypoint adds
stable root and health endpoints so deployment platforms can inspect the
service without touching the Queue Worker or waking business processing.
"""

from datetime import datetime, timezone

import api_server
from fastapi import Request


app = api_server.app
_STARTED_AT = datetime.now(timezone.utc)


@app.get("/", tags=["system"])
def root(request: Request) -> dict:
    """Return basic service metadata without starting or inspecting the worker."""
    return {
        "service": "HivisionIDPhotos API",
        "version": app.version,
        "status": "ok",
        "worker_running": api_server.worker_running,
        "endpoints": {
            "health": "/health",
            "generate": "/generate",
            "process_queue": "/process-queue",
            "docs": "/docs",
        },
    }


@app.get("/health", tags=["system"])
def health(request: Request) -> dict:
    """Return a lightweight liveness response for deployment health checks."""
    now = datetime.now(timezone.utc)
    return {
        "status": "healthy",
        "service": "HivisionIDPhotos API",
        "version": app.version,
        "worker_running": api_server.worker_running,
        "started_at": _STARTED_AT.isoformat(),
        "checked_at": now.isoformat(),
    }
