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
  echo "Advertencia: No se encontró un intérprete de Python (python3 o python) en el PATH; se omite el respaldo." >&2
else
  set +e
  "$PYTHON_BIN" scripts/backup_databases.py --env-file .env --tag deploy
  BACKUP_STATUS=$?
  set -e

  if [ $BACKUP_STATUS -ne 0 ]; then
    echo "Advertencia: El respaldo previo al despliegue falló; se continúa sin detener el despliegue." >&2
  fi
fi

git pull origin main

# Levantar con el compose de linux
docker compose -f deploy/linux/docker-compose.yml up -d --build

