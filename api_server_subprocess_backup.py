from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
from pathlib import Path
import subprocess
import uuid
import io
import os


app = FastAPI(
    title="HivisionIDPhotos API",
    version="1.0.0",
)

# /teamspace/studios/this_studio/HivisionIDPhotos
# 或 Docker 中的 /app/HivisionIDPhotos
PROJECT_DIR = Path(__file__).resolve().parent

BASE_DIR = Path("/tmp/hivision_api")
BASE_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/")
def root():
    return {
        "service": "HivisionIDPhotos",
        "status": "ok",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "project_dir": str(PROJECT_DIR),
    }


@app.post("/generate")
async def generate(
    image: UploadFile = File(...),
):
    job_id = uuid.uuid4().hex

    input_path = BASE_DIR / f"{job_id}.jpg"
    output_path = BASE_DIR / f"{job_id}.png"

    data = await image.read()

    if not data:
        raise HTTPException(
            status_code=400,
            detail="Empty image",
        )

    # 转换为 RGB，避免 RGBA PNG 导致 cv2.split() 通道错误
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.save(
            input_path,
            format="JPEG",
            quality=95,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image: {e}",
        )

    cmd = [
        "python",
        "inference.py",
        "-i",
        str(input_path),
        "-o",
        str(output_path),
        "--height",
        "413",
        "--width",
        "295",
        "--matting_model",
        "birefnet-v1-lite",
        "--face_detect_model",
        "retinaface-resnet50",
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Inference timeout",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start inference: {e}",
        )

    if result.returncode != 0:
        error = result.stderr[-5000:]

        raise HTTPException(
            status_code=500,
            detail=error,
        )

    if not output_path.exists():
        raise HTTPException(
            status_code=500,
            detail="Output image was not generated",
        )

    return FileResponse(
        path=output_path,
        media_type="image/png",
        filename="idphoto.png",
    )
