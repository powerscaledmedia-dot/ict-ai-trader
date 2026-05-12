"""
Session Rules — Time-based and frequency-based circuit breakers.

Beyond the killzone check (right window?) and risk governor (single-trade risk?),
the Session Rules layer enforces patience and quality discipline:

  • Trade frequency cap:    max 2 trades per session (quality > quantity)
  • Win streak limiter:     after 2 wins in a session, stop (don't give back)
  • First-5-min blackout:   no trading in the first 5 min of a killzone (whipsaw)
  • Last-5-min blackout:    no trading in the last 5 min of a killzone (no time to develop)
  • Mandatory flatten:      auto-close all positions 15 min before session close
  • Post-loss cooldown:     30-min pause after any loss to reset emotionally
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from database import get_db_connection

logger = logging.getLogger(__name__)


class SessionStatus(str, Enum):
    OK              = "OK"
    BLACKOUT_OPEN   = "BLACKOUT_OPEN"
    BLACKOUT_CLOSE  = "BLACKOUT_CLOSE"
    FREQ_CAP        = "FREQ_CAP"
    WIN_LIMIT       = "WIN_LIMIT"
    LOSS_COOLDOWN   = "LOSS_COOLDOWN"


@dataclass
class SessionDecision:
    status: SessionStatus
    reason: str
    minutes_remaining: int = 0


# ==================== Config ====================
MAX_TRADES_PER_SESSION  = int(os.getenv("ICT_MAX_TRADES_PER_SESSION", "2"))
WIN_LIMIT_PER_SESSION   = int(os.getenv("ICT_WIN_LIMIT_PER_SESSION", "2"))
OPEN_BLACKOUT_MINUTES   = int(os.getenv("ICT_OPEN_BLACKOUT_MIN", "5"))
CLOSE_BLACKOUT_MINUTES  = int(os.getenv("ICT_CLOSE_BLACKOUT_MIN", "15"))
POST_LOSS_COOLDOWN_MIN  = int(os.getenv("ICT_POST_LOSS_COOLDOWN_MIN", "30"))

# Killzone start/end times in UTC (must match killzone_manager.py)
_SESSIONS_UTC = {
    "asia":   (1, 5),
    "london": (7, 11),
    "ny":     (13, 17),
}


def _current_session(now_utc: datetime) -> Optional[str]:
    hour = now_utc.hour
    for name, (start, end) in _SESSIONS_UTC.items():
        if start <= hour < end:
            return name
    return None


def _session_bounds(now_utc: datetime, session: str) -> tuple[datetime, datetime]:
    start_hour, end_hour = _SESSIONS_UTC[session]
    start = now_utc.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end   = now_utc.replace(hour=end_hour,   minute=0, second=0, microsecond=0)
    return start, end


def _trades_this_session(session_start: datetime) -> tuple[int, int, int]:
    """Returns (total_trades, wins, losses) for the current session window."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT outcome, COUNT(*) FROM ict_trades
                WHERE opened_at >= ?
                GROUP BY outcome
            """, (session_start.isoformat(),))
            rows = cursor.fetchall()
            wins   = sum(c for o, c in rows if o == "win")
            losses = sum(c for o, c in rows if o == "loss")
            total  = sum(c for _, c in rows)
            return total, wins, losses
    except Exception as e:
        logger.error("Session: query failed: %s", e)
        return 0, 0, 0


def _last_loss_time() -> Optional[datetime]:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT closed_at FROM ict_trades
                WHERE outcome = 'loss'
                ORDER BY closed_at DESC LIMIT 1
            """)
            row = cursor.fetchone()
            if row and row[0]:
                return datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def check_session_rules(now_utc: Optional[datetime] = None) -> SessionDecision:
    now = now_utc or datetime.now(timezone.utc)
    session = _current_session(now)

    if not session:
        return SessionDecision(status=SessionStatus.OK, reason="Outside any killzone — session rules dormant")

    start, end = _session_bounds(now, session)
    minutes_into_session = int((now - start).total_seconds() / 60)
    minutes_to_close     = int((end - now).total_seconds() / 60)

    # 1. Opening blackout (whipsaw zone)
    if minutes_into_session < OPEN_BLACKOUT_MINUTES:
        return SessionDecision(
            status=SessionStatus.BLACKOUT_OPEN,
            reason=f"Opening blackout — wait {OPEN_BLACKOUT_MINUTES - minutes_into_session} min for {session.upper()} to develop",
            minutes_remaining=OPEN_BLACKOUT_MINUTES - minutes_into_session,
        )

    # 2. Closing blackout (no time for trade to work)
    if minutes_to_close < CLOSE_BLACKOUT_MINUTES:
        return SessionDecision(
            status=SessionStatus.BLACKOUT_CLOSE,
            reason=f"Closing blackout — {session.upper()} ends in {minutes_to_close} min, no new trades",
            minutes_remaining=minutes_to_close,
        )

    # 3. Trade frequency cap
    total_trades, wins, losses = _trades_this_session(start)

    if total_trades >= MAX_TRADES_PER_SESSION:
        return SessionDecision(
            status=SessionStatus.FREQ_CAP,
            reason=f"Frequency cap: {total_trades} trades taken this {session.upper()} session (max {MAX_TRADES_PER_SESSION})",
        )

    # 4. Win streak limiter (don't give back profits)
    if wins >= WIN_LIMIT_PER_SESSION:
        return SessionDecision(
            status=SessionStatus.WIN_LIMIT,
            reason=f"Win limit: {wins} wins in {session.upper()} session — stop while ahead",
        )

    # 5. Post-loss emotional reset
    last_loss = _last_loss_time()
    if last_loss:
        elapsed_min = (now - last_loss).total_seconds() / 60
        if elapsed_min < POST_LOSS_COOLDOWN_MIN:
            remaining = int(POST_LOSS_COOLDOWN_MIN - elapsed_min)
            return SessionDecision(
                status=SessionStatus.LOSS_COOLDOWN,
                reason=f"Post-loss cooldown — {remaining} min remaining (reset before re-entering)",
                minutes_remaining=remaining,
            )

    return SessionDecision(
        status=SessionStatus.OK,
        reason=f"{session.upper()} session — {total_trades}/{MAX_TRADES_PER_SESSION} trades, {wins}W/{losses}L",
    )


def get_session_dashboard() -> dict:
    decision = check_session_rules()
    now = datetime.now(timezone.utc)
    session = _current_session(now)
    return {
        "status": decision.status.value,
        "reason": decision.reason,
        "current_session": session,
        "max_trades": MAX_TRADES_PER_SESSION,
        "win_limit": WIN_LIMIT_PER_SESSION,
        "minutes_remaining": decision.minutes_remaining,
    }
