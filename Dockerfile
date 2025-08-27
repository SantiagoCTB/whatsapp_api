# ---- Base image ----
FROM python:3.11-slim

# Prevents Python from writing .pyc files & enables unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1

# Optional: set a default PORT (Railway sets PORT automatically)
ENV PORT=5000

# Workdir
WORKDIR /app

# System deps (safe defaults for common libs like mysqlclient/psycopg2). Remove if not needed.
RUN apt-get update && apt-get install -y --no-install-recommends \ 
    build-essential \ 
    default-libmysqlclient-dev \ 
    pkg-config \ 
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better layer caching)
# If you don't have requirements.txt, delete the next two lines.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app
COPY . /app

# Create non-root user
RUN useradd -m appuser
USER appuser

# Expose for local runs (informational)
EXPOSE 5000

# Start with Gunicorn (add `gunicorn` to requirements.txt)
# Falls back to Python if Gunicorn isn't installed
CMD bash -lc 'if command -v gunicorn >/dev/null 2>&1; then \
    exec gunicorn -w 2 -b 0.0.0.0:${PORT:-5000} app:app; \
  else \
    echo "Gunicorn no encontrado; iniciando con python app.py" && exec python app.py; \
  fi'