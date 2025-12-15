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
$defaults = "C:\ProgramData\MySQL\MySQL Server 9.4\my.ini"

Start-Process -NoNewWindow -FilePath $mysqlPath -ArgumentList "--defaults-file=`"$defaults`" --console"

Start-Sleep -Seconds 8

# 3️⃣ Levantar DOCKER COMPOSE con tu compose.windows.yml
Write-Output "Starting Docker Compose..."

$docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
$composeFile = "C:\whatsapp_api\deploy\windows\docker-compose.windows.yml"
$python = "python"
$envFile = "C:\whatsapp_api\.env"
$backupScript = "C:\whatsapp_api\scripts\backup_databases.py"

Write-Output "Generating pre-deploy backup..."
$backupArgs = @("scripts/backup_databases.py", "--env-file", ".env", "--tag", "windows-deploy")
Start-Process -FilePath python -ArgumentList $backupArgs -NoNewWindow -PassThru -Wait -WorkingDirectory "C:\whatsapp_api"

# Primero bajar contenedores huérfanos
& $docker compose -f $composeFile down --remove-orphans

# Luego levantar con build si hace falta
& $docker compose -f $composeFile up -d --build

Write-Output "WhatsApp API + MySQL + Docker are now running!"

Stop-Transcript
