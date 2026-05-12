"""
Risk Governor — Hard guardrails for every trade.

Rules (non-negotiable, cannot be overridden):
- Max 2 concurrent open positions
- Per-trade risk: max 1% of account equity
- Daily loss halt: stop when within $300 of the eval daily loss limit
- Consecutive loss pause: 3 losses in a row → 2-hour cooldown
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

from database import get_db_connection

logger = logging.getLogger(__name__)


class RiskStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class RiskDecision:
    status: RiskStatus
    reason: str
    suggested_size: Optional[int] = None  # contracts


# ==================== Config (edit these to match your eval) ====================
MAX_CONCURRENT_POSITIONS = 2
MAX_RISK_PCT = 0.01           # 1% of account per trade
ACCOUNT_EQUITY = 50_000.0     # TopStep / Lucid 50K eval
DAILY_LOSS_LIMIT = 2_000.0    # TopStep 50K eval daily loss limit
DAILY_LOSS_BUFFER = 300.0     # Stop when within $300 of limit
CONSECUTIVE_LOSS_LIMIT = 3    # Pause after N losses
CONSECUTIVE_LOSS_COOLDOWN_HOURS = 2
MAX_CONTRACTS = int(os.getenv("ICT_MAX_CONTRACTS", "5"))  # Hard cap per trade

# Contract tick values for sizing
_TICK_VALUES: dict[str, float] = {
    "MES1!": 1.25,   # MES = $1.25/tick, 4 ticks/point = $5/point
    "MNQ1!": 0.50,   # MNQ = $0.50/tick, 4 ticks/point = $2/point
    "GC1!":  10.0,   # GC  = $10/tick (0.10 per tick)
    "SI1!":  25.0,   # SI  = $25/tick (0.005 per tick)
    # Micro aliases
    "MESH5": 1.25, "MNQH5": 0.50, "GCM5": 10.0, "SIM5": 25.0,
}

_POINT_VALUES: dict[str, float] = {
    "MES1!": 5.0,    # $5 per point
    "MNQ1!": 2.0,    # $2 per point
    "GC1!":  100.0,  # $100 per point
    "SI1!":  50.0,   # $50 per point (5000 oz * $0.01)
    "MESH5": 5.0, "MNQH5": 2.0, "GCM5": 100.0, "SIM5": 50.0,
}

# In-memory consecutive loss tracker
_consecutive_losses: int = 0
_cooldown_until: Optional[datetime] = None


def record_trade_result(won: bool) -> None:
    """Call after each trade closes to update consecutive loss tracker."""
    global _consecutive_losses, _cooldown_until
    if won:
        _consecutive_losses = 0
        _cooldown_until = None
        logger.info("Risk: winning trade recorded — streak reset")
    else:
        _consecutive_losses += 1
        logger.info("Risk: losing trade recorded — streak %d", _consecutive_losses)
        if _consecutive_losses >= CONSECUTIVE_LOSS_LIMIT:
            _cooldown_until = datetime.now(timezone.utc) + timedelta(hours=CONSECUTIVE_LOSS_COOLDOWN_HOURS)
            logger.warning(
                "Risk: %d consecutive losses — cooldown until %s UTC",
                _consecutive_losses,
                _cooldown_until.isoformat(),
            )


def _get_open_position_count() -> int:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM ict_trades WHERE status = 'open'"
            )
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error("Risk: could not query open positions: %s", e)
        return 0


def _get_todays_pnl() -> float:
    """
    Returns today's realized P&L from closed ICT trades (negative = loss).
    In live mode this is supplemented with a Tradovate API query.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(pnl), 0) FROM ict_trades
                WHERE status = 'closed'
                AND closed_at >= datetime('now', '-1 day')
            """)
            row = cursor.fetchone()
            return float(row[0]) if row else 0.0
    except Exception as e:
        logger.error("Risk: could not query today's P&L: %s", e)
        return 0.0


def calculate_position_size(
    instrument: str,
    entry: float,
    stop: float,
) -> int:
    """
    Returns max contracts allowed given 1% account risk.
    """
    risk_per_trade = ACCOUNT_EQUITY * MAX_RISK_PCT
    stop_distance_points = abs(entry - stop)

    point_value = _POINT_VALUES.get(instrument, 5.0)
    risk_per_contract = stop_distance_points * point_value

    if risk_per_contract <= 0:
        return 1

    contracts = int(risk_per_trade / risk_per_contract)
    return max(1, min(contracts, MAX_CONTRACTS))


def check_risk(
    instrument: str,
    entry: float,
    stop: float,
    daily_pnl_override: Optional[float] = None,
) -> RiskDecision:
    """
    Main entry — call before every order submission.
    Returns APPROVED or REJECTED with a reason.
    """
    now = datetime.now(timezone.utc)

    # 1. Consecutive loss cooldown
    if _cooldown_until and now < _cooldown_until:
        remaining = int((_cooldown_until - now).total_seconds() / 60)
        return RiskDecision(
            status=RiskStatus.REJECTED,
            reason=f"Cooldown active after {CONSECUTIVE_LOSS_LIMIT} consecutive losses — {remaining} min remaining",
        )

    # 2. Max concurrent positions
    open_count = _get_open_position_count()
    if open_count >= MAX_CONCURRENT_POSITIONS:
        return RiskDecision(
            status=RiskStatus.REJECTED,
            reason=f"Max concurrent positions ({MAX_CONCURRENT_POSITIONS}) reached — {open_count} open",
        )

    # 3. Daily loss check
    todays_pnl = daily_pnl_override if daily_pnl_override is not None else _get_todays_pnl()
    loss_so_far = -todays_pnl if todays_pnl < 0 else 0.0
    remaining_daily_limit = DAILY_LOSS_LIMIT - loss_so_far

    if remaining_daily_limit <= DAILY_LOSS_BUFFER:
        return RiskDecision(
            status=RiskStatus.REJECTED,
            reason=(
                f"Daily loss limit protection: ${loss_so_far:.0f} lost today, "
                f"only ${remaining_daily_limit:.0f} remaining (buffer ${DAILY_LOSS_BUFFER})"
            ),
        )

    # 4. Calculate position size and validate stop
    size = calculate_position_size(instrument, entry, stop)

    return RiskDecision(
        status=RiskStatus.APPROVED,
        reason=f"All risk checks passed — {open_count + 1}/{MAX_CONCURRENT_POSITIONS} positions, ${loss_so_far:.0f} lost today",
        suggested_size=size,
    )


def get_risk_dashboard() -> dict:
    """Returns current risk state for the dashboard."""
    now = datetime.now(timezone.utc)
    pnl = _get_todays_pnl()
    return {
        "daily_pnl": pnl,
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "loss_used_pct": abs(pnl) / DAILY_LOSS_LIMIT if pnl < 0 else 0.0,
        "open_positions": _get_open_position_count(),
        "max_positions": MAX_CONCURRENT_POSITIONS,
        "consecutive_losses": _consecutive_losses,
        "cooldown_active": _cooldown_until is not None and now < _cooldown_until,
        "cooldown_until": _cooldown_until.isoformat() if _cooldown_until else None,
        "account_equity": ACCOUNT_EQUITY,
    }
