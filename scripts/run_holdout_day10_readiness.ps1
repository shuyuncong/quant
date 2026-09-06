$ErrorActionPreference = "Stop"

$repositoryRoot = "D:\development\github\quant"
$signalSystemDir = Join-Path $repositoryRoot "quant-python\signal_system"
$cachePreparationScript = "D:\tmp\prep_holdout_cache.py"
$executionDir = "D:\tmp\holdout_day10_execution"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$cacheLog = Join-Path $executionDir "cache_update_$timestamp.log"
$readinessJson = Join-Path $executionDir "holdout_readiness_$timestamp.json"
$summaryJson = Join-Path $executionDir "execution_summary_$timestamp.json"
$readyMarker = Join-Path $executionDir "READY_FOR_MANUAL_CANDIDATE_GENERATION.json"

New-Item -ItemType Directory -Path $executionDir -Force | Out-Null

$summary = [ordered]@{
    version = "holdout_day10_execution.v1"
    started_at = (Get-Date).ToString("o")
    repository = $repositoryRoot
    production_database_connected = $false
    generator_executed = $false
    cache_update_exit_code = $null
    readiness_exit_code = $null
    readiness_state = $null
    holdout_trading_days = $null
    action = "started"
}

try {
    if (-not (Test-Path -LiteralPath $cachePreparationScript)) {
        throw "Missing cache preparation script: $cachePreparationScript"
    }
    if (-not (Test-Path -LiteralPath $signalSystemDir)) {
        throw "Missing signal-system directory: $signalSystemDir"
    }

    Push-Location $signalSystemDir
    try {
        & python $cachePreparationScript `
            --adjust both `
            --workers 1 `
            --limit 800 `
            --interval 3 `
            --retries 8 *>&1 | Tee-Object -FilePath $cacheLog
        $summary.cache_update_exit_code = $LASTEXITCODE
        if ($LASTEXITCODE -ne 0) {
            throw "Local cache update failed with exit code $LASTEXITCODE"
        }

        $readinessOutput = & python "holdout_readiness_status.py" 2>&1
        $summary.readiness_exit_code = $LASTEXITCODE
        $readinessText = ($readinessOutput | Out-String).Trim()
        Set-Content -LiteralPath $readinessJson -Value $readinessText -Encoding UTF8

        try {
            $readiness = $readinessText | ConvertFrom-Json
        }
        catch {
            throw "Readiness output is not valid JSON: $($_.Exception.Message)"
        }

        $summary.readiness_state = $readiness.state
        $summary.holdout_trading_days = $readiness.index.holdout_trading_days

        if (
            $readiness.state -eq "ready_to_probe_candidates" -and
            [int]$readiness.index.holdout_trading_days -ge 10 -and
            $readiness.candidates.generated -eq $false
        ) {
            $marker = [ordered]@{
                status = "ready_for_main_model_review"
                created_at = (Get-Date).ToString("o")
                readiness_json = $readinessJson
                holdout_trading_days = [int]$readiness.index.holdout_trading_days
                generator_executed = $false
                instruction = "Stop and send the readiness JSON to the main model."
            }
            $marker | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $readyMarker -Encoding UTF8
            $summary.action = "ready_for_main_model_review"
        }
        else {
            $summary.action = "stopped_without_generation"
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    $summary.action = "failed"
    $summary.error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
}
finally {
    $summary.finished_at = (Get-Date).ToString("o")
    $summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryJson -Encoding UTF8
}

if ($summary.action -eq "failed") {
    exit 2
}
exit 0
