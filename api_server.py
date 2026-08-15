from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import Response
from hivision.creator import IDCreator
from hivision.creator.human_matting import extract_human_birefnet_lite
from hivision.creator.face_detector import detect_face_retinaface
from PIL import Image

import cv2
import io
import numpy as np
import os
import threading
import time
from urllib.parse import urlparse


app = FastAPI(title="HivisionIDPhotos API", version="3.2.0")

creator = IDCreator()
creator.matting_handler = extract_human_birefnet_lite
creator.detection_handler = detect_face_retinaface

inference_lock = threading.Lock()
worker_lock = threading.Lock()
worker_running = False

HEARTBEAT_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 60
UPLOAD_TIMEOUT_SECONDS = 120


def _run_inference(data: bytes, width: int, height: int) -> tuple[bytes, float]:
    if width < 100 or width > 3000:
        raise ValueError("width must be between 100 and 3000 pixels")
    if height < 100 or height > 3000:
        raise ValueError("height must be between 100 and 3000 pixels")
    if not data:
        raise ValueError("Empty image")

    try:
        pil_image = Image.open(io.BytesIO(data)).convert("RGB")
        image_rgb = np.array(pil_image)
        image_bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    except Exception as exc:
        raise ValueError(f"Invalid image: {exc}") from exc

    start_time = time.perf_counter()
    with inference_lock:
        result = creator(image_bgr, size=(height, width), face_alignment=False)
    elapsed = time.perf_counter() - start_time

    output = result.hd
    if output is None:
        raise RuntimeError("HD result is None")
    success, encoded = cv2.imencode(".png", output)
    if not success:
        raise RuntimeError("Failed to encode PNG")
    return encoded.tobytes(), elapsed


@app.get("/")
def root():
    return {
        "service": "HivisionIDPhotos API",
        "status": "ok",
        "version": "3.2.0",
        "queue_worker": True,
        "worker_mode": "serial",
        "bridge_contract": "vercel-worker-v1",
    }


@app.get("/health")
def health():
    with worker_lock:
        running = worker_running
    return {
        "status": "healthy",
        "run_mode": os.getenv("RUN_MODE", "normal"),
        "worker_running": running,
    }


@app.get("/worker/status")
def worker_status():
    with worker_lock:
        return {"running": worker_running, "mode": "serial"}


@app.post("/generate")
async def generate(
    image: UploadFile = File(...),
    width: int = Form(295),
    height: int = Form(413),
):
    """Legacy synchronous API, kept for direct/manual inference testing."""
    data = await image.read()
    try:
        output, elapsed = _run_inference(data, width, height)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Inference failed: {exc}") from exc

    return Response(
        content=output,
        media_type="image/png",
        headers={
            "X-Photo-Width": str(width),
            "X-Photo-Height": str(height),
            "X-Inference-Time": f"{elapsed:.3f}",
        },
    )


def _bridge_headers(worker_credential: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {worker_credential}",
        "Content-Type": "application/json",
    }


def _log_bridge_request(method: str, url: str, run_id: str, stage: str) -> None:
    """Log the complete Vercel Bridge URL, including hostname, without logging secrets."""
    print(
        f"[QueueWorker] Vercel request stage={stage} method={method} "
        f"url={url} run={run_id}",
        flush=True,
    )


def _heartbeat_loop(bridge_url: str, worker_credential: str, job_id: str, stop_event: threading.Event, worker_run_id: str) -> None:
    import requests

    url = f"{bridge_url}/api/worker/heartbeat"
    while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
        _log_bridge_request("POST", url, worker_run_id, "heartbeat")
        try:
            response = requests.post(
                url,
                headers=_bridge_headers(worker_credential),
                json={"jobId": job_id},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 401:
                print(f"[QueueWorker] heartbeat unauthorized job={job_id}", flush=True)
                return
            if response.status_code == 409:
                print(f"[QueueWorker] heartbeat lease expired job={job_id}", flush=True)
                return
            response.raise_for_status()
        except Exception as exc:
            print(f"[QueueWorker] heartbeat failed job={job_id}: {exc}", flush=True)


def _process_jobs(bridge_url: str, worker_run_id: str, worker_credential: str, max_jobs: int | None) -> None:
    global worker_running
    processed = 0

    try:
        import requests

        bridge_url = bridge_url.rstrip("/")
        headers = _bridge_headers(worker_credential)
        next_url = f"{bridge_url}/api/worker/next"
        finish_url = f"{bridge_url}/api/worker/finish"
        _log_bridge_request("POST", next_url, worker_run_id, "next")
        print(
            f"[QueueWorker] started run={worker_run_id} "
            f"bridge={bridge_url} credential_length={len(worker_credential)}",
            flush=True,
        )

        while max_jobs is None or processed < max_jobs:
            _log_bridge_request("POST", next_url, worker_run_id, "next")
            response = requests.post(
                next_url,
                headers=headers,
                json={},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            if response.status_code == 401:
                response_body = response.text[:1000]
                print(
                    f"[QueueWorker] /next unauthorized run={worker_run_id} "
                    f"status=401 body={response_body!r}",
                    flush=True,
                )
                raise RuntimeError("worker credential is invalid or expired")
            response.raise_for_status()
            payload = response.json()
            status = payload.get("status")

            if status == "empty":
                _log_bridge_request("POST", finish_url, worker_run_id, "finish")
                finish = requests.post(
                    finish_url,
                    headers=headers,
                    json={"processed": processed},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                finish.raise_for_status()
                print(f"[QueueWorker] queue empty, worker finished processed={processed}", flush=True)
                break

            if status != "job" or not payload.get("job"):
                raise RuntimeError(f"unexpected /next response: {payload}")

            job = payload["job"]
            job_id = str(job["id"])
            started = time.perf_counter()
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(
                target=_heartbeat_loop,
                args=(bridge_url, worker_credential, job_id, heartbeat_stop, worker_run_id),
                name=f"heartbeat-{job_id}",
                daemon=True,
            )
            heartbeat_thread.start()

            try:
                input_response = requests.get(
                    job["inputUrl"],
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
                input_response.raise_for_status()

                output, inference_elapsed = _run_inference(
                    input_response.content,
                    int(job.get("width", 295)),
                    int(job.get("height", 413)),
                )

                requests.put(
                    job["outputUrl"],
                    data=output,
                    headers={"Content-Type": "image/png"},
                    timeout=UPLOAD_TIMEOUT_SECONDS,
                ).raise_for_status()

                elapsed_ms = int((time.perf_counter() - started) * 1000)
                complete_url = f"{bridge_url}/api/worker/complete"
                _log_bridge_request("POST", complete_url, worker_run_id, "complete")
                complete = requests.post(
                    complete_url,
                    headers=headers,
                    json={
                        "jobId": job_id,
                        "workerRunId": worker_run_id,
                        "processingTimeMs": elapsed_ms,
                    },
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                complete.raise_for_status()
                processed += 1
                print(
                    f"[QueueWorker] completed job={job_id} "
                    f"total={elapsed_ms}ms inference={inference_elapsed:.3f}s",
                    flush=True,
                )

            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                fail_url = f"{bridge_url}/api/worker/fail"
                _log_bridge_request("POST", fail_url, worker_run_id, "fail")
                try:
                    failed = requests.post(
                        fail_url,
                        headers=headers,
                        json={
                            "jobId": job_id,
                            "workerRunId": worker_run_id,
                            "error": str(exc)[:2000],
                        },
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    failed.raise_for_status()
                except Exception as callback_error:
                    print(f"[QueueWorker] fail callback failed job={job_id}: {callback_error}", flush=True)
                processed += 1
                print(f"[QueueWorker] failed job={job_id} time={elapsed_ms}ms error={exc}", flush=True)
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=2)

    except Exception as exc:
        print(f"[QueueWorker] stopped unexpectedly run={worker_run_id}: {exc}", flush=True)
    finally:
        with worker_lock:
            worker_running = False
        print(f"[QueueWorker] stopped run={worker_run_id} processed={processed}", flush=True)


def _process_queue_payload(payload: dict) -> dict:
    raw_bridge_url = str(payload.get("bridge_url", payload.get("bridgeUrl", ""))).strip().rstrip("/")
    raw_vercel_origin = str(payload.get("vercel_origin", payload.get("vercelOrigin", ""))).strip().rstrip("/")
    worker_run_id = str(payload.get("worker_run_id", payload.get("workerRunId", ""))).strip()
    worker_credential = str(payload.get("worker_credential", payload.get("workerCredential", ""))).strip()

    # `vercel_origin` is the authoritative dynamic Vercel host. It is generated by
    # NextRequest.nextUrl.origin on the exact Preview/Production deployment that
    # received the user's /api/jobs/start request. The old bridge_url is retained
    # only as a backward-compatible fallback.
    bridge_url = raw_bridge_url
    if raw_vercel_origin:
        parsed_origin = urlparse(raw_vercel_origin)
        if parsed_origin.scheme not in ("https", "http") or not parsed_origin.netloc:
            raise HTTPException(status_code=400, detail="Invalid vercel_origin")
        bridge_url = f"{parsed_origin.scheme}://{parsed_origin.netloc}/api/worker"

    if not bridge_url or not bridge_url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="Valid bridge_url is required")
    if not worker_run_id:
        raise HTTPException(status_code=400, detail="worker_run_id is required")
    if len(worker_credential) < 32:
        raise HTTPException(status_code=400, detail="worker_credential is required")

    max_jobs = payload.get("max_jobs", payload.get("maxJobs"))
    if max_jobs is not None:
        try:
            max_jobs = int(max_jobs)
            if max_jobs < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_jobs must be a positive integer")

    print(
        f"[QueueWorker] /process-queue received run={worker_run_id} "
        f"vercel_origin={raw_vercel_origin or '<not-provided>'} "
        f"bridge_url={bridge_url}",
        flush=True,
    )

    return {
        "bridge_url": bridge_url,
        "vercel_origin": raw_vercel_origin,
        "worker_run_id": worker_run_id,
        "worker_credential": worker_credential,
        "max_jobs": max_jobs,
    }


@app.post("/process-queue")
def process_queue(payload: dict, request: Request):
    """Wake a stateless Lightning Worker Run."""
    parsed = _process_queue_payload(payload)

    # This is the hostname of the incoming HTTP request to Lightning itself, not
    # the Vercel hostname. The actual dynamic Vercel hostname is logged above from
    # the explicit vercel_origin field in the wake payload.
    forwarded_host = request.headers.get("x-forwarded-host")
    print(
        f"[QueueWorker] /process-queue inbound host={request.headers.get('host')} "
        f"forwarded_host={forwarded_host or '<none>'}",
        flush=True,
    )

    global worker_running
    with worker_lock:
        if worker_running:
            return {"status": "already_running", "mode": "serial", "worker_run_id": parsed["worker_run_id"]}
        worker_running = True
        thread = threading.Thread(
            target=_process_jobs,
            args=(
                parsed["bridge_url"],
                parsed["worker_run_id"],
                parsed["worker_credential"],
                parsed["max_jobs"],
            ),
            name="id-photo-queue-worker",
            daemon=True,
        )
        thread.start()

    return {
        "status": "started",
        "mode": "serial",
        "worker_run_id": parsed["worker_run_id"],
        "vercel_origin": parsed["vercel_origin"],
    }
