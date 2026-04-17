# Adds DicTide to the current user's Windows Startup folder (logon).
param(
    [Parameter(Mandatory = $false)]
    [string] $TargetPath
)

$ErrorActionPreference = 'Stop'

$scriptRoot = $PSScriptRoot
if (-not $scriptRoot) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$defaultExe = [System.IO.Path]::GetFullPath(
    (Join-Path $scriptRoot '..\dist\DicTide\DicTide.exe')
)

$exePath = $null
if (-not [string]::IsNullOrWhiteSpace($TargetPath)) {
    if (-not (Test-Path -LiteralPath $TargetPath)) {
        Write-Host "Target does not exist:" -ForegroundColor Red
        Write-Host "  $TargetPath"
        Write-Host ""
        Write-Host "Fix the path or build the app, then run this script again."
        exit 1
    }
    $exePath = (Resolve-Path -LiteralPath $TargetPath).Path
}
else {
    if (-not (Test-Path -LiteralPath $defaultExe)) {
        Write-Host "DicTide.exe was not found at the default location:" -ForegroundColor Yellow
        Write-Host "  $defaultExe"
        Write-Host ""
        Write-Host "Build the executable first, or pass an explicit path:" -ForegroundColor Cyan
        Write-Host "  .\add_startup_shortcut.ps1 -TargetPath 'C:\path\to\DicTide.exe'"
        exit 1
    }
    $exePath = (Resolve-Path -LiteralPath $defaultExe).Path
}

$startupFolder = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
if (-not (Test-Path -LiteralPath $startupFolder)) {
    New-Item -ItemType Directory -Path $startupFolder -Force | Out-Null
}

$shortcutPath = Join-Path $startupFolder 'DicTide.lnk'
$workingDir = Split-Path -Parent $exePath

$w = New-Object -ComObject WScript.Shell
$shortcut = $w.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exePath
$shortcut.WorkingDirectory = $workingDir
$shortcut.Save()

Write-Host "Startup shortcut created:" -ForegroundColor Green
Write-Host "  $shortcutPath"
Write-Host "Target: $exePath"
Write-Host "Working directory: $workingDir"
