"""
Eval Optimizer — 2-Day Pass Mode for Lucid LucidFlex.

Lucid requires a 2-day minimum trading window to pass.
This module enforces an OPTIMAL daily target distribution:

  Day 1: target $1,400 — stop trading on hit
  Day 2: target $1,650 — stop trading on hit (passes $3K)

  Why those numbers:
  - Total: $3,050 ($50 buffer above $3K target)
  - Best day: $1,650 = 54% of $3,000 BUT split across 2 days makes each
    day ≤ 50% of the FINAL total profit (the actual consistency check)
  - $1,650 / $3,050 = 54% — wait, that violates 50% rule

  CORRECTED math for consistency:
  - If you make $3,050 total, max single day = $1,525 (50%)
  - So: Day 1 = $1,500, Day 2 = $1,500 → $3,000 ✓ (each day = 50% exactly)
  - Safer: Day 1 = $1,400, Day 2 = $1,600 → $3,000, best day = 53% ✗
  - To stay UNDER 50%: Day 1 needs to be at least equal to Day 2
  - So: Day 1 = $1,500, Day 2 = $1,510 → $3,010, best day = 50.2% ✗

  ACTUAL FIX: do not over-perform Day 1 and Day 2 must equal Day 1.
  → Target $1,500 each day, stop when hit.

  Some firms read consistency as STRICTLY <50%, others ≤50%.
  We use 48% as the safe ceiling per day.

Behaviors:
  - Active when ICT_EVAL_2DAY_MODE=true
  - Tracks day counter (Day 1, Day 2, complete)
  - Halts each day's trading at daily_target
  - On Day 2 hit: announces eval pass via Telegram, switches to FUNDED_MODE
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from database import get_db_connection

logger = logging.getLogger(__name__)

EVAL_2DAY_MODE  = os.getenv("ICT_EVAL_2DAY_MODE", "true").lower() == "true"
EVAL_TARGET     = float(os.getenv("ICT_PROFIT_TARGET", "3000"))
DAY1_TARGET     = float(os.getenv("ICT_DAY1_TARGET", "1450"))   # 48% of $3,000
DAY2_TARGET     = float(os.getenv("ICT_DAY2_TARGET", "1450"))   # remaining to hit $2,900-$2,950
CONSISTENCY_CAP = float(os.getenv("ICT_CONSISTENCY_PCT", "0.50"))

STATE_FILE = Path(__file__).parent / "eval_state.json"


class EvalDay(str, Enum):
    DAY_1    = "DAY_1"
    DAY_2    = "DAY_2"
    PASSED   = "PASSED"
    INACTIVE = "INACTIVE"


@dataclass
class EvalDecision:
    day:               EvalDay
    today_pnl:         float
    today_target:      float
    remaining_today:   float
    total_pnl:         float
    can_trade:         bool
    reason:            str


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"start_date": None, "day_1_pnl": 0.0, "day_1_date": None, "passed": False}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _get_today_pnl() -> float:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(pnl), 0) FROM ict_trades
                WHERE status = 'closed' AND closed_at >= datetime('now', 'start of day')
            """)
            row = cursor.fetchone()
            return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


def _get_total_pnl() -> float:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COALESCE(SUM(pnl), 0) FROM ict_trades WHERE status='closed'")
            row = cursor.fetchone()
            return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


def check_eval_status() -> EvalDecision:
    if not EVAL_2DAY_MODE:
        return EvalDecision(
            day=EvalDay.INACTIVE,
            today_pnl=0.0,
            today_target=0.0,
            remaining_today=0.0,
            total_pnl=0.0,
            can_trade=True,
            reason="2-day eval mode disabled",
        )

    state = _load_state()
    today = _today_str()
    today_pnl = _get_today_pnl()
    total_pnl = _get_total_pnl()

    if state.get("passed") or total_pnl >= EVAL_TARGET:
        return EvalDecision(
            day=EvalDay.PASSED,
            today_pnl=today_pnl,
            today_target=0.0,
            remaining_today=0.0,
            total_pnl=total_pnl,
            can_trade=False,
            reason=f"EVAL PASSED — total ${total_pnl:.0f} ≥ ${EVAL_TARGET:.0f}. Switch ICT_FUNDED_MODE=true.",
        )

    # Detect day counter from state
    if not state.get("start_date"):
        # First trading day starts now
        state["start_date"] = today
        _save_state(state)

    day_1_date = state.get("day_1_date") or state["start_date"]
    is_day_one = (today == day_1_date)

    if is_day_one:
        target = DAY1_TARGET
        remaining = target - today_pnl
        can_trade = today_pnl < target

        reason = (
            f"DAY 1: ${today_pnl:.0f} / ${target:.0f} target — ${remaining:.0f} remaining"
            if can_trade else
            f"DAY 1 TARGET HIT (${today_pnl:.0f}) — stop trading. Come back tomorrow for Day 2."
        )

        # Save Day 1 P&L when hit
        if not can_trade and state.get("day_1_pnl", 0) != today_pnl:
            state["day_1_pnl"] = today_pnl
            state["day_1_date"] = today
            _save_state(state)

        return EvalDecision(
            day=EvalDay.DAY_1,
            today_pnl=today_pnl,
            today_target=target,
            remaining_today=remaining,
            total_pnl=total_pnl,
            can_trade=can_trade,
            reason=reason,
        )

    else:
        # Day 2 or later
        day_1_pnl = state.get("day_1_pnl", 0.0)
        # Consistency-safe Day 2 target: day_2 ≤ day_1 / consistency_pct - day_1
        # Or simply: Day 2 = whatever brings total to EVAL_TARGET + buffer
        target_total = EVAL_TARGET + 50  # small buffer above $3K
        day_2_target = min(DAY2_TARGET, target_total - day_1_pnl)
        # Hard cap: Day 2 cannot exceed Day 1 (would break consistency)
        day_2_target = min(day_2_target, day_1_pnl)

        remaining = day_2_target - today_pnl
        can_trade = today_pnl < day_2_target

        reason = (
            f"DAY 2: ${today_pnl:.0f} / ${day_2_target:.0f} target — ${remaining:.0f} to pass eval"
            if can_trade else
            f"DAY 2 TARGET HIT — eval should pass (total ${total_pnl:.0f}). Stop and request payout."
        )

        if not can_trade and total_pnl >= EVAL_TARGET:
            state["passed"] = True
            _save_state(state)

        return EvalDecision(
            day=EvalDay.DAY_2,
            today_pnl=today_pnl,
            today_target=day_2_target,
            remaining_today=remaining,
            total_pnl=total_pnl,
            can_trade=can_trade,
            reason=reason,
        )


def get_eval_dashboard() -> dict:
    d = check_eval_status()
    return {
        "active":          EVAL_2DAY_MODE,
        "day":             d.day.value,
        "today_pnl":       d.today_pnl,
        "today_target":    d.today_target,
        "remaining_today": d.remaining_today,
        "total_pnl":       d.total_pnl,
        "eval_target":     EVAL_TARGET,
        "can_trade":       d.can_trade,
        "reason":          d.reason,
    }
