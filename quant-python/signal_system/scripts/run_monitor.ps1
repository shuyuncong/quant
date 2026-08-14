$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $scriptRoot
python main.py monitor

