# Reproducible build script for Bot-San-Francisco.exe (Fase 1F).
#
# Usage (from the project root or anywhere):
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#
# Requires: pip install -e .[build]  (pyinstaller + Pillow)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Generando icono desde el logo aprobado..."
python scripts\make_icon.py

Write-Host "Empaquetando Bot-San-Francisco.exe con PyInstaller..."
pyinstaller "Bot-San-Francisco.spec" --noconfirm

$ExePath = Join-Path $ProjectRoot "dist\Bot-San-Francisco.exe"
if (Test-Path $ExePath) {
    Write-Host ""
    Write-Host "Build completado: $ExePath"
} else {
    Write-Host ""
    Write-Host "ADVERTENCIA: no se encontro el exe esperado en $ExePath -- revisar la salida de PyInstaller arriba."
    exit 1
}
