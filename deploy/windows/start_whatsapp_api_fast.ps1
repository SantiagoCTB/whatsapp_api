# ================================
# 🚀 STARTUP SCRIPT WHATSAPP API
# ================================

# ================================
# LOGGING
# ================================
$LogFile = "C:\whatsapp_api\startup.log"
Start-Transcript -Path $LogFile -Append

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# 1️⃣ Asegurar que Docker Desktop Service está iniciado
Write-Output "Starting Docker service..."
Start-Service com.docker.service -ErrorAction SilentlyContinue

# Esperar a que Docker esté completamente disponible
Write-Output "Waiting for Docker to be ready..."
Start-Sleep -Seconds 10

# 🔥 Cerrar TODOS los servicios y procesos MySQL antes de iniciar
Write-Output "Closing MySQL manually..."
Get-Service *mysql* -ErrorAction SilentlyContinue | Stop-Service -Force -ErrorAction SilentlyContinue
Get-Process *mysql* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# 2️⃣ Iniciar MySQL manualmente (ya que tu servicio falla)
Write-Output "Starting MySQL manually..."

$mysqlPath = "C:\Program Files\MySQL\MySQL Server 9.4\bin\mysqld.exe"
$defaults  = "C:\ProgramData\MySQL\MySQL Server 9.4\my.ini"

Start-Process -NoNewWindow -FilePath $mysqlPath -ArgumentList "--defaults-file=`"$defaults`" --console"
Start-Sleep -Seconds 8

# 3️⃣ Levantar DOCKER COMPOSE con tu compose.windows.yml
Write-Output "Starting Docker Compose..."

$docker      = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
$composeFile = "C:\whatsapp_api\deploy\windows\docker-compose.windows.yml"

# Backup pre-deploy (lo mantengo tal cual)
Write-Output "Generating pre-deploy backup..."
$backupArgs = @("scripts/backup_databases.py", "--env-file", ".env", "--tag", "windows-deploy")
Start-Process -FilePath "python" -ArgumentList $backupArgs -NoNewWindow -PassThru -Wait -WorkingDirectory "C:\whatsapp_api"

# Traer los últimos cambios del repositorio antes de reconstruir
# Usamos fetch + reset para garantizar que despliegue exactamente lo que está
# en origin/main (evita quedarse con código viejo por merges pendientes).
Write-Output "Syncing repository with origin/main..."
Set-Location "C:\whatsapp_api"
git fetch --all --prune
git reset --hard origin/main
git clean -fd

$currentCommit = (git rev-parse --short HEAD)
Write-Output "Deploying commit: $currentCommit"

# Asegurar DOCKER_HOST (lo tenías repetido; lo dejo una vez)
$env:DOCKER_HOST = "npipe:////./pipe/docker_engine"

# Primero bajar contenedores, imágenes y volúmenes del stack para eliminar estado viejo
Write-Output "Removing current stack (containers/images/volumes)..."
& $docker compose -f $composeFile down --remove-orphans --volumes --rmi all

# Limpiar cachés globales de build/imágenes para evitar reutilizar capas antiguas
Write-Output "Pruning Docker build cache and dangling images..."
& $docker builder prune -af
& $docker image prune -af
& $docker system prune -af --volumes

# (Opcional) Limpiar red custom si existe
& $docker network rm whapco_win 2>$null

# Reconstrucción total sin caché + recreación forzada
Write-Output "Building images from scratch..."
& $docker compose -f $composeFile build --no-cache --pull --build-arg APP_COMMIT=$currentCommit

Write-Output "Starting fresh containers..."
& $docker compose -f $composeFile up -d --force-recreate --renew-anon-volumes

# Verificación rápida: estado y últimas líneas de logs de web (si existe)
Write-Output "Compose status:"
& $docker compose -f $composeFile ps

Write-Output "Compose images:"
& $docker compose -f $composeFile images

Write-Output "Container commit label (org.opencontainers.image.revision):"
$webContainer = (& $docker compose -f $composeFile ps -q web).Trim()
if ($webContainer) {
  & $docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' $webContainer
}

Write-Output "WhatsApp API + MySQL + Docker are now running!"
Stop-Transcript
