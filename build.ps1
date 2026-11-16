<#
.SYNOPSIS
    Builds the Lossless Video Merger Windows executable.

.DESCRIPTION
    1. Creates/uses a virtual environment (.venv) and installs dependencies.
    2. Downloads FFmpeg (BtbN GPL shared build) into .\ffmpeg if it is missing.
    3. Runs the engine tests (skip with -SkipTests).
    4. Builds with PyInstaller into dist\LosslessVideoMerger\ and zips it.

.PARAMETER OneFile
    Build a single LosslessVideoMerger.exe instead of a folder. Starts slower,
    because FFmpeg is unpacked to a temp folder on every launch.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1
#>
param(
    [switch]$OneFile,
    [switch]$SkipTests,
    [string]$Python = "python",
    [string]$FFmpegUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip",
    [string]$WhisperUrl = "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip"
)

# "Continue": PyInstaller/pip log to stderr, which Windows PowerShell 5.1 would
# otherwise turn into terminating errors. Native failures are caught via $LASTEXITCODE.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
Set-Location $PSScriptRoot
$AppName = "LosslessVideoMerger"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# 1. Virtual environment ------------------------------------------------------
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Step "Creating virtual environment"
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create venv with '$Python'" }
}
Step "Installing dependencies"
& $venvPy -m pip install --disable-pip-version-check -q -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# 2. FFmpeg ---------------------------------------------------------------------
if (-not ((Test-Path "ffmpeg\ffmpeg.exe") -and (Test-Path "ffmpeg\ffprobe.exe"))) {
    Step "Downloading FFmpeg"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("lvm-ffmpeg-" + [guid]::NewGuid())
    New-Item -ItemType Directory $tmp | Out-Null
    try {
        Invoke-WebRequest -Uri $FFmpegUrl -OutFile "$tmp\ffmpeg.zip"
        Expand-Archive "$tmp\ffmpeg.zip" -DestinationPath "$tmp\x"
        $bin = Get-ChildItem -Recurse "$tmp\x" -Directory -Filter bin | Select-Object -First 1
        New-Item -ItemType Directory -Force ffmpeg | Out-Null
        Get-ChildItem $bin.FullName | Where-Object { $_.Name -ne "ffplay.exe" } |
            Copy-Item -Destination ffmpeg
        $lic = Get-ChildItem -Recurse "$tmp\x" -Filter LICENSE.txt | Select-Object -First 1
        if ($lic) { Copy-Item $lic.FullName ffmpeg\LICENSE.txt }
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}
& .\ffmpeg\ffmpeg.exe -hide_banner -version | Select-Object -First 1

# 2b. whisper.cpp (offline speech recognition for auto captions) --------------------
if (-not (Test-Path "whisper\whisper-cli.exe")) {
    Step "Downloading whisper.cpp"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("lvm-whisper-" + [guid]::NewGuid())
    New-Item -ItemType Directory $tmp | Out-Null
    try {
        Invoke-WebRequest -Uri $WhisperUrl -OutFile "$tmp\whisper.zip"
        Expand-Archive "$tmp\whisper.zip" -DestinationPath "$tmp\x"
        $cli = Get-ChildItem -Recurse "$tmp\x" -Filter whisper-cli.exe | Select-Object -First 1
        New-Item -ItemType Directory -Force whisper | Out-Null
        Get-ChildItem $cli.DirectoryName | Where-Object { $_.Name -eq "whisper-cli.exe" -or $_.Name -eq "whisper.dll" -or $_.Name -like "ggml*.dll" } |
            Copy-Item -Destination whisper
        Invoke-WebRequest "https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/LICENSE" -OutFile whisper\LICENSE.txt
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

# 3. Tests ------------------------------------------------------------------------
if (-not $SkipTests) {
    Step "Running engine and editor tests"
    & $venvPy -m unittest tests.test_engine tests.test_render tests.test_formats
    if ($LASTEXITCODE -ne 0) { throw "Tests failed - not building." }
}

# 4. Icon + PyInstaller -----------------------------------------------------------
Step "Rendering icon"
& $venvPy tools\make_icon.py
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed" }

Step "Building executable"
Remove-Item -Recurse -Force build, "dist\$AppName", "dist\$AppName.exe" -ErrorAction SilentlyContinue
$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
# One-file builds must embed FFmpeg; folder builds get it copied in afterwards
# (embedding makes PyInstaller duplicate every FFmpeg DLL).
$dataArgs = if ($OneFile) { @("--add-data", "ffmpeg;ffmpeg") } else { @() }
$excludes = @(
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
    "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtOpenGL", "PySide6.QtSql", "PySide6.QtTest", "tkinter"
) | ForEach-Object { "--exclude-module=$_" }

& $venvPy -m PyInstaller main.py `
    --name $AppName `
    $mode `
    --windowed `
    --noconfirm `
    --clean `
    --icon assets\icon.ico `
    @dataArgs `
    @excludes
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

if ($OneFile) {
    $exe = "dist\$AppName.exe"
} else {
    $exe = "dist\$AppName\$AppName.exe"
    # Qt parts this app never loads (software OpenGL, QML/Quick, PDF). The video
    # preview needs Qt6Multimedia + its FFmpeg plugin (and Qt6Network), which stay.
    $qt = "dist\$AppName\_internal\PySide6"
    "opengl32sw.dll", "Qt6Quick*.dll", "Qt6Qml*.dll", "Qt6Pdf*.dll", "Qt6VirtualKeyboard*.dll" |
        ForEach-Object { Remove-Item "$qt\$_" -ErrorAction SilentlyContinue }
    Remove-Item -Recurse "$qt\plugins\platforminputcontexts" -ErrorAction SilentlyContinue
    Copy-Item -Recurse ffmpeg "dist\$AppName\ffmpeg"
    if (Test-Path whisper) { Copy-Item -Recurse whisper "dist\$AppName\whisper" }
    Copy-Item README.md, LICENSE, THIRD_PARTY_NOTICES.md "dist\$AppName\" -Force
    Step "Creating zip"
    $zip = "dist\$AppName-win64.zip"
    Remove-Item $zip -ErrorAction SilentlyContinue
    Compress-Archive -Path "dist\$AppName" -DestinationPath $zip -CompressionLevel Optimal
    Write-Host "Zip: $zip ($([math]::Round((Get-Item $zip).Length / 1MB)) MB)"
}
Write-Host "`nDone: $exe" -ForegroundColor Green
