"""
SMC Analyzer — Real ICT/Smart Money Concepts pattern confirmation.

Wraps the smartmoneyconcepts library to provide AUDIT-QUALITY confluence
detection for our scanner:
  - BOS  (Break of Structure)   — trend continuation confirmation
  - CHOCH (Change of Character)  — reversal confirmation
  - FVG  (Fair Value Gap)        — premium/discount entry zone
  - OB   (Order Block)           — institutional zone

Webhooks only give us entry/stop/target. This module fetches recent OHLCV
for the instrument and verifies the setup is structurally aligned with
real ICT concepts — turning our heuristic score into evidence-backed grade.

Install:
  pip install smartmoneyconcepts pandas yfinance

Graceful degradation:
  If smartmoneyconcepts is not installed, returns an empty result and the
  scanner falls back to its heuristic scoring.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# smartmoneyconcepts prints emoji on import — force UTF-8 on Windows
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

_SMC_AVAILABLE = False
try:
    from smartmoneyconcepts import smc
    _SMC_AVAILABLE = True
except ImportError:
    logger.warning("smartmoneyconcepts not installed — SMC enhancement disabled")
    smc = None  # type: ignore

_YF_AVAILABLE = False
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    logger.warning("yfinance not installed — SMC enhancement cannot fetch OHLCV")
    yf = None  # type: ignore


# ==================== Config ====================
SMC_ENABLED      = os.getenv("ICT_SMC_ENABLED", "true").lower() == "true"
SMC_LOOKBACK_BARS = int(os.getenv("ICT_SMC_LOOKBACK_BARS", "200"))
SMC_SWING_LENGTH  = int(os.getenv("ICT_SMC_SWING_LENGTH", "10"))
SMC_CACHE_SECONDS = int(os.getenv("ICT_SMC_CACHE_SECONDS", "30"))


# ==================== Instrument → yfinance symbol map ====================
_YF_SYMBOL_MAP = {
    "MES1!": "MES=F",  "MES":  "MES=F",  "MESH5": "MES=F",
    "MNQ1!": "MNQ=F",  "MNQ":  "MNQ=F",  "MNQH5": "MNQ=F",
    "GC1!":  "GC=F",   "GC":   "GC=F",   "GCM5":  "GC=F",
    "SI1!":  "SI=F",   "SI":   "SI=F",   "SIM5":  "SI=F",
    "MGC":   "MGC=F",  "MCL":  "MCL=F",
}

# Timeframe in TV format → yfinance interval
_INTERVAL_MAP = {
    "1":   "1m",   "2":   "2m",  "3":   "5m",
    "5":   "5m",   "15":  "15m", "30":  "30m",
    "60":  "1h",   "240": "1h",  "D":   "1d",
}


# ==================== OHLCV cache ====================
_cache: dict[str, tuple[float, object]] = {}


def _yf_symbol(instrument: str) -> Optional[str]:
    """Map TV symbol → yfinance symbol."""
    return _YF_SYMBOL_MAP.get(instrument.upper())


def _yf_interval(timeframe: str) -> str:
    return _INTERVAL_MAP.get(str(timeframe), "5m")


def _fetch_ohlcv(instrument: str, timeframe: str = "5"):
    """Fetch recent OHLCV with caching. Returns DataFrame or None."""
    if not _YF_AVAILABLE:
        return None

    cache_key = f"{instrument}:{timeframe}"
    now = time.time()
    if cache_key in _cache:
        ts, df = _cache[cache_key]
        if now - ts < SMC_CACHE_SECONDS:
            return df

    symbol = _yf_symbol(instrument)
    if not symbol:
        logger.debug("SMC: no yfinance mapping for %s", instrument)
        return None

    interval = _yf_interval(timeframe)
    # Periods need to match interval availability on yfinance
    period_map = {"1m": "5d", "2m": "5d", "5m": "30d", "15m": "60d", "30m": "60d", "1h": "60d", "1d": "1y"}
    period = period_map.get(interval, "30d")

    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        # Flatten multi-level columns if present
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        else:
            df.columns = [str(c).lower() for c in df.columns]

        # Trim to lookback window
        df = df.tail(SMC_LOOKBACK_BARS).copy()
        _cache[cache_key] = (now, df)
        return df
    except Exception as e:
        logger.warning("SMC: OHLCV fetch failed for %s: %s", instrument, e)
        return None


# ==================== Analysis result ====================

@dataclass
class SMCAnalysis:
    available:           bool   = False
    bos_direction:       int    = 0    # 1=bullish, -1=bearish, 0=none
    choch_direction:     int    = 0
    in_fvg_zone:         bool   = False
    fvg_direction:       int    = 0
    in_ob_zone:          bool   = False
    ob_direction:        int    = 0
    structural_alignment: int   = 0    # +1 aligned, -1 conflicting, 0 neutral
    confluence_factors:  list[str] = field(default_factory=list)
    confluence_count:    int    = 0


def analyze_setup(
    instrument: str,
    timeframe: str,
    entry: float,
    direction: str,   # "bullish" | "bearish"
) -> SMCAnalysis:
    """
    Verify a webhook setup against real SMC pattern detection.
    Returns an analysis with confluence factors to feed into the scanner.

    direction is the WEBHOOK's claimed direction. structural_alignment is
    +1 if SMC confirms it, -1 if SMC conflicts, 0 if no strong opinion.
    """
    result = SMCAnalysis()

    if not SMC_ENABLED or not _SMC_AVAILABLE:
        return result

    df = _fetch_ohlcv(instrument, timeframe)
    if df is None or len(df) < SMC_SWING_LENGTH * 2:
        return result

    result.available = True
    claimed_dir = 1 if direction == "bullish" else -1

    try:
        ohlc = df[["open", "high", "low", "close", "volume"]].copy()

        # 1. Swing highs/lows
        swing_hl = smc.swing_highs_lows(ohlc, swing_length=SMC_SWING_LENGTH)

        # 2. BOS / ChoCH structure
        bos_choch = smc.bos_choch(ohlc, swing_highs_lows=swing_hl, close_break=True)
        recent_window = 5  # last 5 bars

        bos_recent   = bos_choch["BOS"].tail(recent_window).fillna(0)
        choch_recent = bos_choch["CHOCH"].tail(recent_window).fillna(0)

        # Direction of most recent non-zero signal
        for val in reversed(bos_recent.tolist()):
            if val != 0:
                result.bos_direction = int(val)
                break
        for val in reversed(choch_recent.tolist()):
            if val != 0:
                result.choch_direction = int(val)
                break

        # 3. FVG
        fvg_data = smc.fvg(ohlc)
        # Check if entry sits within any recent unfilled FVG
        if "Top" in fvg_data.columns and "Bottom" in fvg_data.columns:
            recent_fvg = fvg_data.tail(20)
            for _, row in recent_fvg.iterrows():
                fvg_dir = row.get("FVG", 0)
                top = row.get("Top")
                bot = row.get("Bottom")
                if fvg_dir != 0 and top is not None and bot is not None:
                    try:
                        if bot <= entry <= top:
                            result.in_fvg_zone = True
                            result.fvg_direction = int(fvg_dir)
                            break
                    except (TypeError, ValueError):
                        pass

        # 4. Order Blocks
        try:
            ob_data = smc.ob(ohlc, swing_highs_lows=swing_hl)
            if ob_data is not None and not ob_data.empty:
                recent_ob = ob_data.tail(20)
                for _, row in recent_ob.iterrows():
                    ob_dir = row.get("OB", 0)
                    top = row.get("Top")
                    bot = row.get("Bottom")
                    if ob_dir != 0 and top is not None and bot is not None:
                        try:
                            if bot <= entry <= top:
                                result.in_ob_zone = True
                                result.ob_direction = int(ob_dir)
                                break
                        except (TypeError, ValueError):
                            pass
        except Exception:
            pass  # OB detection optional

        # ── Build confluence factors ──
        factors: list[str] = []

        if result.choch_direction == claimed_dir:
            factors.append(f"ChoCH confirms {direction}")
        elif result.choch_direction == -claimed_dir:
            factors.append(f"ChoCH CONFLICT: opposes {direction}")

        if result.bos_direction == claimed_dir:
            factors.append(f"BOS confirms {direction}")
        elif result.bos_direction == -claimed_dir:
            factors.append(f"BOS CONFLICT: opposes {direction}")

        if result.in_fvg_zone:
            if result.fvg_direction == claimed_dir:
                factors.append(f"Entry inside aligned FVG zone")
            else:
                factors.append(f"FVG CONFLICT: entry in opposite-direction FVG")

        if result.in_ob_zone:
            if result.ob_direction == claimed_dir:
                factors.append(f"Entry inside aligned Order Block")
            else:
                factors.append(f"OB CONFLICT: entry in opposite-direction OB")

        # ── Alignment score ──
        aligned   = sum(1 for f in factors if "CONFLICT" not in f)
        conflicts = sum(1 for f in factors if "CONFLICT" in f)

        if aligned >= 2 and conflicts == 0:
            result.structural_alignment = 1
        elif conflicts >= 1:
            result.structural_alignment = -1
        else:
            result.structural_alignment = 0

        result.confluence_factors = factors
        result.confluence_count   = aligned

    except Exception as e:
        logger.warning("SMC analysis exception: %s", e)
        return result

    return result


def is_available() -> bool:
    return _SMC_AVAILABLE and _YF_AVAILABLE
