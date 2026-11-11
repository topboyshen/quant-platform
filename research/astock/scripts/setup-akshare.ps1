[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$venvDirectory = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
  throw "Missing dependency file: $requirementsPath"
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
  $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
  if ($pyLauncher) {
    Write-Host "Creating the project Python environment (.venv)..."
    & $pyLauncher.Source -3 -m venv $venvDirectory
  }
  else {
    $pythonLauncher = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if (-not $pythonLauncher) {
      throw "Python 3 was not found. Install Python 3.11 or later, then run npm run data:setup again."
    }
    Write-Host "Creating the project Python environment (.venv)..."
    & $pythonLauncher.Source -m venv $venvDirectory
  }

  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Failed to create .venv (exit $LASTEXITCODE)."
  }
}

$env:PYTHONUTF8 = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

Write-Host "Installing pinned dependencies from requirements.txt..."
& $venvPython -m pip install --requirement $requirementsPath
if ($LASTEXITCODE -ne 0) {
  throw "Failed to install data dependencies (exit $LASTEXITCODE)."
}

Write-Host "Checking Python dependency consistency..."
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) {
  throw "Python dependency check failed (exit $LASTEXITCODE)."
}

$versions = & $venvPython -c "import importlib.metadata as m; print('akshare=' + m.version('akshare')); print('mcp=' + m.version('mcp'))"
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read the installed AkShare/MCP versions."
}

Write-Host "Data environment is ready:"
$versions | ForEach-Object { Write-Host "  $_" }
Write-Host "Next: npm run data:test"
