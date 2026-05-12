"""
Monthly Progress — $10K/month income target tracking.

Tracks:
  • Calendar-month gross P&L
  • Estimated take-home after prop firm split (default 80/20)
  • Progress vs. $10K target
  • Daily run-rate needed to hit target
  • Phase-based scaling plan

Phase 1 (Months 1-2): Pass evals + first payouts → $2K-$5K/mo
Phase 2 (Months 3-4): Scale to 2 funded accounts → $5K-$8K/mo
Phase 3 (Months 5+):  Multi-account systematic income → $10K+/mo
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import get_db_connection

logger = logging.getLogger(__name__)

MONTHLY_TARGET    = float(os.getenv("ICT_MONTHLY_TARGET", "10000"))
PROFIT_SPLIT      = float(os.getenv("ICT_PROFIT_SPLIT", "0.80"))   # Trader keeps 80%
TRADING_DAYS_MO   = int(os.getenv("ICT_TRADING_DAYS_MO", "20"))    # Approx 20 trading days


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _get_month_pnl(now: datetime) -> float:
    """Sum of closed-trade P&L within the current calendar month."""
    try:
        start = _month_start(now)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(pnl), 0) FROM ict_trades
                WHERE status = 'closed' AND closed_at >= ?
            """, (start.isoformat(),))
            row = cursor.fetchone()
            return float(row[0]) if row else 0.0
    except Exception as e:
        logger.error("Monthly: P&L query failed: %s", e)
        return 0.0


def _get_month_trade_count(now: datetime) -> tuple[int, int, int]:
    try:
        start = _month_start(now)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT outcome, COUNT(*) FROM ict_trades
                WHERE status = 'closed' AND closed_at >= ?
                GROUP BY outcome
            """, (start.isoformat(),))
            rows = cursor.fetchall()
            wins   = sum(c for o, c in rows if o == "win")
            losses = sum(c for o, c in rows if o == "loss")
            total  = wins + losses
            return total, wins, losses
    except Exception:
        return 0, 0, 0


def get_monthly_progress() -> dict:
    now = datetime.now(timezone.utc)
    gross  = _get_month_pnl(now)
    take   = gross * PROFIT_SPLIT
    target = MONTHLY_TARGET

    # Required take-home → gross needed
    gross_target = target / PROFIT_SPLIT

    total, wins, losses = _get_month_trade_count(now)
    win_rate = (wins / total * 100) if total > 0 else 0.0
    avg_win  = (gross / wins) if wins > 0 else 0.0

    # Days into month and days remaining
    days_into_month = now.day
    days_in_month   = (_month_start(now) + timedelta(days=32)).replace(day=1).day - 1
    days_remaining  = max(0, days_in_month - days_into_month)

    # Daily run-rate needed
    needed_gross = gross_target - gross
    daily_needed = needed_gross / max(1, days_remaining) if needed_gross > 0 else 0.0

    # Pace status
    expected_at_this_point = gross_target * (days_into_month / days_in_month)
    pace = "AHEAD" if gross >= expected_at_this_point else "BEHIND"
    pace_delta = gross - expected_at_this_point

    # Phase detection (rough heuristic from take-home over rolling 30d)
    rolling_30d_take = _rolling_30d_take()
    if rolling_30d_take < 3000:
        phase = "Phase 1 — Eval & first payouts"
    elif rolling_30d_take < 8000:
        phase = "Phase 2 — Scaling to multi-account"
    else:
        phase = "Phase 3 — $10K/mo systematic"

    return {
        "month_gross_pnl":     round(gross, 2),
        "month_take_home":     round(take, 2),
        "monthly_target":      target,
        "gross_target":        round(gross_target, 2),
        "progress_pct":        round((take / target * 100) if target > 0 else 0, 1),
        "trades_this_month":   total,
        "wins":                wins,
        "losses":              losses,
        "win_rate_pct":        round(win_rate, 1),
        "avg_winner":          round(avg_win, 2),
        "days_into_month":     days_into_month,
        "days_remaining":      days_remaining,
        "daily_gross_needed":  round(daily_needed, 2),
        "pace":                pace,
        "pace_delta":          round(pace_delta, 2),
        "phase":               phase,
        "profit_split":        PROFIT_SPLIT,
    }


def _rolling_30d_take() -> float:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(pnl), 0) FROM ict_trades
                WHERE status = 'closed' AND closed_at >= datetime('now', '-30 days')
            """)
            row = cursor.fetchone()
            gross = float(row[0]) if row else 0.0
            return gross * PROFIT_SPLIT
    except Exception:
        return 0.0
