"""
Trader Profile — Encodes Brody's specific ICT trading style.

This is the override layer that turns the generic ICT scanner into
"trade like Brody". As you send screenshots and describe setups,
your specific rules get encoded here.

Each rule has a confidence multiplier applied on top of base scoring:
  +0.20 = MUST-HAVE confluence in your style
  +0.10 = STRONG signal you look for
  -0.20 = DEAL-BREAKER (immediate downgrade)

The scanner reads this file and applies your personal weights ON TOP of
the generic SMC/heuristic scoring.

Edit trader_profile.json — this module just loads & validates.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROFILE_FILE = Path(__file__).parent / "trader_profile.json"


@dataclass
class TraderProfile:
    """Personal trading style configuration."""

    # ── Instrument preferences ──
    preferred_instruments: list[str]   = field(default_factory=lambda: ["GC1!", "MES1!", "MNQ1!", "SI1!"])
    instrument_weights:    dict        = field(default_factory=lambda: {"GC1!": 1.0, "MES1!": 0.9, "MNQ1!": 0.85, "SI1!": 0.95})

    # ── Session preferences ──
    preferred_sessions:    list[str]   = field(default_factory=lambda: ["asia"])
    session_weights:       dict        = field(default_factory=lambda: {"asia": 1.0, "london": 0.7, "ny": 0.6})

    # Specific windows WITHIN sessions (UTC) — e.g. "first hour of Asia"
    favored_time_windows_utc: list[dict] = field(default_factory=lambda: [
        {"name": "Asia open", "start_hour": 1, "end_hour": 2, "boost": 0.10},
        {"name": "Asia mid",  "start_hour": 2, "end_hour": 4, "boost": 0.05},
    ])

    # ── Setup type preferences ──
    setup_weights: dict = field(default_factory=lambda: {
        "FVG":            1.0,
        "IFVG":           0.9,
        "ORDER_BLOCK":    0.95,
        "LIQUIDITY_GRAB": 0.85,
        "SMT_DIVERGENCE": 0.8,
    })

    # ── Must-have confluences (penalty if missing) ──
    # Each rule: name, weight, description
    required_confluences: list[dict] = field(default_factory=list)

    # ── Deal-breakers — if true, REJECT regardless of score ──
    deal_breakers: list[dict] = field(default_factory=list)

    # ── R:R floors per setup type ──
    rr_minimums: dict = field(default_factory=lambda: {
        "FVG":            2.0,
        "ORDER_BLOCK":    2.5,
        "LIQUIDITY_GRAB": 3.0,
        "SMT_DIVERGENCE": 2.0,
    })

    # ── Entry style — where in the zone you enter ──
    # 0.0 = at the edge, 0.5 = at 50% / CE, 1.0 = at the far side
    entry_position_in_zone: float = 0.5

    # ── Stop placement style ──
    stop_placement: str = "beyond_zone"   # "beyond_zone" | "swing" | "fixed_ticks"
    stop_buffer_ticks: int = 4

    # ── Target style ──
    target_style: str = "next_liquidity"  # "next_liquidity" | "fixed_rr" | "premium_discount"

    # ── Personal notes / rules in plain English ──
    notes: list[str] = field(default_factory=list)


def load_profile() -> TraderProfile:
    if not PROFILE_FILE.exists():
        profile = TraderProfile()
        save_profile(profile)
        return profile

    try:
        data = json.loads(PROFILE_FILE.read_text())
        return TraderProfile(**data)
    except Exception as e:
        logger.warning("Failed to load trader profile: %s — using defaults", e)
        return TraderProfile()


def save_profile(profile: TraderProfile) -> None:
    PROFILE_FILE.write_text(json.dumps(profile.__dict__, indent=2))


def apply_profile_scoring(
    payload: dict,
    base_score: float,
    base_factors: list[str],
) -> tuple[float, list[str]]:
    """
    Apply Brody's personal style preferences on top of generic scoring.
    Returns (adjusted_score, factors_added).
    """
    profile = load_profile()
    score = base_score
    factors = list(base_factors)

    # Instrument preference
    instrument = payload.get("instrument", "")
    inst_weight = profile.instrument_weights.get(instrument, 0.5)
    if inst_weight >= 0.9:
        score += 0.05
        factors.append(f"[Brody] Preferred instrument {instrument}")

    # Session preference
    killzone = payload.get("killzone", "").lower()
    sess_weight = profile.session_weights.get(killzone, 0.5)
    score += (sess_weight - 0.5) * 0.10
    if sess_weight >= 0.9:
        factors.append(f"[Brody] Favored session {killzone}")

    # Time-of-day boost within session
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for window in profile.favored_time_windows_utc:
            if window["start_hour"] <= now.hour < window["end_hour"]:
                score += window.get("boost", 0)
                factors.append(f"[Brody] {window['name']} window")
                break
    except Exception:
        pass

    # Setup type weight
    setup_type = payload.get("setup", "").upper()
    setup_weight = profile.setup_weights.get(setup_type, 0.5)
    if setup_weight >= 0.95:
        score += 0.05
        factors.append(f"[Brody] High-confidence setup type")

    # R:R floor per setup
    try:
        entry = float(payload.get("entry", 0))
        stop  = float(payload.get("stop", 0))
        target = float(payload.get("target", 0))
        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = reward / risk if risk > 0 else 0
        min_rr = profile.rr_minimums.get(setup_type, 2.0)
        if rr < min_rr:
            score -= 0.15
            factors.append(f"[Brody] R:R {rr:.1f} below personal floor {min_rr} for {setup_type}")
    except (TypeError, ValueError):
        pass

    # Required confluences (each missing one is a penalty)
    for req in profile.required_confluences:
        condition_field = req.get("payload_field")
        expected = req.get("expected_value")
        weight = req.get("weight", -0.10)
        name = req.get("name", "unknown")
        if condition_field and payload.get(condition_field) != expected:
            score += weight  # weight is usually negative
            factors.append(f"[Brody] Missing required: {name}")

    # Deal-breakers (hard reject)
    for db in profile.deal_breakers:
        condition_field = db.get("payload_field")
        forbidden_value = db.get("forbidden_value")
        name = db.get("name", "unknown")
        if condition_field and payload.get(condition_field) == forbidden_value:
            score = 0.0  # nuke the score
            factors.append(f"[Brody] DEAL-BREAKER: {name}")

    return max(0.0, min(1.0, score)), factors


def describe_profile() -> str:
    """Human-readable summary of current profile — for dashboard / Telegram."""
    p = load_profile()
    lines = [
        f"Preferred instruments: {', '.join(p.preferred_instruments)}",
        f"Preferred sessions: {', '.join(p.preferred_sessions)}",
        f"R:R floors: {p.rr_minimums}",
        f"Required confluences: {len(p.required_confluences)}",
        f"Deal-breakers: {len(p.deal_breakers)}",
    ]
    if p.notes:
        lines.append("Notes:")
        for n in p.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)
