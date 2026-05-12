"""
Eval Optimizer — 2-Day Pass Mode for Lucid LucidFlex.

Lucid requires a 2-day MINIMUM trading window to pass.
Target: exactly $3,000 profit, split 50/50.

  Day 1: $1,500 (stop here)
  Day 2: $1,500 (stop here → eval passes at $3,000)
  Best day: 50.0% of total

CONSISTENCY MATH:
  Lucid's 50% rule means "no single day's profit can exceed 50% of total profit".
  The 50/50 split is the ONLY mathematically clean 2-day pass at $3K:
    - $1,500 + $1,500 = $3,000, each day = 50.0% exactly
    - ANY uneven split makes one day > 50% and fails
    - To stay strictly UNDER 50% would need a 3rd small day

  Interpretation matters:
    - "≤ 50%" (inclusive): 50/50 passes
    - "<  50%" (strict):   50/50 fails by 1 cent → need ICT_STRICT_CONSISTENCY=true

STRICT MODE (ICT_STRICT_CONSISTENCY=true):
  Adds a Day 3 trim trade to push best-day ratio below 50%.
    Day 1: $1,500
    Day 2: $1,500
    Day 3: $200 (any small profit)
    Total: $3,200, best day = 1500/3200 = 46.9% ✓ strictly under 50%

Behaviors:
  - Active when ICT_EVAL_2DAY_MODE=true
  - Tracks day counter (Day 1, Day 2, Day 3-trim if strict, PASSED)
  - Halts each day's trading at its target
  - On eval pass: Telegram alert + suggests setting FUNDED_MODE=true
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

EVAL_2DAY_MODE     = os.getenv("ICT_EVAL_2DAY_MODE", "true").lower() == "true"
EVAL_TARGET        = float(os.getenv("ICT_PROFIT_TARGET", "3000"))
DAY1_TARGET        = float(os.getenv("ICT_DAY1_TARGET", "1500"))
DAY2_TARGET        = float(os.getenv("ICT_DAY2_TARGET", "1500"))
CONSISTENCY_CAP    = float(os.getenv("ICT_CONSISTENCY_PCT", "0.50"))
STRICT_CONSISTENCY = os.getenv("ICT_STRICT_CONSISTENCY", "false").lower() == "true"
# Day 3 trim trade for strict-consistency interpretation (brings ratio < 50%)
DAY3_TRIM_TARGET   = float(os.getenv("ICT_DAY3_TRIM", "200"))

STATE_FILE = Path(__file__).parent / "eval_state.json"


class EvalDay(str, Enum):
    DAY_1     = "DAY_1"
    DAY_2     = "DAY_2"
    DAY_3_TRIM = "DAY_3_TRIM"  # only used in strict-consistency mode
    PASSED    = "PASSED"
    INACTIVE  = "INACTIVE"


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
        # Day 2 (or Day 3 trim in strict mode)
        day_1_pnl = state.get("day_1_pnl", 0.0)
        day_2_pnl = state.get("day_2_pnl", 0.0)
        day_2_date = state.get("day_2_date")

        is_day_two = (day_2_date is None or today == day_2_date)

        if is_day_two:
            # Day 2 target: bring total to exactly $3,000 (so 50/50 split)
            # Hard cap: Day 2 cannot EXCEED Day 1 (would break ≤50% consistency)
            day_2_target = min(DAY2_TARGET, EVAL_TARGET - day_1_pnl, day_1_pnl)
            remaining = day_2_target - today_pnl
            can_trade = today_pnl < day_2_target

            # On Day 2 target hit
            if not can_trade:
                state["day_2_pnl"] = today_pnl
                state["day_2_date"] = today
                _save_state(state)

                if STRICT_CONSISTENCY and total_pnl <= EVAL_TARGET:
                    # In strict mode, the 50/50 split sits exactly at 50% — need Day 3 trim
                    reason = (
                        f"DAY 2 done (${today_pnl:.0f}). Total ${total_pnl:.0f}. "
                        f"Strict consistency mode — take 1 small Day 3 trim trade tomorrow (~${DAY3_TRIM_TARGET:.0f}) "
                        f"to push best-day ratio below 50%."
                    )
                else:
                    state["passed"] = True
                    _save_state(state)
                    reason = f"DAY 2 TARGET HIT — EVAL PASSED (total ${total_pnl:.0f}). Stop and request payout."

                return EvalDecision(
                    day=EvalDay.DAY_2,
                    today_pnl=today_pnl,
                    today_target=day_2_target,
                    remaining_today=0.0,
                    total_pnl=total_pnl,
                    can_trade=False,
                    reason=reason,
                )

            return EvalDecision(
                day=EvalDay.DAY_2,
                today_pnl=today_pnl,
                today_target=day_2_target,
                remaining_today=remaining,
                total_pnl=total_pnl,
                can_trade=True,
                reason=f"DAY 2: ${today_pnl:.0f} / ${day_2_target:.0f} — ${remaining:.0f} to complete eval",
            )

        # Day 3 trim (only in strict mode after Day 2 hit $3K exactly)
        trim_target = DAY3_TRIM_TARGET
        remaining = trim_target - today_pnl
        can_trade = today_pnl < trim_target

        if not can_trade:
            state["passed"] = True
            _save_state(state)
            reason = (
                f"DAY 3 TRIM HIT (${today_pnl:.0f}) — EVAL PASSED with best-day ratio "
                f"{(day_1_pnl / total_pnl * 100):.1f}% (strict <50% satisfied)."
            )
        else:
            reason = f"DAY 3 trim: ${today_pnl:.0f} / ${trim_target:.0f} — push ratio under 50%"

        return EvalDecision(
            day=EvalDay.DAY_3_TRIM,
            today_pnl=today_pnl,
            today_target=trim_target,
            remaining_today=max(0, remaining),
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
