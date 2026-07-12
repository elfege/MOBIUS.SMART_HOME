# =============================================================================
# 0_MOBIUS.SMART_HOME Dockerfile
# Python 3.11 FastAPI application for Hubitat automation
# =============================================================================

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
#  - curl: healthcheck
#  - espeak-ng + ffmpeg: local Sonos TTS (services/sonos) — synthesize and
#    transcode announcement clips on the box; no cloud TTS. Without these,
#    only the pre-recorded canonical clips in static/audio/sonos/ play.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    espeak-ng \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY apps/ ./apps/
COPY services/ ./services/
COPY models/ ./models/
COPY config/ ./config/
COPY templates/ ./templates/
COPY static/ ./static/

# Create non-root user for security
# /app/logs is used by tee to write startup output for the nginx reloading page
RUN useradd -m -r appuser && mkdir -p /app/logs && chown -R appuser:appuser /app
USER appuser

# Expose application port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/api/health || exit 1

# Run with uvicorn for production
# Single worker: SSE requires shared memory (async handles concurrency)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000", "--workers", "1", "--timeout-keep-alive", "120"]
