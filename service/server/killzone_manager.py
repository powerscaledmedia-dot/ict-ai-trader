"""
Killzone Manager — ICT session window enforcement.

Asia:   19:00–23:00 CST  (01:00–05:00 UTC)
London: 01:00–05:00 CST  (07:00–11:00 UTC)
NY:     07:00–11:00 CST  (13:00–17:00 UTC)

CST = UTC-6 (standard) / CDT = UTC-5 (daylight saving).
This module always works in UTC internally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class KillzoneStatus(str, Enum):
    TRADE = "TRADE"      # Inside an active killzone — execute signals
    WATCH = "WATCH"      # Outside killzone — log but don't execute
    BLOCKED = "BLOCKED"  # Macro blackout active — hard stop


@dataclass
class KillzoneResult:
    status: KillzoneStatus
    session: Optional[str]
    reason: str
    minutes_to_next: Optional[int] = None


# UTC hour ranges for each session (inclusive start, exclusive end)
_SESSIONS = {
    "asia":   (1, 5),    # 01:00–05:00 UTC = 19:00–23:00 CST
    "london": (7, 11),   # 07:00–11:00 UTC = 01:00–05:00 CST
    "ny":     (13, 17),  # 13:00–17:00 UTC = 07:00–11:00 CST
}

# Macro events that trigger a blackout window (minutes before/after)
_MACRO_EVENTS: list[dict] = []  # Populated by news_sentinel at runtime
_BLACKOUT_BEFORE_MINUTES = 30
_BLACKOUT_AFTER_MINUTES = 60  # Longer after FOMC/CPI — volatility lingers


def set_macro_events(events: list[dict]) -> None:
    """Called by news_sentinel to register upcoming macro events."""
    global _MACRO_EVENTS
    _MACRO_EVENTS = events
    logger.info("Macro events updated: %d upcoming", len(events))


def _active_session(now_utc: datetime) -> Optional[str]:
    hour = now_utc.hour
    minute = now_utc.minute
    fractional = hour + minute / 60.0
    for name, (start, end) in _SESSIONS.items():
        if start <= fractional < end:
            return name
    return None


def _minutes_to_next_session(now_utc: datetime) -> int:
    hour = now_utc.hour
    minute = now_utc.minute
    current_minutes = hour * 60 + minute

    starts_utc = [s * 60 for s, _ in _SESSIONS.values()]
    # Find next start after current time (wrapping over midnight)
    future = [s for s in starts_utc if s > current_minutes]
    if not future:
        future = starts_utc  # wrap to next day
    next_start = min(future)
    delta = next_start - current_minutes
    if delta < 0:
        delta += 24 * 60
    return delta


def _check_macro_blackout(now_utc: datetime) -> Optional[str]:
    for event in _MACRO_EVENTS:
        event_time_str = event.get("time_utc")
        if not event_time_str:
            continue
        try:
            event_time = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        delta_minutes = (event_time - now_utc).total_seconds() / 60
        after_minutes = (now_utc - event_time).total_seconds() / 60

        if -_BLACKOUT_BEFORE_MINUTES <= delta_minutes <= 0:
            return f"{event.get('name', 'Macro event')} in {int(-delta_minutes)} min"
        if 0 <= after_minutes <= _BLACKOUT_AFTER_MINUTES:
            return f"{event.get('name', 'Macro event')} released {int(after_minutes)} min ago"
    return None


def check_killzone(now_utc: Optional[datetime] = None) -> KillzoneResult:
    """Main entry point — call before every signal evaluation."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # 1. Check macro blackout first (always blocks regardless of session)
    blackout_reason = _check_macro_blackout(now_utc)
    if blackout_reason:
        return KillzoneResult(
            status=KillzoneStatus.BLOCKED,
            session=None,
            reason=f"MACRO BLACKOUT: {blackout_reason}",
        )

    # 2. Check if we're inside an active killzone
    session = _active_session(now_utc)
    if session:
        return KillzoneResult(
            status=KillzoneStatus.TRADE,
            session=session,
            reason=f"Inside {session.upper()} killzone",
        )

    # 3. Outside all killzones
    mins = _minutes_to_next_session(now_utc)
    return KillzoneResult(
        status=KillzoneStatus.WATCH,
        session=None,
        reason="Outside killzone — signal logged, not executed",
        minutes_to_next=mins,
    )


def get_session_schedule() -> dict:
    """Return session windows in both UTC and CST for the dashboard."""
    return {
        "asia":   {"utc": "01:00–05:00", "cst": "19:00–23:00"},
        "london": {"utc": "07:00–11:00", "cst": "01:00–05:00"},
        "ny":     {"utc": "13:00–17:00", "cst": "07:00–11:00"},
    }
