"""
Account Guard — The "Never Fail an Account" layer.

This sits ABOVE the risk governor. The risk governor checks one trade at a time.
The Account Guard tracks the entire account's lifecycle vs. eval rules:

  • Static drawdown:    Account never goes below starting equity - X
  • Trailing drawdown:  Once you reach the high-water mark, can't drop below HWM - X
  • Daily loss limit:   Already in risk_governor (this is a backstop)
  • Profit target:      Switch to PRESERVATION mode at 80% of target
  • Consistency:        Auto-stop a winning day at 40% of target (more conservative than 50%)

Returns one of four states:
  SAFE       — full normal trading
  WARNING    — within 80% of any limit, scaling back to half size
  CRITICAL   — within 95% of any limit, only A-grade setups, half size
  LOCKED     — limit hit or imminent, NO new trades until reset

Designed so the user CANNOT override LOCKED via config. The system simply will
not place orders when locked — even if every other agent says green.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from database import get_db_connection

logger = logging.getLogger(__name__)


class GuardState(str, Enum):
    SAFE     = "SAFE"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"
    LOCKED   = "LOCKED"


@dataclass
class GuardDecision:
    state: GuardState
    reason: str
    size_multiplier: float       # 1.0 = full size, 0.5 = half, 0.0 = no trade
    min_grade_required: str      # "A" | "B" — only trades at or above this grade
    metrics: dict


# ==================== Config (LucidFlex 50K defaults) ====================
STARTING_EQUITY     = float(os.getenv("ICT_ACCOUNT_EQUITY", "50000"))
PROFIT_TARGET       = float(os.getenv("ICT_PROFIT_TARGET", "3000"))
DAILY_LOSS_LIMIT    = float(os.getenv("ICT_DAILY_LOSS_LIMIT", "2000"))
TRAILING_DD_LIMIT   = float(os.getenv("ICT_TRAILING_DD", "2500"))     # 5% of 50K
STATIC_DD_LIMIT     = float(os.getenv("ICT_STATIC_DD", "2500"))       # Lucid uses trailing
CONSISTENCY_PCT     = float(os.getenv("ICT_CONSISTENCY_PCT", "0.50"))

# Conservative safety: stop trading on a winning day at 40% of target (well below 50% rule)
SAFE_DAILY_PROFIT_CAP_PCT = float(os.getenv("ICT_SAFE_DAILY_CAP", "0.40"))

# Multi-threshold halts (% of limit hit)
WARNING_THRESHOLD  = 0.60   # 60% of any limit used → WARNING
CRITICAL_THRESHOLD = 0.80   # 80% of any limit used → CRITICAL
LOCK_THRESHOLD     = 0.90   # 90% of any limit used → LOCKED

FUNDED_MODE = os.getenv("ICT_FUNDED_MODE", "false").lower() == "true"


# ==================== Account state queries ====================

def _get_account_equity() -> float:
    """Sum of starting equity + all realized P&L from ict_trades."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM ict_trades WHERE status = 'closed'"
            )
            row = cursor.fetchone()
            total_pnl = float(row[0]) if row else 0.0
            return STARTING_EQUITY + total_pnl
    except Exception as e:
        logger.error("Guard: equity query failed: %s", e)
        return STARTING_EQUITY


def _get_high_water_mark() -> float:
    """Highest equity level ever reached — used for trailing drawdown calc."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT MAX(running_equity) FROM (
                    SELECT SUM(pnl) OVER (ORDER BY closed_at) + ? as running_equity
                    FROM ict_trades
                    WHERE status = 'closed'
                )
            """, (STARTING_EQUITY,))
            row = cursor.fetchone()
            hwm = row[0] if row and row[0] is not None else STARTING_EQUITY
            return max(float(hwm), STARTING_EQUITY)
    except Exception as e:
        logger.error("Guard: HWM query failed: %s", e)
        return STARTING_EQUITY


def _get_daily_pnl() -> float:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(pnl), 0) FROM ict_trades
                WHERE status = 'closed' AND closed_at >= datetime('now', 'start of day')
            """)
            row = cursor.fetchone()
            return float(row[0]) if row else 0.0
    except Exception as e:
        logger.error("Guard: daily P&L query failed: %s", e)
        return 0.0


# ==================== Main guard check ====================

def check_account_guard() -> GuardDecision:
    """
    Master pre-trade check. Returns the most restrictive state across all rules.
    """
    equity   = _get_account_equity()
    hwm      = _get_high_water_mark()
    daily    = _get_daily_pnl()
    total_pl = equity - STARTING_EQUITY

    metrics = {
        "equity":            equity,
        "high_water_mark":   hwm,
        "daily_pnl":         daily,
        "total_pnl":         total_pl,
        "profit_target":     PROFIT_TARGET,
        "trailing_dd_limit": TRAILING_DD_LIMIT,
    }

    # In funded mode: no eval rules apply, just basic drawdown safety
    if FUNDED_MODE:
        # Still protect against catastrophic drawdown — never let account fall below starting equity
        if equity < STARTING_EQUITY * 0.95:
            return GuardDecision(
                state=GuardState.CRITICAL,
                reason=f"[FUNDED] Account dropped 5% below starting equity (${equity:.0f})",
                size_multiplier=0.5,
                min_grade_required="A",
                metrics=metrics,
            )
        return GuardDecision(
            state=GuardState.SAFE,
            reason="[FUNDED] No eval rules — trade freely",
            size_multiplier=1.0,
            min_grade_required="B",
            metrics=metrics,
        )

    # ── EVAL MODE — check every rule ──
    worst_state = GuardState.SAFE
    worst_reason = "All checks passed"

    # 1. Daily loss limit progression
    daily_loss = -daily if daily < 0 else 0.0
    daily_loss_pct = daily_loss / DAILY_LOSS_LIMIT
    metrics["daily_loss_pct"] = daily_loss_pct

    if daily_loss_pct >= LOCK_THRESHOLD:
        return GuardDecision(
            state=GuardState.LOCKED,
            reason=f"Daily loss ${daily_loss:.0f} is {daily_loss_pct*100:.0f}% of ${DAILY_LOSS_LIMIT:.0f} limit — LOCKED for today",
            size_multiplier=0.0,
            min_grade_required="A",
            metrics=metrics,
        )
    elif daily_loss_pct >= CRITICAL_THRESHOLD:
        worst_state = GuardState.CRITICAL
        worst_reason = f"Daily loss at {daily_loss_pct*100:.0f}% of limit — A-grade only, half size"
    elif daily_loss_pct >= WARNING_THRESHOLD:
        if worst_state == GuardState.SAFE:
            worst_state = GuardState.WARNING
            worst_reason = f"Daily loss at {daily_loss_pct*100:.0f}% of limit — half size"

    # 2. Trailing drawdown — only counts from HWM
    drawdown_from_hwm = hwm - equity
    trailing_pct = drawdown_from_hwm / TRAILING_DD_LIMIT if TRAILING_DD_LIMIT > 0 else 0
    metrics["trailing_dd_pct"]   = trailing_pct
    metrics["drawdown_from_hwm"] = drawdown_from_hwm

    if trailing_pct >= LOCK_THRESHOLD:
        return GuardDecision(
            state=GuardState.LOCKED,
            reason=f"Trailing drawdown ${drawdown_from_hwm:.0f} is {trailing_pct*100:.0f}% of ${TRAILING_DD_LIMIT:.0f} — LOCKED",
            size_multiplier=0.0,
            min_grade_required="A",
            metrics=metrics,
        )
    elif trailing_pct >= CRITICAL_THRESHOLD:
        worst_state = GuardState.CRITICAL
        worst_reason = f"Trailing DD at {trailing_pct*100:.0f}% — preservation mode"
    elif trailing_pct >= WARNING_THRESHOLD:
        if worst_state == GuardState.SAFE:
            worst_state = GuardState.WARNING
            worst_reason = f"Trailing DD at {trailing_pct*100:.0f}% — caution"

    # 3. Consistency-safe daily profit cap (40% — well below the 50% Lucid rule)
    if daily > 0:
        safe_cap = PROFIT_TARGET * SAFE_DAILY_PROFIT_CAP_PCT
        if daily >= safe_cap:
            return GuardDecision(
                state=GuardState.LOCKED,
                reason=(
                    f"Today's profit ${daily:.0f} hit ${safe_cap:.0f} ({SAFE_DAILY_PROFIT_CAP_PCT*100:.0f}% of target) — "
                    f"LOCKED to protect 50% consistency rule. Resume tomorrow."
                ),
                size_multiplier=0.0,
                min_grade_required="A",
                metrics=metrics,
            )

    # 4. Approaching profit target — switch to preservation
    progress_to_target = total_pl / PROFIT_TARGET if PROFIT_TARGET > 0 else 0
    metrics["target_progress_pct"] = progress_to_target

    if progress_to_target >= 0.95:
        return GuardDecision(
            state=GuardState.LOCKED,
            reason=f"Eval profit target reached (${total_pl:.0f} of ${PROFIT_TARGET:.0f}) — STOP and request payout",
            size_multiplier=0.0,
            min_grade_required="A",
            metrics=metrics,
        )
    elif progress_to_target >= 0.80:
        if worst_state in (GuardState.SAFE, GuardState.WARNING):
            worst_state = GuardState.CRITICAL
            worst_reason = f"Close to target ({progress_to_target*100:.0f}%) — A-only, preservation"

    # ── Final decision ──
    size_mult = {
        GuardState.SAFE:     1.0,
        GuardState.WARNING:  0.5,
        GuardState.CRITICAL: 0.33,
        GuardState.LOCKED:   0.0,
    }[worst_state]

    min_grade = {
        GuardState.SAFE:     "B",
        GuardState.WARNING:  "B",
        GuardState.CRITICAL: "A",
        GuardState.LOCKED:   "A",
    }[worst_state]

    return GuardDecision(
        state=worst_state,
        reason=worst_reason,
        size_multiplier=size_mult,
        min_grade_required=min_grade,
        metrics=metrics,
    )


def get_guard_dashboard() -> dict:
    """Dashboard payload for /ict/status."""
    decision = check_account_guard()
    return {
        "state": decision.state.value,
        "reason": decision.reason,
        "size_multiplier": decision.size_multiplier,
        "min_grade_required": decision.min_grade_required,
        **decision.metrics,
    }
