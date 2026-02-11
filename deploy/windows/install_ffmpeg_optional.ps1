$ErrorActionPreference = "Continue"

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $zipPath = "C:\ffmpeg.zip"
    $extractPath = "C:\ffmpeg"

    Write-Host "Intentando descargar FFmpeg..."
    Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zipPath -UseBasicParsing

    Write-Host "Extrayendo FFmpeg..."
    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force

    $ffmpegDir = Get-ChildItem $extractPath -Directory | Select-Object -First 1
    if ($ffmpegDir -and (Test-Path (Join-Path $ffmpegDir.FullName "bin\ffmpeg.exe"))) {
        $ffmpegBin = Join-Path $ffmpegDir.FullName "bin"
        setx /M PATH ("$env:PATH;$ffmpegBin") | Out-Null
        Write-Host "FFmpeg instalado correctamente en $ffmpegBin"
    }
    else {
        Write-Host "FFmpeg no encontrado tras la extracción. Se continúa sin FFmpeg."
    }
}
catch {
    Write-Host "No se pudo instalar FFmpeg. Se continúa sin FFmpeg."
}

exit 0
