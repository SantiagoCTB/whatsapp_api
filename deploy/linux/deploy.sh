#!/bin/bash
set -e

cd /opt/whapco

# Exportar todas las variables del .env al entorno
set -a
source .env
set +a

# Traer últimos cambios de Git
git pull origin main

# Levantar con el compose de linux
docker compose -f deploy/linux/docker-compose.yml up -d --build

