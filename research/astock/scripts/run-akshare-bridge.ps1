[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$bridgePath = Join-Path $projectRoot "mcp\akshare_bridge.py"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
  throw "The project .venv does not exist. Run npm run data:setup first."
}
if (-not (Test-Path -LiteralPath $bridgePath -PathType Leaf)) {
  throw "The AkShare stdin bridge does not exist: $bridgePath"
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
& $venvPython $bridgePath
exit $LASTEXITCODE
