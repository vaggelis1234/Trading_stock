# Creates a Desktop shortcut that opens the Trading Bot window.
# The shortcut uses this repository's .venv and the repository root as the
# working directory, so config, artifacts, and the user environment stay in place.
# Run from any directory:
#   powershell -ExecutionPolicy Bypass -File scripts\install_desktop_shortcut.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Entry = Join-Path $Root "scripts\desktop_control.py"

if (-not (Test-Path $Pythonw)) {
    throw "Missing $Pythonw. Create the project virtualenv before installing the shortcut."
}
if (-not (Test-Path $Entry)) {
    throw "Missing $Entry."
}

$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "Trading Bot.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = "`"$Entry`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.WindowStyle = 1
$Shortcut.Description = "Trading Bot Control Center"
$Shortcut.Save()

Write-Output "Created $ShortcutPath"
Write-Output "Target: $Pythonw `"$Entry`""
Write-Output "WorkingDirectory: $Root"
