#!/bin/bash
set -e

# Ir a la carpeta del proyecto (donde está el .env principal)
cd /opt/whapco

# Traer últimos cambios de Git
git pull origin main

# Levantar/actualizar servicios usando el docker-compose de deploy/linux
docker compose -f deploy/linux/docker-compose.yml up -d --build
