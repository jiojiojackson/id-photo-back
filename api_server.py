from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header
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


app = FastAPI(title="HivisionIDPhotos API", version="3.1.0")

creator = IDCreator()
creator.matting_handler = extract_human_birefnet_lite
creator.detection_handler = detect_face_retinaface

# The inference lock guarantees that the GPU/model is used by only one job at a time.
inference_lock = threading.Lock()
worker_lock = threading.Lock()
worker_running = False


def _check_worker_key(api_key: str | None) -> None:
    expected = os.getenv("LIGHTNING_API_KEY")
    if not expected or api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


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
    # This lock is intentional: do not run multiple IDCreator jobs concurrently.
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
        "version": "3.1.0",
        "queue_worker": True,
        "worker_mode": "serial",
    }


@app.get("/health")
def health():
    return {"status": "healthy", "run_mode": os.getenv("RUN_MODE", "normal")}


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


# Lightning receives only the Vercel bridge URL in the request body. The shared
# LIGHTNING_API_KEY is reused in-memory to authenticate calls back to Vercel.
# No R2 credentials are ever stored or transmitted to Lightning.

def _process_jobs(bridge_url: str, max_jobs: int | None) -> None:
    global worker_running
    processed = 0
    try:
        import requests

        bridge_url = bridge_url.rstrip("/")
        bridge_token = os.getenv("LIGHTNING_API_KEY")
        if not bridge_token:
            raise RuntimeError("LIGHTNING_API_KEY is not configured")
        headers = {
            "Authorization": f"Bearer {bridge_token}",
            "Accept": "application/json",
        }

        while max_jobs is None or processed < max_jobs:
            response = requests.get(
                f"{bridge_url}/api/worker/next",
                headers=headers,
                timeout=30,
            )
            if response.status_code == 204:
                requests.post(
                    f"{bridge_url}/api/worker/finish",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"processed": processed},
                    timeout=30,
                ).raise_for_status()
                break

            response.raise_for_status()
            job = response.json().get("job")
            if not job:
                break

            job_id = str(job["jobId"])
            if job.get("skip"):
                processed += 1
                continue

            started = time.perf_counter()
            try:
                requests.post(
                    f"{bridge_url}/api/worker/job/{job_id}/start",
                    headers=headers,
                    timeout=30,
                ).raise_for_status()

                input_response = requests.get(job["inputUrl"], timeout=60)
                input_response.raise_for_status()

                # _run_inference contains an explicit lock, so jobs are always serial.
                output, _ = _run_inference(
                    input_response.content,
                    int(job.get("width", 295)),
                    int(job.get("height", 413)),
                )

                requests.put(
                    job["outputUrl"],
                    data=output,
                    headers={"Content-Type": "image/png"},
                    timeout=120,
                ).raise_for_status()

                elapsed_ms = int((time.perf_counter() - started) * 1000)
                requests.post(
                    f"{bridge_url}/api/worker/job/{job_id}",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"status": "completed", "processingTimeMs": elapsed_ms},
                    timeout=30,
                ).raise_for_status()
                processed += 1
                print(f"[QueueWorker] completed job={job_id} time={elapsed_ms}ms")
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                try:
                    requests.post(
                        f"{bridge_url}/api/worker/job/{job_id}",
                        headers={**headers, "Content-Type": "application/json"},
                        json={
                            "status": "failed",
                            "error": str(exc)[:2000],
                            "processingTimeMs": elapsed_ms,
                        },
                        timeout=30,
                    ).raise_for_status()
                except Exception as callback_error:
                    print(f"[QueueWorker] callback failed job={job_id}: {callback_error}")
                processed += 1
                print(f"[QueueWorker] failed job={job_id} error={exc}")
    except Exception as exc:
        print(f"[QueueWorker] stopped unexpectedly: {exc}")
    finally:
        with worker_lock:
            worker_running = False
        print(f"[QueueWorker] stopped processed={processed}")


@app.post("/process-queue")
def process_queue(
    payload: dict,
    x_api_key: str | None = Header(default=None),
):
    _check_worker_key(x_api_key)

    bridge_url = str(payload.get("bridgeUrl", "")).strip()
    if not bridge_url or not bridge_url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="Valid bridgeUrl is required")

    max_jobs = payload.get("maxJobs")
    if max_jobs is not None:
        try:
            max_jobs = int(max_jobs)
            if max_jobs < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="maxJobs must be a positive integer")

    global worker_running
    with worker_lock:
        if worker_running:
            return {"status": "already_running"}
        worker_running = True
        thread = threading.Thread(
            target=_process_jobs,
            args=(bridge_url, max_jobs),
            name="id-photo-queue-worker",
            daemon=True,
        )
        thread.start()

    return {"status": "started", "mode": "serial"}
