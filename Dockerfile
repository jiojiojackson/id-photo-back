FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RUN_MODE=beast

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-worker.txt ./

RUN pip install --upgrade pip && \
    pip install -r requirements.txt -r requirements-worker.txt

COPY . /app/HivisionIDPhotos

WORKDIR /app/HivisionIDPhotos

EXPOSE 8000

CMD ["uvicorn", "app_entry:app", "--host", "0.0.0.0", "--port", "8000"]
