# =============================================
# 🚀 FAST STARTUP SCRIPT WHATSAPP API (WINDOWS)
# =============================================
# Despliegue rápido: actualiza código, reconstruye solo lo necesario
# y NO elimina volúmenes/imágenes/contenedores globalmente.

$LogFile = "C:\whatsapp_api\startup-fast.log"
Start-Transcript -Path $LogFile -Append

trap {
  try { Stop-Transcript | Out-Null } catch {}
  throw
}

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest


function Invoke-External {
  param(
    [Parameter(Mandatory = $true)]
    [scriptblock]$Command,
    [Parameter(Mandatory = $true)]
    [string]$Description
  )

  & $Command
  if ($LASTEXITCODE -ne 0) {
    throw "$Description failed with exit code $LASTEXITCODE"
  }
}

Write-Output "Starting Docker service..."
Start-Service com.docker.service -ErrorAction SilentlyContinue

Write-Output "Waiting for Docker to be ready..."
Start-Sleep -Seconds 10

Write-Output "Starting MySQL manually..."
$mysqlPath = "C:\Program Files\MySQL\MySQL Server 9.4\bin\mysqld.exe"
$defaults  = "C:\ProgramData\MySQL\MySQL Server 9.4\my.ini"
Start-Process -NoNewWindow -FilePath $mysqlPath -ArgumentList "--defaults-file=`"$defaults`" --console" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 8

$docker      = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
$composeFile = "C:\whatsapp_api\deploy\windows\docker-compose.windows.yml"

Write-Output "Generating pre-deploy backup..."
$backupArgs = @("scripts/backup_databases.py", "--env-file", ".env", "--tag", "windows-fast-deploy")
Start-Process -FilePath "python" -ArgumentList $backupArgs -NoNewWindow -PassThru -Wait -WorkingDirectory "C:\whatsapp_api"

Write-Output "Syncing repository with origin/main..."
Set-Location "C:\whatsapp_api"
git fetch --all --prune
git reset --hard origin/main
git clean -fd

$currentCommit = (git rev-parse --short HEAD)
Write-Output "Deploying commit: $currentCommit"

$env:DOCKER_HOST = "npipe:////./pipe/docker_engine"

Write-Output "Rebuilding web image without full cleanup..."
Invoke-External -Description "Docker compose build web" -Command { & $docker compose -f $composeFile build --pull --build-arg APP_COMMIT=$currentCommit web }

Write-Output "Updating running containers (no deps, no forced volume renewal)..."
Invoke-External -Description "Docker compose up web" -Command { & $docker compose -f $composeFile up -d --no-deps web }

Write-Output "Compose status:"
Invoke-External -Description "Docker compose ps" -Command { & $docker compose -f $composeFile ps }

Write-Output "Compose images:"
Invoke-External -Description "Docker compose images" -Command { & $docker compose -f $composeFile images }

Write-Output "Container commit label (org.opencontainers.image.revision):"
$webContainerRaw = & $docker compose -f $composeFile ps -q web
if ($LASTEXITCODE -ne 0) {
  throw "Docker compose ps -q web failed with exit code $LASTEXITCODE"
}

$webContainer = if ($webContainerRaw) { "$webContainerRaw".Trim() } else { "" }
if ($webContainer) {
  Invoke-External -Description "Docker inspect web container label" -Command { & $docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' $webContainer }
} else {
  Write-Output "No running 'web' container found after fast deploy."
}

Write-Output "Fast deploy completed successfully."
Stop-Transcript
