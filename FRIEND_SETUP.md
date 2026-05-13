# Friend Setup Guide — ICT AI-Trader Platform

This guide sets up your own instance of the ICT trading platform so you can:
- Receive copy-traded signals from the main account
- Run your own 4-agent pipeline for your TopStep account
- View your own ICT dashboard

## Requirements

- Windows 10/11 (Mac/Linux also works)
- Python 3.10+ (`python --version` to check)
- Node.js 18+ (for the dashboard — optional)
- A prop firm eval account (TopStep via Tradovate, or Lucid via Rithmic)
- TradingView account with the Pine script loaded
- Telegram account (for trade notifications)

## Step 1 — Clone the Repo

```powershell
git clone https://github.com/powerscaledmedia-dot/ict-ai-trader.git
cd ict-ai-trader
```

Or download the ZIP from: https://github.com/powerscaledmedia-dot/ict-ai-trader/archive/refs/heads/main.zip

## Step 2 — Configure Your Environment

Copy the example env file:
```powershell
Copy-Item .env.example .env
```

Open `.env` and fill in YOUR credentials:

```env
# Your Tradovate credentials (TopStep uses Tradovate)
TRADOVATE_ENV=demo           # Keep as "demo" until you're live trading
TRADOVATE_USERNAME=your_username
TRADOVATE_PASSWORD=your_password
TRADOVATE_CID=               # From Tradovate API settings (optional)
TRADOVATE_SECRET=            # From Tradovate API settings (optional)

# Your Telegram bot for notifications
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Anthropic API key (for nightly analysis — optional)
ANTHROPIC_API_KEY=your_key

# Risk settings — adjust for your eval
ICT_ACCOUNT_EQUITY=50000
ICT_DAILY_LOSS_LIMIT=2000
ICT_DAILY_LOSS_BUFFER=300
```

## Step 3 — Install Dependencies

```powershell
# Backend (all dependencies in one command)
pip install -r service/requirements.txt
pip install "pydantic[email]"

# Frontend (optional — for the dashboard UI)
cd service/frontend
npm install
cd ../..
```

## Step 4 — Start the Platform

```powershell
.\scripts\start-ai-trader.ps1
```

This opens:
- Backend API: http://localhost:8000
- ICT Dashboard: http://localhost:3000/ict
- API Docs: http://localhost:8000/docs

## Step 5 — Connect TradingView

1. Open TradingView and load your chart (MES1!, MNQ1!, GC1!, SI1!)
2. Open `pine_scripts/ict_webhook_sender.pine` and add it to your chart
3. Create a TradingView Alert:
   - Condition: "Bull ICT Signal Fired" or "Bear ICT Signal Fired"
   - Webhook URL: `http://YOUR_PC_IP:8000/webhook/tradingview`
   - (Find your IP with: `ipconfig` in PowerShell — use your local IPv4)

## Step 6 — Copy Trading (Follow Main Account)

If the main account operator has enabled copy trading:

1. Register your agent at `http://localhost:3000/register`
2. Go to Copy Trading page
3. Follow the main account's signal provider ID (get this from your friend)
4. All their AI's signals will replicate to your account automatically

## Step 7 — Set Up Nightly Analysis (Optional)

To run the nightly Claude analysis automatically:

```powershell
# Register Windows Task Scheduler job
$action = New-ScheduledTaskAction -Execute "python" `
    -Argument "C:\path\to\ai-trader\service\server\strategy_analyst.py"
$trigger = New-ScheduledTaskTrigger -Daily -At "11:59PM"
Register-ScheduledTask -TaskName "ICTTradeAnalyst" -Action $action -Trigger $trigger
```

## Troubleshooting

**Backend won't start:**
- Check Python is 3.11+: `python --version`
- Check all deps: `pip install -r service/requirements.txt`
- Check `.env` has no formatting errors

**TradingView webhook not reaching server:**
- Make sure port 8000 is open in Windows Firewall
- Use your machine's local IP, not localhost (TV sends from their servers)
- For public internet access, use ngrok: `ngrok http 8000`

**Tradovate orders not executing:**
- Start with `TRADOVATE_ENV=demo` for paper trading
- Check credentials are correct
- Verify your account has API access enabled in Tradovate settings

**Dashboard shows "Connecting...":**
- Make sure backend is running on port 8000
- Check browser console for CORS errors
- Verify `CLAWTRADER_CORS_ORIGINS=http://localhost:3000` in `.env`

## Account Settings

Your risk rules (in `.env`) should match your TopStep eval:

| TopStep Eval | Profit Target | Daily Loss Limit |
|---|---|---|
| $25K | $1,500 | $1,500 |
| $50K | $3,000 | $2,000 |
| $100K | $6,000 | $3,000 |

Set `ICT_DAILY_LOSS_LIMIT` to YOUR eval's daily loss limit.
Set `ICT_DAILY_LOSS_BUFFER=300` to halt $300 before the limit (safety buffer).
