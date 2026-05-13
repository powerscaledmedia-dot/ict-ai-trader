"""
ICT Backtesting Engine — replay historical OHLCV data through the ICT scanner.

Data source: yfinance (free) for ES=F, NQ=F, GC=F, SI=F.
Applies ICT detection logic candle-by-candle and simulates entry/exit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    entry_time: str
    exit_time: Optional[str]
    setup_type: str
    direction: str
    instrument: str
    entry: float
    stop: float
    target: float
    exit_price: float
    pnl: float
    outcome: str   # "win" | "loss" | "breakeven"
    rr: float
    grade: str
    session: str


@dataclass
class BacktestResult:
    instrument: str
    start_date: str
    end_date: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_rr: float
    total_pnl: float
    max_drawdown: float
    sharpe_ratio: float
    trades: list[BacktestTrade] = field(default_factory=list)
    by_setup: dict = field(default_factory=dict)
    by_session: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)


def _get_session(dt: datetime) -> str:
    """Returns session name based on UTC hour."""
    h = dt.hour
    if 1 <= h < 5:
        return "asia"
    if 7 <= h < 11:
        return "london"
    if 13 <= h < 17:
        return "ny"
    return "off"


def _fetch_historical(instrument: str, start: str, end: str, interval: str = "5m") -> list[dict]:
    """
    Fetch historical OHLCV via yfinance.
    instrument: "MES=F" | "MNQ=F" | "GC=F" | "SI=F"
    Returns list of {time, open, high, low, close, volume}.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(instrument)
        df = ticker.history(start=start, end=end, interval=interval)
        if df.empty:
            logger.warning("No data returned for %s %s–%s", instrument, start, end)
            return []

        candles = []
        for ts, row in df.iterrows():
            candles.append({
                "time": ts.isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0)),
            })
        return candles
    except ImportError:
        logger.error("yfinance not installed — run: pip install yfinance")
        return []
    except Exception as e:
        logger.error("Historical data fetch failed: %s", e)
        return []


def _simulate_trade(
    candles: list[dict],
    entry_idx: int,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    setup_type: str,
    grade: str,
    session: str,
    point_value: float = 5.0,
    contracts: int = 1,
) -> BacktestTrade:
    """
    Simulate a trade from entry_idx forward until stop or target hit.
    Returns a BacktestTrade with P&L.
    """
    entry_time = candles[entry_idx]["time"]
    exit_price = entry
    exit_time = None
    outcome = "open"

    for i in range(entry_idx + 1, min(entry_idx + 100, len(candles))):
        c = candles[i]
        if direction == "bullish":
            if c["low"] <= stop:
                exit_price = stop
                outcome = "loss"
                exit_time = c["time"]
                break
            if c["high"] >= target:
                exit_price = target
                outcome = "win"
                exit_time = c["time"]
                break
        else:
            if c["high"] >= stop:
                exit_price = stop
                outcome = "loss"
                exit_time = c["time"]
                break
            if c["low"] <= target:
                exit_price = target
                outcome = "win"
                exit_time = c["time"]
                break

    if outcome == "open":
        exit_price = candles[min(entry_idx + 99, len(candles) - 1)]["close"]
        outcome = "loss" if (
            (direction == "bullish" and exit_price < entry) or
            (direction == "bearish" and exit_price > entry)
        ) else "win"
        exit_time = candles[min(entry_idx + 99, len(candles) - 1)]["time"]

    risk_points = abs(entry - stop)
    reward_points = abs(target - entry)
    rr = reward_points / risk_points if risk_points > 0 else 0.0

    if direction == "bullish":
        pnl = (exit_price - entry) * point_value * contracts
    else:
        pnl = (entry - exit_price) * point_value * contracts

    return BacktestTrade(
        entry_time=entry_time,
        exit_time=exit_time,
        setup_type=setup_type,
        direction=direction,
        instrument=instrument if False else "",
        entry=entry,
        stop=stop,
        target=target,
        exit_price=exit_price,
        pnl=round(pnl, 2),
        outcome=outcome,
        rr=round(rr, 2),
        grade=grade,
        session=session,
    )


def run_backtest(
    instrument: str,
    start_date: str,
    end_date: str,
    setups: Optional[list[str]] = None,
    killzone_filter: Optional[str] = None,
    min_grade: str = "B",
    interval: str = "5m",
) -> BacktestResult:
    """
    Run a full backtest.
    instrument: "MES=F" | "MNQ=F" | "GC=F" | "SI=F"
    setups: list of setup types to test, or None for all
    killzone_filter: "asia" | "london" | "ny" | None for all
    """
    from ict_scanner import Candle, detect_fvg, detect_order_block, detect_liquidity_grab, _grade_from_score, _score_from_payload

    _POINT_VALUES = {
        "MES=F": 5.0, "MNQ=F": 2.0, "GC=F": 100.0, "SI=F": 50.0,
        "ES=F": 50.0, "NQ=F": 20.0,
    }
    point_value = _POINT_VALUES.get(instrument, 5.0)

    raw_candles = _fetch_historical(instrument, start_date, end_date, interval)
    if not raw_candles:
        return BacktestResult(
            instrument=instrument, start_date=start_date, end_date=end_date,
            total_trades=0, wins=0, losses=0, win_rate=0.0, avg_rr=0.0,
            total_pnl=0.0, max_drawdown=0.0, sharpe_ratio=0.0,
        )

    candle_objects = [
        Candle(
            open=c["open"], high=c["high"], low=c["low"],
            close=c["close"], volume=c["volume"]
        )
        for c in raw_candles
    ]

    trades: list[BacktestTrade] = []
    equity_curve = [0.0]

    for i in range(3, len(candle_objects) - 10):
        dt = datetime.fromisoformat(raw_candles[i]["time"].replace("Z", "+00:00").split("+")[0])
        session = _get_session(dt)

        if killzone_filter and session != killzone_filter:
            continue

        window = candle_objects[max(0, i - 20): i + 1]
        c = raw_candles[i]

        detected = []

        # FVG check
        fvg = detect_fvg(window[-3:])
        if fvg and (not setups or "FVG" in setups):
            direction, top, bottom = fvg
            if direction.value == "bullish":
                entry = (top + bottom) / 2
                stop = bottom - (top - bottom) * 0.5
                target = entry + (entry - stop) * 2.5
            else:
                entry = (top + bottom) / 2
                stop = top + (top - bottom) * 0.5
                target = entry - (stop - entry) * 2.5

            payload = {
                "setup": "FVG", "direction": direction.value,
                "entry": entry, "stop": stop, "target": target,
                "killzone": session, "timeframe": "5",
            }
            score, _ = _score_from_payload(payload)
            grade = _grade_from_score(score)
            grade_order = {"A": 0, "B": 1, "C": 2}
            min_grade_order = {"A": 0, "B": 1, "C": 2}
            if grade_order.get(grade.value, 2) <= min_grade_order.get(min_grade, 1):
                detected.append(("FVG", direction.value, entry, stop, target, grade.value))

        # Order Block check
        ob = detect_order_block(window)
        if ob and (not setups or "ORDER_BLOCK" in setups):
            direction, ob_high, ob_low = ob
            if direction.value == "bullish":
                entry = ob_high
                stop = ob_low - (ob_high - ob_low) * 0.2
                target = entry + (entry - stop) * 3.0
            else:
                entry = ob_low
                stop = ob_high + (ob_high - ob_low) * 0.2
                target = entry - (stop - entry) * 3.0

            payload = {
                "setup": "ORDER_BLOCK", "direction": direction.value,
                "entry": entry, "stop": stop, "target": target,
                "killzone": session, "timeframe": "5",
            }
            score, _ = _score_from_payload(payload)
            grade = _grade_from_score(score)
            grade_order = {"A": 0, "B": 1, "C": 2}
            if grade_order.get(grade.value, 2) <= min_grade_order.get(min_grade, 1):
                detected.append(("ORDER_BLOCK", direction.value, entry, stop, target, grade.value))

        # Liquidity Grab check
        lg = detect_liquidity_grab(window)
        if lg and (not setups or "LIQUIDITY_GRAB" in setups):
            direction, level = lg
            entry = c["close"]
            if direction.value == "bullish":
                stop = c["low"] - (c["high"] - c["low"]) * 0.3
                target = entry + (entry - stop) * 2.5
            else:
                stop = c["high"] + (c["high"] - c["low"]) * 0.3
                target = entry - (stop - entry) * 2.5

            detected.append(("LIQUIDITY_GRAB", direction.value, entry, stop, target, "B"))

        # Simulate first detected setup only (no stacking)
        if detected:
            setup_type, direction, entry, stop, target, grade_val = detected[0]
            trade = _simulate_trade(
                raw_candles, i, direction, entry, stop, target,
                setup_type, grade_val, session, point_value,
            )
            trade.instrument = instrument
            trades.append(trade)
            equity_curve.append(equity_curve[-1] + trade.pnl)

            # Skip forward past this trade to avoid re-entering
            # (simplified — skip 10 candles)
            i += 10

    if not trades:
        return BacktestResult(
            instrument=instrument, start_date=start_date, end_date=end_date,
            total_trades=0, wins=0, losses=0, win_rate=0.0, avg_rr=0.0,
            total_pnl=0.0, max_drawdown=0.0, sharpe_ratio=0.0,
        )

    wins_list = [t for t in trades if t.outcome == "win"]
    losses_list = [t for t in trades if t.outcome == "loss"]
    total_pnl = sum(t.pnl for t in trades)
    avg_rr = sum(t.rr for t in trades) / len(trades) if trades else 0.0

    # Max drawdown
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd

    # Sharpe (simplified)
    if len(equity_curve) > 1:
        import statistics
        pnls = [equity_curve[i] - equity_curve[i - 1] for i in range(1, len(equity_curve))]
        mean_pnl = statistics.mean(pnls)
        std_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 1.0
        sharpe = (mean_pnl / std_pnl) * (252 ** 0.5) if std_pnl > 0 else 0.0
    else:
        sharpe = 0.0

    # By setup breakdown
    by_setup: dict = {}
    for t in trades:
        if t.setup_type not in by_setup:
            by_setup[t.setup_type] = {"wins": 0, "losses": 0, "pnl": 0.0}
        by_setup[t.setup_type]["wins" if t.outcome == "win" else "losses"] += 1
        by_setup[t.setup_type]["pnl"] += t.pnl

    # By session breakdown
    by_session: dict = {}
    for t in trades:
        if t.session not in by_session:
            by_session[t.session] = {"wins": 0, "losses": 0, "pnl": 0.0}
        by_session[t.session]["wins" if t.outcome == "win" else "losses"] += 1
        by_session[t.session]["pnl"] += t.pnl

    # Statistical validation — is this edge real or lucky?
    validation_report = {}
    if len(trades) >= 5:
        try:
            from validation import run_full_validation
            trade_pnls = [t.pnl for t in trades]
            validation_report = run_full_validation(trade_pnls, initial_capital=50_000)
        except Exception as e:
            logger.warning("Validation skipped: %s", e)

    return BacktestResult(
        instrument=instrument,
        start_date=start_date,
        end_date=end_date,
        total_trades=len(trades),
        wins=len(wins_list),
        losses=len(losses_list),
        win_rate=len(wins_list) / len(trades) if trades else 0.0,
        avg_rr=round(avg_rr, 2),
        total_pnl=round(total_pnl, 2),
        max_drawdown=round(max_dd, 2),
        sharpe_ratio=round(sharpe, 2),
        trades=trades,
        by_setup=by_setup,
        by_session=by_session,
        validation=validation_report,
    )
