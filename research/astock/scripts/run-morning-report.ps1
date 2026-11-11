[CmdletBinding()]
param(
  [switch]$DryRun,
  [switch]$PrepareOnly,
  [switch]$StrictData,
  [switch]$TradingDaysOnly,
  [string]$Date,
  [string]$InputFile
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$nodeArgs = @("scripts/morning-report.mjs")
if ($DryRun) { $nodeArgs += "--dry-run" }
if ($PrepareOnly) { $nodeArgs += "--prepare-only" }
if ($StrictData) { $nodeArgs += "--strict-data" }
if ($TradingDaysOnly) { $nodeArgs += "--trading-days-only" }
if ($Date) { $nodeArgs += @("--date", $Date) }
if ($InputFile) { $nodeArgs += @("--input", $InputFile) }

$previousLocation = Get-Location
try {
  Set-Location -LiteralPath $projectRoot
  & node @nodeArgs
  exit $LASTEXITCODE
}
finally {
  Set-Location -LiteralPath $previousLocation
}
