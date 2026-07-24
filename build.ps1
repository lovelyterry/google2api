param (
    [switch]$OneFile,
    [switch]$Lto,
    [switch]$DisableClcache
)

$ErrorActionPreference = "Continue"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "Checking build environment..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$BaseDir = Get-Location
$VenvPython = "$BaseDir\.venv\Scripts\python.exe"
$VenvNuitkaDir = "$BaseDir\.venv\Lib\site-packages\nuitka"
$BuildDir = "$BaseDir\build"

if (-not (Test-Path -Path $VenvPython)) {
    Write-Host "[!] Error: Virtual environment Python ($VenvPython) not found!" -ForegroundColor Red
    Write-Host "Please make sure .venv environment exists." -ForegroundColor Yellow
    exit 1
}

Write-Host "[+] Using virtual environment Python: $VenvPython" -ForegroundColor Green

if (-not (Test-Path -Path $VenvNuitkaDir)) {
    Write-Host "[!] Nuitka is NOT installed in .venv." -ForegroundColor Yellow
    Write-Host "[*] Trying to install nuitka and zstandard into .venv..." -ForegroundColor Cyan

    if (Get-Command "uv" -ErrorAction SilentlyContinue) {
        Write-Host "[*] Found 'uv', installing using 'uv pip install'..." -ForegroundColor Cyan
        uv pip install nuitka zstandard --python $VenvPython
    } else {
        Write-Host "[*] Installing pip via ensurepip..." -ForegroundColor Cyan
        & $VenvPython -m ensurepip --default-pip
        & $VenvPython -m pip install nuitka zstandard
    }

    if (-not (Test-Path -Path $VenvNuitkaDir)) {
        Write-Host "[!] Error: Failed to install Nuitka into .venv!" -ForegroundColor Red
        Write-Host "[!] Please manually run: uv pip install nuitka zstandard" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "[+] Nuitka installed successfully!" -ForegroundColor Green
} else {
    Write-Host "[+] Nuitka is already installed in .venv" -ForegroundColor Green
}

$MainPy = "$BaseDir\main.py"
$FrontDir = "$BaseDir\front"
$CredsDir = "$BaseDir\creds"

if (-not (Test-Path -Path $MainPy)) {
    Write-Host "[!] Error: Entry file ($MainPy) not found!" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -Path $BuildDir)) {
    New-Item -ItemType Directory -Path $BuildDir | Out-Null
}

$ModeFlag = "--onefile"
if ($PSBoundParameters.ContainsKey('OneFile') -and -not $OneFile) {
    $ModeFlag = "--standalone"
}

$LtoValue = "no"
if ($Lto) {
    $LtoValue = "yes"
}

$NuitkaArgs = @(
    "-m", "nuitka",
    $ModeFlag,
    "--msvc=latest",
    "--disable-ccache",
    "--output-dir=$BuildDir",
    "--windows-console-mode=force",
    "--output-filename=google2api.exe",
    "--lto=$LtoValue",
    "--python-flag=-O",
    "--show-progress",
    "--show-memory",
    "--assume-yes-for-downloads",
    "--include-package=src",
    "--include-package=fastapi",
    "--include-package=hypercorn",
    "--include-package=curl_cffi",
    "--include-package=tiktoken",
    "--include-package=pydantic",
    "--include-package=pypinyin",
    "--include-package-data=pypinyin",
    "--include-package=starlette",
    "--include-package=dotenv",
    "--include-package=aiofiles",
    "--include-package-data=tiktoken",
    "--include-package-data=curl_cffi"
)

if (Test-Path -Path $FrontDir) {
    $NuitkaArgs += "--include-data-dir=$FrontDir=front"
    Write-Host "[+] Including static asset directory: front/" -ForegroundColor Green
}

$NuitkaArgs += $MainPy

Write-Host "`n==========================================" -ForegroundColor Cyan
Write-Host "Starting build [Mode: $ModeFlag, Compiler: MSVC (clcache disabled)]..." -ForegroundColor Cyan
Write-Host "Output Directory: $BuildDir" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "Command: $VenvPython $($NuitkaArgs -join ' ')" -ForegroundColor Gray
Write-Host ""

& $VenvPython $NuitkaArgs

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n==========================================" -ForegroundColor Green
    Write-Host "Build completed successfully!" -ForegroundColor Green
    Write-Host "==========================================" -ForegroundColor Green
    if ($ModeFlag -eq "--onefile") {
        Write-Host "Executable file: $BuildDir\google2api.exe" -ForegroundColor Yellow
    } else {
        Write-Host "Output directory: $BuildDir\main.dist" -ForegroundColor Yellow
        Write-Host "Main executable:  $BuildDir\main.dist\google2api.exe" -ForegroundColor Yellow
    }
    if (Test-Path -Path $CredsDir) {
        Write-Host "`nNote: Make sure 'creds' directory is in the same folder as google2api.exe when running." -ForegroundColor Yellow
    }
} else {
    Write-Host "`n[!] Build failed with exit code $LASTEXITCODE." -ForegroundColor Red
}
