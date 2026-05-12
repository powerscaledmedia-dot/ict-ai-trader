"""
Strategy Analyst — Nightly Claude Opus review of trade history.

Runs once per night via Windows Task Scheduler.
Reads the last 7 days of trades from SQLite, sends to Claude Opus 4.7,
parses updated setup weights, saves analysis log, and sends Telegram summary.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

LOG_DIR = Path(__file__).parent / "logs" / "analyst"
WEIGHTS_FILE = Path(__file__).parent / "setup_weights.json"

LOG_DIR.mkdir(parents=True, exist_ok=True)


def _fetch_trade_history(days: int = 7) -> list[dict]:
    """Fetch recent ICT trades from SQLite."""
    try:
        from database import get_db_connection
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    setup_type, direction, instrument, timeframe,
                    entry_price, exit_price, stop_price, target_price,
                    pnl, outcome, grade, score, killzone,
                    confluence_factors, opened_at, closed_at,
                    news_status
                FROM ict_trades
                WHERE closed_at >= datetime('now', ?)
                ORDER BY closed_at DESC
            """, (f"-{days} days",))
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        logger.error("Failed to fetch trade history: %s", e)
        return []


def _build_analysis_prompt(trades: list[dict]) -> str:
    if not trades:
        return "No trades found in the last 7 days. Please advise on standby strategy."

    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]

    summary_lines = []
    for t in trades:
        rr = 0.0
        if t.get("entry_price") and t.get("exit_price") and t.get("stop_price"):
            risk = abs(float(t["entry_price"]) - float(t["stop_price"]))
            reward = abs(float(t["exit_price"]) - float(t["entry_price"]))
            rr = round(reward / risk, 2) if risk > 0 else 0.0

        summary_lines.append(
            f"- {t.get('setup_type')} {t.get('direction')} {t.get('instrument')} "
            f"[{t.get('killzone', '?')} session] "
            f"Grade:{t.get('grade')} R:R:{rr} P&L:${t.get('pnl', 0):.2f} "
            f"Outcome:{t.get('outcome', '?')} "
            f"News:{t.get('news_status', '?')}"
        )

    trade_log = "\n".join(summary_lines)

    return f"""You are an expert ICT (Inner Circle Trader) trading analyst reviewing an automated trading system's performance.

The system trades MES, MNQ, GC, and SI futures using ICT concepts during Asia, London, and NY killzones.
Primary setups: FVG (Fair Value Gap), IFVG (Inverse FVG), Order Blocks, Liquidity Grabs, SMT Divergence.

Trade log — last 7 days ({len(trades)} trades, {len(wins)} wins, {len(losses)} losses):
{trade_log}

Please analyze and provide:

1. PERFORMANCE SUMMARY: Win rate, average R:R, best/worst sessions, best/worst setups.

2. PATTERN ANALYSIS: What patterns do you see in the losing trades? (time, setup type, news context, grade)

3. UPDATED SETUP WEIGHTS: Rate each setup 0.0–1.0 based on this data.
   Format as JSON: {{"FVG": 0.X, "IFVG": 0.X, "ORDER_BLOCK": 0.X, "LIQUIDITY_GRAB": 0.X, "SMT_DIVERGENCE": 0.X}}

4. RULE CHANGES: 3 specific, actionable rule changes to improve performance.
   Be concrete: "Raise minimum grade threshold to A-only during NY session" not "trade better setups".

5. EVAL PROGRESS: Are we on track to pass the TopStep 50K eval ($3,000 profit target, $2,000 daily loss limit)?
   What adjustments are needed?

Respond in structured sections. Be direct and data-driven."""


def _call_claude(prompt: str) -> Optional[str]:
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — skipping Claude analysis")
        return None

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-opus-4-7",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        data = resp.json()
        content = data.get("content", [{}])
        return content[0].get("text", "") if content else ""
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        return None


def _extract_weights(analysis_text: str) -> dict:
    """Parse the JSON setup weights block from Claude's response."""
    import re
    match = re.search(r'\{[^{}]*"FVG"[^{}]*\}', analysis_text)
    if match:
        try:
            weights = json.loads(match.group())
            # Validate all required keys present and in range
            required = {"FVG", "IFVG", "ORDER_BLOCK", "LIQUIDITY_GRAB", "SMT_DIVERGENCE"}
            if required.issubset(weights.keys()):
                return {k: max(0.0, min(1.0, float(v))) for k, v in weights.items()}
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_weights(weights: dict) -> None:
    if not weights:
        return
    WEIGHTS_FILE.write_text(json.dumps(weights, indent=2))
    logger.info("Setup weights updated: %s", weights)

    # Apply to running ict_scanner
    try:
        from ict_scanner import load_setup_weights
        load_setup_weights(weights)
    except Exception as e:
        logger.warning("Could not hot-reload weights: %s", e)


def _send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def run_nightly_analysis() -> None:
    """Main entry — called by Windows Task Scheduler at 23:59 CST."""
    logger.info("Strategy Analyst: starting nightly analysis")

    trades = _fetch_trade_history(days=7)
    prompt = _build_analysis_prompt(trades)
    analysis = _call_claude(prompt)

    if not analysis:
        logger.warning("Strategy Analyst: no analysis produced")
        return

    # Save full analysis log
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"analyst_{today}.md"
    log_path.write_text(f"# Nightly Analysis — {today}\n\n{analysis}")
    logger.info("Analysis saved to %s", log_path)

    # Extract and save updated weights
    weights = _extract_weights(analysis)
    if weights:
        _save_weights(weights)

    # Build Telegram summary (first 1000 chars of analysis)
    wins = len([t for t in trades if t.get("outcome") == "win"])
    total = len(trades)
    wr = f"{wins/total*100:.0f}%" if total > 0 else "N/A"

    telegram_msg = (
        f"*Nightly Trade Analysis — {today}*\n\n"
        f"Trades: {total} | Wins: {wins} | Win Rate: {wr}\n\n"
        f"{analysis[:800]}...\n\n"
        f"Full report: `logs/analyst/analyst_{today}.md`"
    )
    _send_telegram(telegram_msg)
    logger.info("Strategy Analyst: nightly analysis complete")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))

    logging.basicConfig(level=logging.INFO)
    run_nightly_analysis()
