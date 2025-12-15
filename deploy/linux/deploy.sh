#!/bin/bash
set -e

cd /opt/whapco

# Exportar todas las variables del .env al entorno
set -a
source .env
set +a

# Traer últimos cambios de Git
echo "Generando backup previo al despliegue..."
PYTHON_BIN=$(command -v python3 || command -v python || true)

if [ -z "$PYTHON_BIN" ]; then
  echo "No se encontró un intérprete de Python (python3 o python) en el PATH; abortando respaldo." >&2
  exit 1
fi

"$PYTHON_BIN" scripts/backup_databases.py --env-file .env --tag deploy || {
  echo "El respaldo previo al despliegue falló; abortando." >&2
  exit 1
}

git pull origin main

# Levantar con el compose de linux
docker compose -f deploy/linux/docker-compose.yml up -d --build

