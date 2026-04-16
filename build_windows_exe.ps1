param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Missing .venv\Scripts\python.exe. Create the virtual environment and install dependencies first."
}

$Python = ".venv\Scripts\python.exe"

if ($Clean) {
    if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
    if (Test-Path "dist") { Remove-Item "dist" -Recurse -Force }
}

& $Python -m pip show pyinstaller | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed in the current virtual environment. Install it first, for example: .venv\Scripts\python.exe -m pip install pyinstaller"
}

$commonArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--add-data", "converter_assets;converter_assets",
    "--copy-metadata", "imageio",
    "--copy-metadata", "imageio-ffmpeg",
    "--copy-metadata", "numpy",
    "--copy-metadata", "Pillow",
    "--collect-all", "pywinauto",
    "--collect-all", "comtypes",
    "--collect-all", "imageio",
    "--collect-all", "imageio_ffmpeg"
)

& $Python @commonArgs "--name" "Recorder" "recorder_app.py" | Out-Host
& $Python @commonArgs "--name" "SessionViewer" "session_viewer_app.py" | Out-Host

Write-Host ""
Write-Host "Build completed. Output folders:"
Write-Host "  $ProjectRoot\dist\Recorder"
Write-Host "  $ProjectRoot\dist\SessionViewer"
Write-Host ""
Write-Host "Distribute the whole folder, not just the exe file."