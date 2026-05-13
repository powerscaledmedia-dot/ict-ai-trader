"""
Statistical Validation — Is the ICT system's edge REAL or just lucky?

Three independent tests:
  • Monte Carlo permutation: shuffle trade P&L order, see if Sharpe is significant
  • Bootstrap Sharpe CI: 95% confidence interval on risk-adjusted return
  • Walk-Forward: split history into windows, test consistency over time

Run BEFORE trusting the system with real eval capital. A backtest with
72% win rate over 20 trades could easily be luck. After Monte Carlo,
you'll know if the edge survives random reordering.

Adapted from HKUDS/Vibe-Trading (MIT license).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    pnl: float
    won: bool = False


# ==================== Monte Carlo Permutation Test ====================

def monte_carlo_test(
    trade_pnls: list[float],
    initial_capital: float = 50_000,
    n_simulations: int = 1000,
    seed: int = 42,
) -> dict:
    """
    Shuffle trade P&L order 1,000x. If your Sharpe is rarely exceeded by
    random reordering, the strategy has a real edge. If 50%+ of shuffles
    beat your actual Sharpe — your "edge" is just luck.

    Returns dict with:
      p_value_sharpe: probability of seeing this Sharpe by chance.
                      <0.05 = statistically significant edge
                      >0.20 = probably just lucky
      actual_sharpe, simulated_sharpe_p5, p95 distribution
    """
    if len(trade_pnls) < 5:
        return {"error": "need at least 5 trades", "p_value_sharpe": 1.0}

    pnls = np.array(trade_pnls, dtype=float)
    actual = _path_metrics(pnls, initial_capital)

    rng = np.random.default_rng(seed)
    sim_sharpes = []
    sharpe_better_count = 0
    dd_better_count = 0

    for _ in range(n_simulations):
        shuffled = rng.permutation(pnls)
        sim = _path_metrics(shuffled, initial_capital)
        sim_sharpes.append(sim["sharpe"])
        if sim["sharpe"] >= actual["sharpe"]:
            sharpe_better_count += 1
        if sim["max_dd"] >= actual["max_dd"]:  # less negative = better
            dd_better_count += 1

    sim_arr = np.array(sim_sharpes)
    p_value = sharpe_better_count / n_simulations

    return {
        "actual_sharpe":         round(actual["sharpe"], 4),
        "actual_max_dd":         round(actual["max_dd"], 4),
        "p_value_sharpe":        round(p_value, 4),
        "p_value_max_dd":        round(dd_better_count / n_simulations, 4),
        "simulated_sharpe_mean": round(float(sim_arr.mean()), 4),
        "simulated_sharpe_std":  round(float(sim_arr.std()), 4),
        "simulated_sharpe_p5":   round(float(np.percentile(sim_arr, 5)), 4),
        "simulated_sharpe_p95":  round(float(np.percentile(sim_arr, 95)), 4),
        "n_simulations":         n_simulations,
        "n_trades":              len(trade_pnls),
        "verdict":               _verdict(p_value),
    }


def _path_metrics(pnls: np.ndarray, initial_capital: float) -> dict:
    equity = initial_capital + np.cumsum(pnls)
    returns = np.diff(equity) / equity[:-1] if len(equity) > 1 else np.array([0.0])
    std = returns.std()
    sharpe = float(returns.mean() / (std + 1e-10) * np.sqrt(252))
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / np.where(peak > 0, peak, 1.0)
    return {"sharpe": sharpe, "max_dd": float(dd.min())}


def _verdict(p_value: float) -> str:
    if p_value < 0.05:
        return "STRONG EDGE — statistically significant (p<0.05)"
    elif p_value < 0.15:
        return "MODERATE EDGE — likely real but not conclusive"
    elif p_value < 0.30:
        return "WEAK EDGE — could be luck, need more trades"
    else:
        return "NO EDGE — performance indistinguishable from random"


# ==================== Bootstrap Sharpe CI ====================

def bootstrap_sharpe_ci(
    trade_pnls: list[float],
    initial_capital: float = 50_000,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    seed: int = 42,
) -> dict:
    """
    Resample trades WITH replacement to compute a confidence interval on Sharpe.
    Tells you the range of Sharpe ratios consistent with your data.
    """
    if len(trade_pnls) < 5:
        return {"error": "need at least 5 trades"}

    pnls = np.array(trade_pnls, dtype=float)
    actual = _path_metrics(pnls, initial_capital)

    rng = np.random.default_rng(seed)
    boot_sharpes = []
    for _ in range(n_bootstrap):
        sample = rng.choice(pnls, size=len(pnls), replace=True)
        boot_sharpes.append(_path_metrics(sample, initial_capital)["sharpe"])

    boot = np.array(boot_sharpes)
    alpha = 1 - confidence
    lower = float(np.percentile(boot, alpha / 2 * 100))
    upper = float(np.percentile(boot, (1 - alpha / 2) * 100))

    return {
        "actual_sharpe":      round(actual["sharpe"], 4),
        "ci_lower":           round(lower, 4),
        "ci_upper":           round(upper, 4),
        "confidence_pct":     int(confidence * 100),
        "interpretation":     f"Sharpe is {round(actual['sharpe'], 2)} (95% CI: {round(lower, 2)} to {round(upper, 2)})",
        "n_bootstrap":        n_bootstrap,
        "robust":             lower > 0,
    }


# ==================== Walk-Forward Analysis ====================

def walk_forward_analysis(
    trade_pnls: list[float],
    n_windows: int = 4,
    initial_capital: float = 50_000,
) -> dict:
    """
    Split trades into N sequential windows. If win rate / Sharpe is
    consistent across windows → real edge. If only one window is good →
    overfitting or regime-dependent luck.
    """
    if len(trade_pnls) < n_windows * 5:
        return {"error": f"need at least {n_windows * 5} trades for {n_windows} windows"}

    pnls = np.array(trade_pnls, dtype=float)
    window_size = len(pnls) // n_windows
    windows = []

    for i in range(n_windows):
        start = i * window_size
        end = start + window_size if i < n_windows - 1 else len(pnls)
        chunk = pnls[start:end]
        win_rate = float((chunk > 0).mean())
        metrics = _path_metrics(chunk, initial_capital)
        windows.append({
            "window":     i + 1,
            "trades":     len(chunk),
            "win_rate":   round(win_rate * 100, 1),
            "total_pnl":  round(float(chunk.sum()), 2),
            "sharpe":     round(metrics["sharpe"], 4),
        })

    win_rates = [w["win_rate"] for w in windows]
    sharpe_vals = [w["sharpe"] for w in windows]
    consistency = float(np.std(win_rates) / max(1.0, np.mean(win_rates)))

    return {
        "n_windows":           n_windows,
        "windows":             windows,
        "win_rate_mean":       round(float(np.mean(win_rates)), 1),
        "win_rate_std":        round(float(np.std(win_rates)), 1),
        "sharpe_mean":         round(float(np.mean(sharpe_vals)), 4),
        "consistency_ratio":   round(consistency, 4),
        "verdict":             _wf_verdict(consistency, win_rates),
    }


def _wf_verdict(consistency: float, win_rates: list[float]) -> str:
    min_wr = min(win_rates)
    if consistency < 0.15 and min_wr > 50:
        return "CONSISTENT — performance stable across time, edge is real"
    elif consistency < 0.30:
        return "MOSTLY CONSISTENT — minor regime sensitivity"
    elif min_wr < 40:
        return "UNSTABLE — one or more windows show poor performance, regime risk"
    else:
        return "VARIABLE — performance fluctuates significantly across windows"


# ==================== Combined report ====================

def run_full_validation(
    trade_pnls: list[float],
    initial_capital: float = 50_000,
) -> dict:
    """Run all three tests and return a unified report."""
    mc = monte_carlo_test(trade_pnls, initial_capital)
    boot = bootstrap_sharpe_ci(trade_pnls, initial_capital)
    wf = walk_forward_analysis(trade_pnls, initial_capital=initial_capital)

    return {
        "n_trades":      len(trade_pnls),
        "monte_carlo":   mc,
        "bootstrap":     boot,
        "walk_forward":  wf,
        "overall_verdict": _overall_verdict(mc, wf),
    }


def _overall_verdict(mc: dict, wf: dict) -> str:
    p = mc.get("p_value_sharpe", 1.0)
    wf_v = wf.get("verdict", "")

    if p < 0.05 and "CONSISTENT" in wf_v and "UN" not in wf_v:
        return "✓ GO LIVE — edge is statistically significant and consistent"
    if p < 0.15 and "CONSISTENT" in wf_v and "UN" not in wf_v:
        return "✓ CAUTIOUS GO — edge likely real, scale slowly"
    if p > 0.30:
        return "✗ DO NOT GO LIVE — performance indistinguishable from random"
    if "UNSTABLE" in wf_v:
        return "✗ DO NOT GO LIVE — performance unstable across time windows"
    return "⚠ MARGINAL — collect more data before going live"
