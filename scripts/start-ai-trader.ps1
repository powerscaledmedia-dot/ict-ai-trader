# ================================================================
# Start AI-Trader ICT Platform
# Run this script from the ai-trader repo root:
#   .\scripts\start-ai-trader.ps1
# ================================================================

$ROOT = Split-Path $PSScriptRoot -Parent
$SERVER = Join-Path $ROOT "service\server"
$FRONTEND = Join-Path $ROOT "service\frontend"

Write-Host "=== AI-Trader ICT Platform ===" -ForegroundColor Cyan
Write-Host "Root: $ROOT" -ForegroundColor Gray

# Check Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Python not found. Install Python 3.11+" -ForegroundColor Red
    exit 1
}

# Check Node
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Node.js not found. Install Node.js 18+" -ForegroundColor Red
    exit 1
}

# Install backend deps if needed
$REQ = Join-Path $ROOT "service\requirements.txt"
Write-Host "Installing backend dependencies..." -ForegroundColor Yellow
python -m pip install -r $REQ -q

# Install ICT-specific deps
Write-Host "Installing ICT dependencies..." -ForegroundColor Yellow
python -m pip install yfinance feedparser anthropic -q

# Install frontend deps if node_modules missing
$NODE_MODULES = Join-Path $FRONTEND "node_modules"
if (-not (Test-Path $NODE_MODULES)) {
    Write-Host "Installing frontend dependencies..." -ForegroundColor Yellow
    Push-Location $FRONTEND
    npm install --silent
    Pop-Location
}

Write-Host ""
Write-Host "Starting backend server on http://localhost:8000 ..." -ForegroundColor Green
Start-Process -NoNewWindow -FilePath "python" -ArgumentList "-m uvicorn main:app --host 0.0.0.0 --port 8000 --reload" -WorkingDirectory $SERVER

Start-Sleep -Seconds 2

Write-Host "Starting frontend on http://localhost:3000 ..." -ForegroundColor Green
Start-Process -NoNewWindow -FilePath "npm" -ArgumentList "run dev" -WorkingDirectory $FRONTEND

Write-Host ""
Write-Host "=== AI-Trader ICT Platform Running ===" -ForegroundColor Cyan
Write-Host "  Backend API: http://localhost:8000" -ForegroundColor White
Write-Host "  API Docs:    http://localhost:8000/docs" -ForegroundColor White
Write-Host "  Dashboard:   http://localhost:3000" -ForegroundColor White
Write-Host "  ICT Status:  http://localhost:8000/ict/status" -ForegroundColor White
Write-Host ""
Write-Host "  Webhook URL for TradingView: http://YOUR_IP:8000/webhook/tradingview" -ForegroundColor Yellow
Write-Host ""
Write-Host "Press Ctrl+C to stop." -ForegroundColor Gray

# Keep running
try {
    while ($true) { Start-Sleep -Seconds 60 }
} finally {
    Write-Host "Stopping AI-Trader..." -ForegroundColor Red
    Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-Process node -ErrorAction SilentlyContinue | Stop-Process -Force
}
