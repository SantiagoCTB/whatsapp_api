# Usa una imagen ligera de Python
FROM python:3.10-slim

# Establece el directorio de trabajo
WORKDIR /app

# Copia los archivos del proyecto
COPY . .

# Instala dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Expone el puerto 8080 que usará Cloud Run
EXPOSE 8080

# Define la variable de entorno para Flask
ENV PORT=8080

# Comando de inicio
CMD ["gunicorn", "-b", ":8080", "app:app"]
