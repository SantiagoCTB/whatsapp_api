# Stage frontend
FROM node:20 AS frontend
WORKDIR /app
COPY frontend/ ./frontend/
RUN npm ci --prefix frontend && npm run build --prefix frontend

# Stage backend
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend /app/static ./static
EXPOSE 5000
CMD ["sh","-c","gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 4"]
