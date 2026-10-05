"""Deploy with: modal deploy modal_app.py

The HTTP control plane runs on CPU. A detached Modal Function owns the complete
GPU worker lifetime; HTTP responses never own an inference thread.
"""
from pathlib import Path
import modal

ROOT = Path(__file__).resolve().parent
app = modal.App("id-photo-gpu")
run_registry = modal.Dict.from_name("id-photo-gpu-runs", create_if_missing=True)
control_secret = modal.Secret.from_name("id-photo-modal-control")

control_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi==0.115.12", "requests==2.32.4")
    .add_local_file(ROOT / "modal_contract.py", "/root/modal_contract.py")
)
gpu_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("libglib2.0-0", "libgomp1")
    .pip_install(
        "onnxruntime-gpu[cuda,cudnn]==1.22.0",
        "numpy==1.26.4",
        "opencv-python-headless==4.11.0.86",
        "pillow==11.2.1",
        "requests==2.32.4",
        "fastapi==0.115.12",
    )
    .env({
        "REQUIRE_CUDA": "1",
        "RUN_MODE": "beast",
        "CACHE_MODELS_DURING_WORKER": "1",
        "ONNX_INTRA_OP_THREADS": "4",
    })
    .add_local_dir(
        ROOT / "hivision", "/root/hivision", copy=True,
        ignore=modal.FilePatternMatcher("**/__pycache__/**", "**/*.pyc", "**/*.onnx"),
    )
    .add_local_file(ROOT / "api_server.py", "/root/api_server.py", copy=True)
    .add_local_file(ROOT / "modal_contract.py", "/root/modal_contract.py", copy=True)
)


def download_models():
    import urllib.request
    from pathlib import Path

    models = {
        "/root/hivision/creator/weights/birefnet-v1-lite.onnx": "https://github.com/ZhengPeng7/BiRefNet/releases/download/v1/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
        "/root/hivision/creator/retinaface/weights/retinaface-resnet50.onnx": "https://github.com/Zeyi-Lin/HivisionIDPhotos/releases/download/pretrained-model/retinaface-resnet50.onnx",
    }
    for name, url in models.items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=180) as response, path.open("wb") as target:
            import shutil
            shutil.copyfileobj(response, target)
        if path.stat().st_size < 10_000_000:
            raise RuntimeError("Model download was incomplete")
        print("Model ready:", path.name, path.stat().st_size)


gpu_image = gpu_image.run_function(download_models)


def run_heartbeat(payload, stop):
    import requests
    import time

    while not stop.is_set():
        try:
            response = requests.post(
                payload["bridge_url"] + "/heartbeat",
                headers={"Authorization": "Bearer " + payload["worker_credential"]},
                json={"ready": True}, timeout=20,
            )
            try:
                if response.status_code in (401, 409):
                    return
                response.raise_for_status()
            finally:
                response.close()
        except Exception:
            print("[ModalWorker] run heartbeat temporarily unavailable")
        if stop.wait(25):
            return


@app.function(
    image=gpu_image, gpu="L4", cpu=4, memory=16384,
    timeout=3600, max_containers=1, min_containers=0,
    scaledown_window=60, retries=0,
)
def gpu_worker(payload: dict, test_image: bytes | None = None, width: int = 295, height: int = 413):
    import threading
    import time
    import onnxruntime as ort

    stop = threading.Event()
    heartbeat = None
    backend = None
    run_id = payload.get("worker_run_id")
    if test_image is None:
        heartbeat = threading.Thread(target=run_heartbeat, args=(payload, stop), daemon=True)
        heartbeat.start()
        run_registry.put(run_id, {"status": "running", "updated_at": time.time()})
    try:
        ort.preload_dlls(directory="")
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            raise RuntimeError("CUDAExecutionProvider is unavailable")
        import api_server as backend
        import hivision.creator.human_matting as matting
        import hivision.creator.face_detector as detector
        from hivision.creator.retinaface.inference import load_onnx_model as load_retinaface

        backend._set_worker_model_cache(True)
        matting.BIREFNET_V1_LITE_SESS = matting.load_onnx_model(matting.WEIGHTS["birefnet-v1-lite"])
        detector.RETINAFCE_SESS = load_retinaface("/root/hivision/creator/retinaface/weights/retinaface-resnet50.onnx")
        providers = {
            "birefnet-v1-lite": matting.BIREFNET_V1_LITE_SESS.get_providers(),
            "retinaface": detector.RETINAFCE_SESS.get_providers(),
        }
        if any("CUDAExecutionProvider" not in values for values in providers.values()):
            raise RuntimeError("A model fell back to CPU execution")
        print("[ModalWorker] models ready gpu=L4 providers=", providers, flush=True)
        if test_image is not None:
            image, elapsed = backend._run_inference(test_image, width, height)
            return {"png": image, "inference_seconds": elapsed, "providers": providers, "gpu": "L4"}
        processed = backend._process_jobs(
            payload["bridge_url"], run_id, payload["worker_credential"], payload.get("max_jobs"),
        )
        return {"processed": processed, "gpu": "L4", "providers": providers}
    except Exception as exc:
        if test_image is None:
            import requests
            message = backend._safe_error(exc) if backend else "GPU worker initialization failed"
            try:
                with requests.post(
                    payload["bridge_url"] + "/finish",
                    headers={"Authorization": "Bearer " + payload["worker_credential"]},
                    json={"error": message}, timeout=20,
                ) as response:
                    response.raise_for_status()
            except Exception:
                print("[ModalWorker] could not report worker failure")
        import re
        safe_message = re.sub(r"https?://\S+", "<URL redacted>", str(exc))[:2000]
        raise RuntimeError(safe_message) from None
    finally:
        stop.set()
        if heartbeat:
            heartbeat.join(timeout=2)
        if backend:
            backend._clear_worker_model_cache()
            backend._set_worker_model_cache(False)
        if run_id:
            run_registry.put(run_id, {"status": "finished", "updated_at": time.time()})


@app.function(image=control_image, timeout=3720, max_containers=2, scaledown_window=30, retries=0)
async def watch_worker(payload: dict, call_id: str):
    """Observe GPU failures even if the GPU process cannot execute its finally."""
    import requests
    import time
    import re
    call = modal.FunctionCall.from_id(call_id)
    started = time.monotonic()
    error = None
    try:
        while True:
            try:
                await call.get.aio(timeout=20)
                break
            except modal.exception.FunctionTimeoutError:
                error = "Modal GPU worker exceeded its execution time limit"
                break
            except modal.exception.TimeoutError:
                state = await run_registry.get.aio(payload["worker_run_id"], {})
                if state.get("status") == "starting" and time.monotonic() - started > 600:
                    await call.cancel.aio()
                    error = "Modal GPU could not start within 10 minutes"
                    break
            except (modal.exception.ConnectionError, modal.exception.ServiceError, modal.exception.InternalError):
                continue
            except Exception as exc:
                error = re.sub(r"https?://\S+", "<URL redacted>", str(exc))[:2000]
                break
        if error:
            try:
                with requests.post(
                    payload["bridge_url"] + "/finish",
                    headers={"Authorization": "Bearer " + payload["worker_credential"]},
                    json={"error": error}, timeout=20,
                ) as response:
                    response.raise_for_status()
            except Exception:
                print("[ModalControl] worker failure callback unavailable")
    finally:
        active = await run_registry.get.aio("_active", {})
        if active.get("run_id") == payload["worker_run_id"]:
            await run_registry.pop.aio("_active", None)


@app.function(image=control_image, secrets=[control_secret], timeout=60, max_containers=2, scaledown_window=60)
@modal.asgi_app()
def web():
    from fastapi import FastAPI, HTTPException, Request
    from modal_contract import normalize_run_payload
    import hmac
    import os
    import time

    api = FastAPI(title="ID Photo Modal control", docs_url=None, redoc_url=None, openapi_url=None)

    def authenticate(request: Request):
        expected = os.environ.get("MODAL_BACKEND_TOKEN", "")
        header = request.headers.get("authorization", "")
        if not expected or not hmac.compare_digest(header, "Bearer " + expected):
            raise HTTPException(401, "Unauthorized")

    @api.get("/health")
    async def health(request: Request):
        authenticate(request)
        active = await run_registry.get.aio("_active", {})
        return {"status": "healthy", "backend": "modal", "gpu": "L4", "models": ["birefnet-v1-lite", "retinaface"], "worker_run_id": active.get("run_id")}

    @api.post("/process-queue")
    async def process_queue(payload: dict, request: Request):
        authenticate(request)
        try:
            parsed = normalize_run_payload(payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        run_id = parsed["worker_run_id"]
        inserted = await run_registry.put.aio(run_id, {"status": "starting", "updated_at": time.time()}, skip_if_exists=True)
        if not inserted:
            return {"status": "already_running", "worker_run_id": run_id, "backend": "modal"}
        await run_registry.put.aio("_active", {"run_id": run_id})
        call = None
        try:
            call = await gpu_worker.spawn.aio(parsed)
            await watch_worker.spawn.aio(parsed, call.object_id)
        except Exception:
            if call is not None:
                await call.cancel.aio(terminate_containers=True)
            await run_registry.pop.aio(run_id)
            active = await run_registry.get.aio("_active", {})
            if active.get("run_id") == run_id:
                await run_registry.pop.aio("_active", None)
            raise HTTPException(503, "GPU worker could not be scheduled")
        return {"status": "started", "worker_run_id": run_id, "backend": "modal", "call_id": call.object_id}

    return api
