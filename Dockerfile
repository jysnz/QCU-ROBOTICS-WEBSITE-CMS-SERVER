FROM python:3.10-slim

# Install FFmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run Gunicorn with Uvicorn workers for production
# -w 1: a single worker -- video processing is memory-heavy (ffmpeg) and this
# service doesn't need request concurrency, so more workers just multiply
# the per-process memory overhead (boto3, supabase, etc.) without benefit.
# -k uvicorn.workers.UvicornWorker: tells gunicorn to use uvicorn for async support
CMD gunicorn main:app -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
