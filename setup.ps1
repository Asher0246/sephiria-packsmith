$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root "runtime"
$runtimePython = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $runtimePython)) {
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($launcher) {
    & $launcher.Source -3.12 -m venv $venv
  }
  if (-not (Test-Path $runtimePython) -and $python) {
    & $python.Source -m venv $venv
  }
  if (-not (Test-Path $runtimePython)) {
    throw "Python 3.12 is required. Install it from python.org, then run setup.ps1 again."
  }
}

& $runtimePython -m pip install -r (Join-Path $root "requirements-runtime.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
Write-Host "Runtime dependencies are ready."
