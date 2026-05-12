# Register Windows Task Scheduler job for nightly ICT analyst
# Run once from the repo root:
#   .\scripts\register-nightly-analyst.ps1

$PYTHON = (Get-Command python).Source
$SCRIPT = Join-Path (Get-Location) "service\server\strategy_analyst.py"

if (-not (Test-Path $SCRIPT)) {
    Write-Host "ERROR: strategy_analyst.py not found at $SCRIPT" -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute $PYTHON `
    -Argument "`"$SCRIPT`"" `
    -WorkingDirectory (Join-Path (Get-Location) "service\server")

# 11:59 PM CST = 05:59 AM UTC next day — Task Scheduler uses local time
# Adjust the time to match 11:59 PM in YOUR local timezone
$trigger = New-ScheduledTaskTrigger -Daily -At "11:59PM"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 2)

try {
    Register-ScheduledTask `
        -TaskName "JarvisTradeAnalyst" `
        -Description "ICT AI-Trader nightly strategy analysis using Claude Opus" `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Force

    Write-Host "Task registered: JarvisTradeAnalyst" -ForegroundColor Green
    Write-Host "Runs nightly at 11:59 PM" -ForegroundColor Gray
    Write-Host ""
    Write-Host "To run manually: python service\server\strategy_analyst.py" -ForegroundColor Gray
    Write-Host "To view logs:    service\server\logs\analyst\" -ForegroundColor Gray
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Try running PowerShell as Administrator." -ForegroundColor Yellow
}
