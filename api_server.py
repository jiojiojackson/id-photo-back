from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from PIL import Image
from hivision.creator import IDCreator
from hivision.creator.human_matting import extract_human_birefnet_lite
from hivision.creator.face_detector import detect_face_retinaface

import cv2
import io
import numpy as np
import os
import threading
import time


app = FastAPI(
    title="HivisionIDPhotos API",
    version="3.0.0",
)

# ---------------------------------------------------------
# HivisionIDPhotos
# ---------------------------------------------------------

creator = IDCreator()

creator.matting_handler = extract_human_birefnet_lite
creator.detection_handler = detect_face_retinaface

# BiRefNet + RetinaFace 内存占用较大
# 第一版单实例串行处理
inference_lock = threading.Lock()


# ---------------------------------------------------------
# Basic API
# ---------------------------------------------------------

@app.get("/")
def root():
    return {
        "service": "HivisionIDPhotos API",
        "status": "ok",
        "version": "3.0.0",
        "matting_model": "birefnet-v1-lite",
        "face_detect_model": "retinaface-resnet50",
        "output": "hd",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "run_mode": os.getenv("RUN_MODE", "normal"),
    }


@app.get("/debug/model-status")
def model_status():
    from hivision.creator import human_matting
    from hivision.creator import face_detector

    return {
        "run_mode": os.getenv("RUN_MODE", "normal"),
        "birefnet_loaded": (
            human_matting.BIREFNET_V1_LITE_SESS is not None
        ),
        "retinaface_loaded": (
            face_detector.RETINAFCE_SESS is not None
        ),
    }


# ---------------------------------------------------------
# Generate ID Photo
# ---------------------------------------------------------

@app.post("/generate")
async def generate(
    image: UploadFile = File(...),
    width: int = Form(295),
    height: int = Form(413),
):
    # -----------------------------------------------------
    # 参数校验
    # -----------------------------------------------------

    if width < 100 or width > 3000:
        raise HTTPException(
            status_code=400,
            detail="width must be between 100 and 3000 pixels",
        )

    if height < 100 or height > 3000:
        raise HTTPException(
            status_code=400,
            detail="height must be between 100 and 3000 pixels",
        )

    # -----------------------------------------------------
    # 读取图片
    # -----------------------------------------------------

    data = await image.read()

    if not data:
        raise HTTPException(
            status_code=400,
            detail="Empty image",
        )

    # -----------------------------------------------------
    # RGB -> BGR
    # -----------------------------------------------------

    try:
        pil_image = Image.open(
            io.BytesIO(data)
        ).convert("RGB")

        image_rgb = np.array(pil_image)

        image_bgr = cv2.cvtColor(
            image_rgb,
            cv2.COLOR_RGB2BGR,
        )

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image: {e}",
        )

    # -----------------------------------------------------
    # Hivision inference
    # -----------------------------------------------------

    start_time = time.time()

    try:

        with inference_lock:

            result = creator(
                image_bgr,
                size=(height, width),
                face_alignment=False,
            )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Inference failed: {e}",
        )

    elapsed = time.time() - start_time

    print(
        f"[API] size={width}x{height} "
        f"HD inference={elapsed:.3f}s"
    )

    # -----------------------------------------------------
    # ONLY return HD result
    # -----------------------------------------------------

    try:

        output = result.hd

        if output is None:
            raise RuntimeError(
                "HD result is None"
            )

        success, encoded = cv2.imencode(
            ".png",
            output,
        )

        if not success:
            raise RuntimeError(
                "Failed to encode PNG"
            )

        return Response(
            content=encoded.tobytes(),
            media_type="image/png",
            headers={
                "X-Photo-Width": str(width),
                "X-Photo-Height": str(height),
                "X-Inference-Time": f"{elapsed:.3f}",
            },
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Failed to encode HD result: {e}",
        )