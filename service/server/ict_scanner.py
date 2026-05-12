"""
ICT Scanner — Pure rule-based detection for ICT concepts.

Detects: FVG, IFVG, Order Block, Liquidity Grab, SMT Divergence.
No LLM calls at runtime — deterministic Python only.
Setup quality is scored 0.0–1.0 using weighted heuristics.

HIGH-CONVICTION MODE (ICT_HIGH_CONVICTION=true in .env):
  - Only A-grade setups proceed (score >= 0.85)
  - Minimum 3 confluence factors required
  - Minimum R:R of 2.0
  - Expect 1-3 trades per week (selectivity > frequency)
  - Recommended for first eval pass and 80%+ WR target
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

HIGH_CONVICTION_MODE = os.getenv("ICT_HIGH_CONVICTION", "true").lower() == "true"
MIN_CONFLUENCE_FACTORS = int(os.getenv("ICT_MIN_CONFLUENCE", "3"))
MIN_RR_FLOOR = float(os.getenv("ICT_MIN_RR", "2.0"))
HIGH_CONVICTION_SCORE_THRESHOLD = float(os.getenv("ICT_HIGH_CONVICTION_SCORE", "0.85"))


class SetupType(str, Enum):
    FVG = "FVG"
    IFVG = "IFVG"
    ORDER_BLOCK = "ORDER_BLOCK"
    LIQUIDITY_GRAB = "LIQUIDITY_GRAB"
    SMT_DIVERGENCE = "SMT_DIVERGENCE"


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class Grade(str, Enum):
    A = "A"
    B = "B"
    C = "C"  # C-grade setups are rejected


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        return self.high - self.low

    @property
    def body_ratio(self) -> float:
        if self.range_size == 0:
            return 0.0
        return self.body_size / self.range_size

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


@dataclass
class ICTSetup:
    setup_type: SetupType
    direction: Direction
    instrument: str
    timeframe: str
    entry: float
    stop: float
    target: float
    grade: Grade
    score: float                    # 0.0–1.0
    confluence_factors: list[str] = field(default_factory=list)
    raw_payload: dict = field(default_factory=dict)

    @property
    def risk_reward(self) -> float:
        risk = abs(self.entry - self.stop)
        reward = abs(self.target - self.entry)
        return reward / risk if risk > 0 else 0.0

    @property
    def is_tradeable(self) -> bool:
        # High-conviction: A-only, 3+ confluences, score >= 0.85
        if HIGH_CONVICTION_MODE:
            return (
                self.grade == Grade.A
                and self.score >= HIGH_CONVICTION_SCORE_THRESHOLD
                and len(self.confluence_factors) >= MIN_CONFLUENCE_FACTORS
                and self.risk_reward >= MIN_RR_FLOOR
            )
        # Standard: A or B, R:R >= 2.0
        return self.grade in (Grade.A, Grade.B) and self.risk_reward >= MIN_RR_FLOOR

    @property
    def rejection_reason(self) -> str:
        """Why this setup was rejected, if not tradeable."""
        if HIGH_CONVICTION_MODE:
            if self.grade != Grade.A:
                return f"High-conviction requires A-grade (this is {self.grade.value})"
            if self.score < HIGH_CONVICTION_SCORE_THRESHOLD:
                return f"Score {self.score:.2f} below high-conviction threshold {HIGH_CONVICTION_SCORE_THRESHOLD}"
            if len(self.confluence_factors) < MIN_CONFLUENCE_FACTORS:
                return f"Only {len(self.confluence_factors)} confluence factors (need {MIN_CONFLUENCE_FACTORS}+)"
            if self.risk_reward < MIN_RR_FLOOR:
                return f"R:R {self.risk_reward:.1f} below {MIN_RR_FLOOR} floor"
        else:
            if self.grade == Grade.C:
                return f"Grade C (score {self.score:.2f}) — below B threshold"
            if self.risk_reward < MIN_RR_FLOOR:
                return f"R:R {self.risk_reward:.1f} below {MIN_RR_FLOOR} floor"
        return "ok"


# Setup weights — updated nightly by strategy_analyst.py
_SETUP_WEIGHTS: dict[str, float] = {
    "FVG": 1.0,
    "IFVG": 0.9,
    "ORDER_BLOCK": 0.95,
    "LIQUIDITY_GRAB": 0.85,
    "SMT_DIVERGENCE": 0.8,
}


def load_setup_weights(weights: dict[str, float]) -> None:
    """Called by strategy_analyst after nightly review to update weights."""
    global _SETUP_WEIGHTS
    _SETUP_WEIGHTS.update(weights)
    logger.info("Setup weights updated: %s", _SETUP_WEIGHTS)


def get_setup_weights() -> dict[str, float]:
    return dict(_SETUP_WEIGHTS)


def detect_fvg(candles: list[Candle]) -> Optional[tuple[Direction, float, float]]:
    """
    Detect Fair Value Gap on last 3 candles.
    Bullish FVG: candle[0].high < candle[2].low (gap between c0 and c2).
    Bearish FVG: candle[0].low > candle[2].high.
    Returns (direction, gap_top, gap_bottom) or None.
    """
    if len(candles) < 3:
        return None
    c0, c1, c2 = candles[-3], candles[-2], candles[-1]

    # Bullish FVG
    if c0.high < c2.low:
        gap_size = c2.low - c0.high
        # Displacement candle (c1) should be bullish and large
        if c1.is_bullish and c1.body_ratio > 0.5:
            return Direction.BULLISH, c2.low, c0.high

    # Bearish FVG
    if c0.low > c2.high:
        gap_size = c0.low - c2.high  # noqa: F841
        if c1.is_bearish and c1.body_ratio > 0.5:
            return Direction.BEARISH, c0.low, c2.high

    return None


def detect_order_block(candles: list[Candle], lookback: int = 20) -> Optional[tuple[Direction, float, float]]:
    """
    Order Block: last opposing candle before a strong displacement move.
    Bullish OB: last bearish candle before a strong up-move.
    Bearish OB: last bullish candle before a strong down-move.
    Returns (direction, ob_high, ob_low) or None.
    """
    if len(candles) < lookback:
        return None

    recent = candles[-lookback:]
    last = recent[-1]

    # Look for strong displacement (displacement candle has large body)
    for i in range(len(recent) - 2, 0, -1):
        c = recent[i]
        # Bullish displacement — look for last bearish candle before it
        if c.is_bullish and c.body_ratio > 0.6:
            for j in range(i - 1, -1, -1):
                ob = recent[j]
                if ob.is_bearish:
                    # Price must currently be at or below the OB high
                    if last.low <= ob.high:
                        return Direction.BULLISH, ob.high, ob.low
                    break

        # Bearish displacement — look for last bullish candle before it
        if c.is_bearish and c.body_ratio > 0.6:
            for j in range(i - 1, -1, -1):
                ob = recent[j]
                if ob.is_bullish:
                    if last.high >= ob.low:
                        return Direction.BEARISH, ob.high, ob.low
                    break

    return None


def detect_liquidity_grab(candles: list[Candle], lookback: int = 20) -> Optional[tuple[Direction, float]]:
    """
    Liquidity Grab: price spikes above swing high / below swing low then reverses.
    Returns (direction, grabbed_level) or None.
    """
    if len(candles) < lookback + 2:
        return None

    history = candles[-(lookback + 2):-2]
    sweep = candles[-2]
    current = candles[-1]

    if not history:
        return None

    swing_high = max(c.high for c in history)
    swing_low = min(c.low for c in history)

    # Bullish grab: spike below swing low, then reversal up
    if sweep.low < swing_low and current.close > sweep.open:
        return Direction.BULLISH, swing_low

    # Bearish grab: spike above swing high, then reversal down
    if sweep.high > swing_high and current.close < sweep.open:
        return Direction.BEARISH, swing_high

    return None


def detect_smt_divergence(
    primary_candles: list[Candle],
    correlated_candles: list[Candle],
    lookback: int = 5,
) -> Optional[Direction]:
    """
    SMT Divergence: primary instrument makes new swing, correlated does NOT.
    e.g. MES makes new high but MNQ fails → bearish divergence.
    """
    if len(primary_candles) < lookback or len(correlated_candles) < lookback:
        return None

    p_recent = primary_candles[-lookback:]
    c_recent = correlated_candles[-lookback:]

    p_high = max(c.high for c in p_recent[:-1])
    c_high = max(c.high for c in c_recent[:-1])
    p_low = min(c.low for c in p_recent[:-1])
    c_low = min(c.low for c in c_recent[:-1])

    p_last = p_recent[-1]
    c_last = c_recent[-1]

    # Bearish SMT: primary makes new high, correlated fails
    if p_last.high > p_high and c_last.high <= c_high:
        return Direction.BEARISH

    # Bullish SMT: primary makes new low, correlated holds
    if p_last.low < p_low and c_last.low >= c_low:
        return Direction.BULLISH

    return None


def _score_from_payload(payload: dict) -> tuple[float, list[str]]:
    """
    Score an incoming TradingView webhook payload.
    Returns (score 0.0–1.0, confluence_factors list).
    """
    factors = []
    score = 0.0

    setup_type = payload.get("setup", "").upper()
    base_weight = _SETUP_WEIGHTS.get(setup_type, 0.5)
    score += base_weight * 0.4
    factors.append(f"Base weight {base_weight:.2f} for {setup_type}")

    # Risk-reward bonus
    try:
        entry = float(payload.get("entry", 0))
        stop = float(payload.get("stop", 0))
        target = float(payload.get("target", 0))
        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = reward / risk if risk > 0 else 0
        if rr >= 3:
            score += 0.3
            factors.append(f"Strong R:R {rr:.1f}")
        elif rr >= 2:
            score += 0.2
            factors.append(f"Good R:R {rr:.1f}")
        elif rr >= 1.5:
            score += 0.1
            factors.append(f"Acceptable R:R {rr:.1f}")
        else:
            factors.append(f"Weak R:R {rr:.1f} — penalized")
            score -= 0.1
    except (TypeError, ValueError):
        pass

    # Session alignment bonus
    killzone = payload.get("killzone", "").lower()
    if killzone in ("asia", "london", "ny"):
        score += 0.2
        factors.append(f"Killzone aligned: {killzone.upper()}")

    # Timeframe bonus (lower = higher precision for scalping)
    timeframe = str(payload.get("timeframe", "15"))
    if timeframe in ("1", "2", "3", "5"):
        score += 0.1
        factors.append(f"Scalp timeframe: {timeframe}m")
    elif timeframe in ("15", "30"):
        score += 0.05
        factors.append(f"Intraday timeframe: {timeframe}m")

    score = max(0.0, min(1.0, score))
    return score, factors


def _grade_from_score(score: float) -> Grade:
    if score >= 0.75:
        return Grade.A
    if score >= 0.55:
        return Grade.B
    return Grade.C


def evaluate_webhook_payload(payload: dict) -> ICTSetup:
    """
    Main entry — evaluate a TradingView webhook payload.
    Returns an ICTSetup with grade and score.
    """
    score, factors = _score_from_payload(payload)
    grade = _grade_from_score(score)

    setup_type_str = payload.get("setup", "FVG").upper()
    try:
        setup_type = SetupType(setup_type_str)
    except ValueError:
        setup_type = SetupType.FVG

    direction_str = payload.get("direction", "bullish").lower()
    direction = Direction.BULLISH if direction_str == "bullish" else Direction.BEARISH

    return ICTSetup(
        setup_type=setup_type,
        direction=direction,
        instrument=payload.get("instrument", "MES1!"),
        timeframe=str(payload.get("timeframe", "5")),
        entry=float(payload.get("entry", 0)),
        stop=float(payload.get("stop", 0)),
        target=float(payload.get("target", 0)),
        grade=grade,
        score=score,
        confluence_factors=factors,
        raw_payload=payload,
    )
