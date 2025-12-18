FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    build-essential \
    tesseract-ocr \
    tesseract-ocr-spa \
    libtesseract-dev \
    libleptonica-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos todo el proyecto
COPY . .

# Puerto interno del contenedor
ENV PORT=8000
EXPOSE 8000

# Tu app ASGI es asgi_app en el módulo asgi.py
CMD ["uvicorn", "asgi:asgi_app", "--host", "0.0.0.0", "--port", "8000"]
