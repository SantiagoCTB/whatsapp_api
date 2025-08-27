# ---- Base image ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PORT=5000

WORKDIR /app

# System deps (adjust/remove if you don't need them)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better layer caching)
# If you don't have requirements.txt, you can remove the next two lines.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt || true

# Copy the app
COPY . /app

# Create non-root user and fix ownership so app can write to /app/*
RUN useradd -m appuser && \
    mkdir -p /app/static/uploads /app/media && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

# Start with Gunicorn (fallback to python)
CMD bash -lc 'if command -v gunicorn >/dev/null 2>&1; then \
    exec gunicorn -w 2 -b 0.0.0.0:${PORT:-5000} app:app; \
  else \
    echo "Gunicorn not found; starting with python app.py" && exec python app.py; \
  fi'
