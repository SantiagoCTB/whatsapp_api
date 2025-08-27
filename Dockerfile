########## Frontend build stage ##########
FROM node:18 AS build-frontend

WORKDIR /app/frontend
COPY frontend/ .
RUN npm install && npm run build

########## Final image ##########
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

WORKDIR /app

# System deps (adjust/remove if you don't need them)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better layer caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt || true

# Copy the app source
COPY . /app

# Copy built frontend assets
COPY --from=build-frontend /app/frontend/dist /app/static/

# Create non-root user and fix ownership so app can write to /app/*
RUN useradd -m appuser && \
    mkdir -p /app/static/uploads /app/media && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE ${PORT}

# Run the Flask app with Gunicorn
CMD bash -lc 'gunicorn -w 2 -b 0.0.0.0:${PORT:-5000} app:app'
