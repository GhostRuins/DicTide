# Build DicTide:
# - Folder distribution: dist\DicTide\DicTide.exe
# - Optional single installer (Inno Setup): dist\installer\DicTideSetup-*.exe
# Requires: Python 3.10+ with pip, venv recommended.

param(
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Create a venv first: python -m venv .venv" -ForegroundColor Yellow
    exit 1
}

& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
& .\.venv\Scripts\python.exe -m PyInstaller DicTide.spec --clean --noconfirm

$outDir = Join-Path $PSScriptRoot "dist\DicTide"
if (Test-Path (Join-Path $PSScriptRoot "DEPLOY_NOTE.txt")) {
    Copy-Item -Force (Join-Path $PSScriptRoot "DEPLOY_NOTE.txt") $outDir
}

Write-Host "Done. Copy the whole folder: $outDir" -ForegroundColor Green
Write-Host "Run: $outDir\DicTide.exe" -ForegroundColor Green

if ($Installer) {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        Write-Host "Inno Setup compiler (ISCC.exe) not found in standard paths." -ForegroundColor Yellow
        Write-Host "Install Inno Setup 6 and run again with -Installer." -ForegroundColor Yellow
        exit 1
    }
    & $iscc ".\DicTideInstaller.iss"
    $installerDir = Join-Path $PSScriptRoot "dist\installer"
    Write-Host "Installer created under: $installerDir" -ForegroundColor Green
}
