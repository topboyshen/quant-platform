[CmdletBinding()]
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$PiArguments
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$previousLocation = Get-Location
try {
  Set-Location -LiteralPath $projectRoot
  & pi --provider "ollama-cloud" --model "deepseek-v4-pro" --thinking "high" @PiArguments
  exit $LASTEXITCODE
}
finally {
  Set-Location -LiteralPath $previousLocation
}
