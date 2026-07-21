#!/bin/bash
# Start Gunicorn processes with Uvicorn workers
# For production environments

# Set default workers if not provided
WORKERS=${GUNICORN_WORKERS:-4}
PORT=${PORT:-8000}

echo "Starting Gunicorn with $WORKERS workers on port $PORT..."
gunicorn backend.main:app \
  --workers $WORKERS \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:$PORT \
  --timeout 120
