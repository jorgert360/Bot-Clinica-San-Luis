# Creates a Bot-San-Francisco.lnk shortcut on the current user's Desktop,
# pointing at the built exe (Fase 1F).
#
# Usage (from the project root or anywhere):
#   powershell -ExecutionPolicy Bypass -File scripts\create_desktop_shortcut.ps1
#
# Build the exe first (scripts\build_exe.ps1) if dist\Bot-San-Francisco.exe
# does not exist yet.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ExePath = Join-Path $ProjectRoot "dist\Bot-San-Francisco.exe"

if (-not (Test-Path $ExePath)) {
    Write-Host "No se encontro $ExePath -- ejecute scripts\build_exe.ps1 primero."
    exit 1
}

$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Bot-San-Francisco.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $ExePath
$Shortcut.WorkingDirectory = Split-Path -Parent $ExePath
$Shortcut.IconLocation = $ExePath
$Shortcut.Description = "Bot-San-Francisco -- Automatizacion de descarga de facturas"
$Shortcut.Save()

Write-Host "Acceso directo creado: $ShortcutPath"
Write-Host "Apunta a: $ExePath"
