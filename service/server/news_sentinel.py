"""
News Sentinel — monitors Trump/Truth Social, financial news, and macro calendar.

Returns CLEAR | CAUTION | HALT before any trade executes.
Uses RSS feeds and free API tiers only — no paid subscriptions needed.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 120  # Re-check every 2 minutes

# Keywords that escalate to HALT
_HALT_KEYWORDS = {
    "tariff", "rate hike", "emergency", "circuit breaker", "trading halt",
    "market crash", "black swan", "systemic", "federal reserve", "fomc",
    "nuclear", "war declaration", "sanctions", "bank failure", "margin call",
    "flash crash", "exchange halt", "market closure",
}

# Keywords that trigger CAUTION (trade with extra care)
_CAUTION_KEYWORDS = {
    "tariffs", "inflation", "recession", "unemployment", "rate cut",
    "cpi", "pce", "nfp", "jobs report", "fed chair", "powell",
    "earnings miss", "guidance", "downgrade", "upgrade", "acquisition",
    "merger", "investigation", "probe", "lawsuit",
}

# Macro events that trigger pre/post blackouts
_MACRO_PATTERNS = {
    "fomc": 60,       # 60 min after release
    "cpi": 30,
    "pce": 30,
    "nfp": 30,        # Non-farm payroll
    "ppi": 20,
    "gdp": 20,
    "unemployment": 20,
}


class SentinelStatus(str, Enum):
    CLEAR = "CLEAR"
    CAUTION = "CAUTION"
    HALT = "HALT"


@dataclass
class SentinelResult:
    status: SentinelStatus
    reason: str
    headlines: list[str] = field(default_factory=list)
    checked_at: str = ""

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = datetime.now(timezone.utc).isoformat()


# ==================== Cache ====================
_cache: Optional[SentinelResult] = None
_cache_time: float = 0.0


def _is_cache_valid() -> bool:
    return _cache is not None and (time.time() - _cache_time) < CACHE_TTL_SECONDS


# ==================== RSS Fetchers ====================

def _fetch_rss_headlines(url: str, timeout: int = 5) -> list[str]:
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "ICT-Sentinel/1.0"})
        if resp.status_code != 200:
            return []
        # Simple RSS title extraction without feedparser dependency
        titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", resp.text, re.DOTALL)
        # Skip the channel title (first one)
        return [t.strip() for t in titles[1:21] if t.strip()]
    except Exception as e:
        logger.warning("RSS fetch failed for %s: %s", url, e)
        return []


def _fetch_trump_headlines() -> list[str]:
    """Fetch Trump's Truth Social posts via RSS."""
    feeds = [
        "https://truthsocial.com/@realDonaldTrump.rss",
        "https://rss.app/feeds/trump.xml",  # Fallback aggregator
    ]
    for feed in feeds:
        headlines = _fetch_rss_headlines(feed)
        if headlines:
            return headlines
    return []


def _fetch_financial_news() -> list[str]:
    """Fetch financial news from free RSS sources."""
    feeds = [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=ES=F,NQ=F&region=US&lang=en-US",
        "https://www.investing.com/rss/news.rss",
        "https://feeds.marketwatch.com/marketwatch/topstories",
    ]
    all_headlines = []
    for feed in feeds:
        all_headlines.extend(_fetch_rss_headlines(feed))
        if len(all_headlines) > 20:
            break
    return all_headlines[:20]


def _fetch_alpha_vantage_news(api_key: str) -> list[str]:
    """Fetch news sentiment from Alpha Vantage (free tier: 25 calls/day)."""
    if not api_key or api_key == "demo":
        return []
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=NEWS_SENTIMENT&tickers=SPY,QQQ&apikey={api_key}&limit=10"
        )
        resp = requests.get(url, timeout=8)
        data = resp.json()
        feed = data.get("feed", [])
        return [item.get("title", "") for item in feed[:10] if item.get("title")]
    except Exception as e:
        logger.warning("Alpha Vantage news fetch failed: %s", e)
        return []


# ==================== Analysis ====================

def _classify_headlines(headlines: list[str]) -> tuple[SentinelStatus, str]:
    combined = " ".join(headlines).lower()
    found_halt = [kw for kw in _HALT_KEYWORDS if kw in combined]
    found_caution = [kw for kw in _CAUTION_KEYWORDS if kw in combined]

    if found_halt:
        return SentinelStatus.HALT, f"Market-halting keywords detected: {', '.join(found_halt[:3])}"
    if found_caution:
        return SentinelStatus.CAUTION, f"Caution keywords in news: {', '.join(found_caution[:3])}"
    return SentinelStatus.CLEAR, "No significant news risk detected"


def _check_macro_timing() -> Optional[str]:
    """
    Check if we're near a scheduled macro event using Finnhub free API.
    Returns block reason string or None.
    """
    try:
        now = datetime.now(timezone.utc)
        tomorrow = now + timedelta(days=1)
        url = (
            f"https://finnhub.io/api/v1/calendar/economic"
            f"?from={now.strftime('%Y-%m-%d')}&to={tomorrow.strftime('%Y-%m-%d')}"
            f"&token=d0eo1q9r01qt5sif4a5gd0eo1q9r01qt5sif4a60"  # Free public token
        )
        resp = requests.get(url, timeout=5)
        events = resp.json().get("economicCalendar", [])

        for event in events:
            event_name = (event.get("event") or "").lower()
            event_time_str = event.get("time") or ""
            if not event_time_str:
                continue

            for macro_key, blackout_minutes in _MACRO_PATTERNS.items():
                if macro_key in event_name:
                    try:
                        event_dt = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
                        delta = (event_dt - now).total_seconds() / 60
                        after_delta = (now - event_dt).total_seconds() / 60

                        if -30 <= delta <= 0:
                            return f"{event.get('event')} in {int(-delta)} min — pre-event blackout"
                        if 0 <= after_delta <= blackout_minutes:
                            return f"{event.get('event')} released {int(after_delta)} min ago — post-event blackout"
                    except ValueError:
                        continue
    except Exception as e:
        logger.debug("Macro calendar check failed (non-critical): %s", e)
    return None


# ==================== Main Entry ====================

def check_news(alpha_vantage_key: str = "demo") -> SentinelResult:
    """
    Main entry — call before every signal evaluation.
    Results are cached for CACHE_TTL_SECONDS to avoid hammering feeds.
    """
    global _cache, _cache_time

    if _is_cache_valid():
        return _cache

    # Macro timing check first (fast, deterministic)
    macro_block = _check_macro_timing()
    if macro_block:
        result = SentinelResult(
            status=SentinelStatus.HALT,
            reason=macro_block,
        )
        _cache = result
        _cache_time = time.time()
        return result

    # Gather all headlines
    trump_headlines = _fetch_trump_headlines()
    financial_headlines = _fetch_financial_news()
    av_headlines = _fetch_alpha_vantage_news(alpha_vantage_key)

    all_headlines = trump_headlines + financial_headlines + av_headlines
    status, reason = _classify_headlines(all_headlines)

    result = SentinelResult(
        status=status,
        reason=reason,
        headlines=all_headlines[:10],
    )
    _cache = result
    _cache_time = time.time()

    if status != SentinelStatus.CLEAR:
        logger.warning("News Sentinel: %s — %s", status.value, reason)

    return result


def get_sentinel_cache() -> Optional[SentinelResult]:
    return _cache
