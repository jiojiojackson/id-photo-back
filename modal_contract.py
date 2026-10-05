"""Validate wake requests before allocating a GPU."""
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID
import os
import re


def normalize_run_payload(payload: dict) -> dict:
    origin = str(payload.get("vercel_origin", "")).rstrip("/")
    parsed = urlparse(origin)
    allowed = {value.strip().rstrip("/") for value in os.getenv(
        "ALLOWED_VERCEL_ORIGINS", "https://id-photo-front.vercel.app"
    ).split(",") if value.strip()}
    if parsed.scheme != "https" or parsed.username or parsed.password or origin not in allowed:
        raise ValueError("Vercel origin is not allowed")
    run_id = str(payload.get("worker_run_id", ""))
    try:
        UUID(run_id)
    except (ValueError, AttributeError):
        raise ValueError("Invalid worker_run_id")
    credential = str(payload.get("worker_credential", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", credential):
        raise ValueError("Invalid worker credential")
    try:
        expiry = datetime.fromisoformat(str(payload.get("worker_credential_expires_at", "")).replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError
    except ValueError:
        raise ValueError("Worker credential is expired or invalid")
    max_jobs = payload.get("max_jobs")
    if max_jobs is not None and (not isinstance(max_jobs, int) or isinstance(max_jobs, bool) or max_jobs < 1):
        raise ValueError("max_jobs must be a positive integer")
    return {
        "worker_run_id": run_id,
        "worker_credential": credential,
        "worker_credential_expires_at": expiry.isoformat(),
        "vercel_origin": origin,
        "bridge_url": origin + "/api/worker",
        "max_jobs": max_jobs,
    }
