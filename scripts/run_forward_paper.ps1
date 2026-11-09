param(
    [switch]$Init,
    [switch]$Sync,
    [switch]$Calendar,
    [double]$Capital = 500000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$signalSystem = Join-Path $root "quant-python\signal_system"
$env:ETF_ROTATION_STATE_DIR = Join-Path $signalSystem "state\forward_paper_500k"

$arguments = @("forward_paper.py")
if ($Init) {
    $arguments += @("--action", "init", "--capital", "$Capital")
} else {
    $arguments += @("--action", "cycle")
    if ($Sync) { $arguments += "--sync" }
    if ($Calendar) { $arguments += "--calendar" }
}

Push-Location $signalSystem
try {
    & python @arguments
} finally {
    Pop-Location
}
