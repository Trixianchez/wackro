<#
.SYNOPSIS
  Builds Wackro.exe (normal) and Wackro-Admin.exe (asks for administrator) with PyInstaller.

.EXAMPLE
  .\venv\Scripts\Activate.ps1
  .\build.ps1                    # tests, then both exes
  .\build.ps1 -Only Normal       # just Wackro.exe
  .\build.ps1 -Only Admin        # just Wackro-Admin.exe
  .\build.ps1 -SkipTests         # don't run the unit tests first
  .\build.ps1 -Installer         # also dist\Wackro-Setup-<version>.exe (needs Inno Setup 6)

If PowerShell refuses to run the script: Set-ExecutionPolicy -Scope Process Bypass
Output goes to .\dist . This script never touches git.
#>
[CmdletBinding()]
param(
    [ValidateSet("Both", "Normal", "Admin")]
    [string]$Only = "Both",
    [switch]$SkipTests,
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Invoke-Step {
    param([string]$Title, [scriptblock]$Command)
    Write-Host ""
    Write-Host "==> $Title" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Step failed (exit code ${LASTEXITCODE}): $Title"
    }
}

if (-not $env:VIRTUAL_ENV) {
    throw "Activate the virtual environment first:  .\venv\Scripts\Activate.ps1"
}

$exeSuffix = if ($env:OS -eq "Windows_NT") { ".exe" } else { "" }

Invoke-Step "Installing requirements (runtime + PyInstaller)" {
    python -m pip install -r requirements-build.txt
}

if (-not $SkipTests) {
    Invoke-Step "Running the unit tests" {
        python tools\run_tests.py
    }
}

if (-not (Test-Path "assets\Wackro.ico")) {
    Write-Host "NOTE: assets\Wackro.ico not found, building without an icon." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==> Cleaning build\ and dist\" -ForegroundColor Cyan
foreach ($folder in @("build", "dist")) {
    if (Test-Path $folder) { Remove-Item -Recurse -Force $folder }
}

function Build-One {
    param([string]$Title, [bool]$Admin)
    if ($Admin) { $env:WACKRO_ADMIN = "1" } else { Remove-Item Env:\WACKRO_ADMIN -ErrorAction SilentlyContinue }
    try {
        Invoke-Step $Title {
            python -m PyInstaller --noconfirm --clean --distpath dist --workpath build Wackro.spec
        }
    }
    finally {
        Remove-Item Env:\WACKRO_ADMIN -ErrorAction SilentlyContinue
    }
}

$built = @()
if ($Only -eq "Both" -or $Only -eq "Normal") {
    Build-One "Building Wackro$exeSuffix (normal)" $false
    $built += "dist\Wackro$exeSuffix"
}
if ($Only -eq "Both" -or $Only -eq "Admin") {
    Build-One "Building Wackro-Admin$exeSuffix (asks for administrator)" $true
    $built += "dist\Wackro-Admin$exeSuffix"
}

if ($Installer) {
    $iscc = @(
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $iscc) {
        throw "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php, then run this again."
    }
    $version = (Select-String -Path config.py -Pattern '^APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
    $env:WACKRO_ONEDIR = "1"
    try {
        Invoke-Step "Building the installed version (folder build)" {
            python -m PyInstaller --noconfirm --clean --distpath dist\app --workpath build\onedir Wackro.spec
        }
    }
    finally {
        Remove-Item Env:\WACKRO_ONEDIR -ErrorAction SilentlyContinue
    }
    Invoke-Step "Building Wackro-Setup-$version.exe" {
        & $iscc "/DAppVersion=$version" installer\Wackro.iss
    }
    $built += "dist\Wackro-Setup-$version.exe"
}

Write-Host ""
Write-Host "==> Done" -ForegroundColor Green
foreach ($path in $built) {
    if (-not (Test-Path $path)) { throw "Expected output is missing: $path" }
    $file = Get-Item $path
    $hash = (Get-FileHash -Algorithm SHA256 $path).Hash
    "{0}  {1:N1} MB" -f $file.FullName, ($file.Length / 1MB)
    "    SHA-256: $hash"
}
