# ================================
# 🚀 STARTUP SCRIPT WHATSAPP API
# ================================

# ================================
# LOGGING
# ================================
$LogFile = "C:\whatsapp_api\startup.log"
Start-Transcript -Path $LogFile -Append

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
Write-Output "Pulling latest changes from Git..."
Set-Location "C:\whatsapp_api"
git pull origin main

# Asegurar DOCKER_HOST (lo tenías repetido; lo dejo una vez)
$env:DOCKER_HOST = "npipe:////./pipe/docker_engine"

# Primero bajar contenedores huérfanos
& $docker compose -f $composeFile down --remove-orphans

# (Opcional) Limpiar red custom si existe
& $docker network rm whapco_win 2>$null

# 🔥 CLAVE: forzar recreación y rebuild para que no quede "versión vieja"
# - --build: reconstruye la imagen (si tienes build:)
# - --force-recreate: recrea contenedores aunque "parezca igual"
# - --pull always: si usas image: también intenta traer lo último del tag
# - --no-cache: evita usar capas viejas
& $docker compose -f $composeFile up -d --build --force-recreate --pull always --no-cache

# Verificación rápida: estado y últimas líneas de logs de web (si existe)
Write-Output "Compose status:"
& $docker compose -f $composeFile ps

Write-Output "WhatsApp API + MySQL + Docker are now running!"
Stop-Transcript