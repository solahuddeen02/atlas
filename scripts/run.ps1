param(
  [Parameter(Position=0)]
  [ValidateSet("bootstrap","dev","test","clean","deps")]
  [string]$Task = "dev"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Ensure-Venv {
  if (-not (Test-Path $VenvPython)) {
    Write-Host "'.venv' not found. Creating venv..." -ForegroundColor Yellow
    & py -3.11 -m venv (Join-Path $ProjectRoot ".venv")
  }
}

Set-Location $ProjectRoot
Ensure-Venv

switch ($Task) {
  "bootstrap" {
    & $VenvPython -m pip install -U pip
    if (Test-Path (Join-Path $ProjectRoot "requirements.txt")) {
      & $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
    } else {
      & $VenvPython -m pip install fastapi uvicorn sqlalchemy pytest httpx
    }
    Write-Host "Bootstrap done." -ForegroundColor Green
  }

  "deps" {
    if (Test-Path (Join-Path $ProjectRoot "requirements.txt")) {
      & $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
    } else {
      Write-Host "No requirements.txt found." -ForegroundColor Yellow
    }
  }

  "dev" {
    & $VenvPython -m uvicorn core.main:app --reload
  }

  "test" {
    & $VenvPython -m pytest
  }

  "clean" {
    if (Test-Path (Join-Path $ProjectRoot ".pytest_cache")) {
      Remove-Item -Recurse -Force (Join-Path $ProjectRoot ".pytest_cache")
    }
    Get-ChildItem -Recurse -Force -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Recurse -Force -Filter "*.pyc" | Remove-Item -Force -ErrorAction SilentlyContinue
    Write-Host "Clean done." -ForegroundColor Green
  }

  default {
    throw "Unknown task: $Task"
  }
}
