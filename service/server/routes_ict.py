"""
ICT Routes — FastAPI endpoints for the ICT trading system.

Endpoints:
  POST /webhook/tradingview    — receive TV alert, run 4-agent pipeline
  GET  /ict/status             — agent + risk dashboard
  GET  /ict/signals            — recent signal log
  GET  /ict/trades             — trade history
  POST /ict/backtest           — run a backtest
  POST /ict/close              — manually close a position
  GET  /ict/weights            — current setup weights
  POST /ict/weights            — update setup weights manually
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from database import get_db_connection
from ict_scanner import evaluate_webhook_payload, get_setup_weights, load_setup_weights, Grade
from killzone_manager import check_killzone, KillzoneStatus, get_session_schedule
from news_sentinel import check_news, SentinelStatus
from risk_governor import check_risk, RiskStatus, get_risk_dashboard, record_trade_result
from account_guard import check_account_guard, GuardState, get_guard_dashboard
from session_rules import check_session_rules, SessionStatus, get_session_dashboard
from monthly_progress import get_monthly_progress

logger = logging.getLogger(__name__)

ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "demo")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Tradovate bridge — TopStep execution (optional)
try:
    import tradovate_bridge as tvb
    _TRADOVATE_ENABLED = tvb.is_configured()
except ImportError:
    _TRADOVATE_ENABLED = False

# Rithmic bridge — Lucid Trading execution (optional)
try:
    import rithmic_bridge as rb
    _RITHMIC_ENABLED = rb.is_configured()
except ImportError:
    _RITHMIC_ENABLED = False


# ==================== Pydantic Models ====================

class TVWebhookPayload(BaseModel):
    setup: str                  # FVG | IFVG | ORDER_BLOCK | LIQUIDITY_GRAB | SMT_DIVERGENCE
    direction: str              # bullish | bearish
    instrument: str             # MES1! | MNQ1! | GC1! | SI1!
    timeframe: str = "5"
    entry: float
    stop: float
    target: float
    killzone: str = ""          # asia | london | ny (TV can pass this)
    confidence: str = ""
    timestamp: str = ""


class BacktestRequest(BaseModel):
    instrument: str = "MES=F"
    start_date: str             # YYYY-MM-DD
    end_date: str               # YYYY-MM-DD
    setups: Optional[list[str]] = None
    killzone_filter: Optional[str] = None
    min_grade: str = "B"


class WeightsUpdate(BaseModel):
    FVG: float = 1.0
    IFVG: float = 0.9
    ORDER_BLOCK: float = 0.95
    LIQUIDITY_GRAB: float = 0.85
    SMT_DIVERGENCE: float = 0.8


# ==================== Helpers ====================

def _send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=8,
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def _log_signal(
    signal_uuid: str,
    payload: dict,
    killzone_status: str,
    scanner_grade: Optional[str],
    scanner_score: Optional[float],
    risk_status: Optional[str],
    news_status: Optional[str],
    final_decision: str,
    rejection_reason: Optional[str],
    trade_id: Optional[int],
) -> None:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO ict_signals (
                    signal_uuid, payload, killzone_status, scanner_grade, scanner_score,
                    risk_status, news_status, final_decision, rejection_reason, trade_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_uuid, json.dumps(payload), killzone_status,
                scanner_grade, scanner_score, risk_status, news_status,
                final_decision, rejection_reason, trade_id,
            ))
            conn.commit()
    except Exception as e:
        logger.error("Signal log failed: %s", e)


def _log_trade(trade_uuid: str, setup: object, contracts: int, order_id: Optional[str], news_status: str) -> int:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ict_trades (
                    trade_uuid, setup_type, direction, instrument, timeframe,
                    entry_price, stop_price, target_price, grade, score,
                    killzone, confluence_factors, news_status, tradovate_order_id,
                    contracts, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """, (
                trade_uuid,
                setup.setup_type.value,
                setup.direction.value,
                setup.instrument,
                setup.timeframe,
                setup.entry,
                setup.stop,
                setup.target,
                setup.grade.value,
                setup.score,
                setup.raw_payload.get("killzone", ""),
                json.dumps(setup.confluence_factors),
                news_status,
                order_id,
                contracts,
            ))
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logger.error("Trade log failed: %s", e)
        return -1


# ==================== Route Registration ====================

def register_ict_routes(app: FastAPI) -> None:

    @app.post("/webhook/tradingview")
    async def receive_tradingview_webhook(request: Request):
        """
        Main TradingView webhook receiver.
        Runs the full 4-agent pipeline and executes if approved.
        Returns 200 immediately (TV requires fast ACK).
        """
        try:
            body = await request.json()
        except Exception:
            # TV sometimes sends plain text — handle gracefully
            body = {}

        signal_uuid = str(uuid.uuid4())

        # ── Agent 0: Account Guard (HARD LOCK — never fail an eval) ──
        guard = check_account_guard()
        if guard.state == GuardState.LOCKED:
            _log_signal(
                signal_uuid, body, "LOCKED", None, None, None, None,
                "REJECTED", f"ACCOUNT GUARD LOCKED: {guard.reason}", None,
            )
            logger.warning("Signal LOCKED by account guard: %s", guard.reason)
            _send_telegram(f"🔒 *ACCOUNT GUARD LOCKED*\n{guard.reason}")
            return {"status": "locked", "reason": guard.reason, "metrics": guard.metrics}

        # ── Agent 0.5: Session Rules (frequency, blackout, cooldown) ──
        sess = check_session_rules()
        if sess.status != SessionStatus.OK:
            _log_signal(
                signal_uuid, body, sess.status.value, None, None, None, None,
                "REJECTED", sess.reason, None,
            )
            logger.info("Signal REJECTED by session rules: %s", sess.reason)
            return {"status": "session_blocked", "reason": sess.reason, "minutes_remaining": sess.minutes_remaining}

        # ── Agent 1: Killzone Manager ──
        kz = check_killzone()
        if kz.status == KillzoneStatus.BLOCKED:
            _log_signal(
                signal_uuid, body, kz.status.value, None, None, None, None,
                "REJECTED", kz.reason, None,
            )
            logger.info("Signal BLOCKED by killzone: %s", kz.reason)
            return {"status": "blocked", "reason": kz.reason}

        if kz.status == KillzoneStatus.WATCH:
            _log_signal(
                signal_uuid, body, kz.status.value, None, None, None, None,
                "WATCHED", kz.reason, None,
            )
            logger.info("Signal WATCHED (outside killzone): %s", kz.reason)
            return {"status": "watched", "reason": kz.reason, "minutes_to_next": kz.minutes_to_next}

        # ── Agent 2: ICT Scanner ──
        body["killzone"] = body.get("killzone") or kz.session or ""
        setup = evaluate_webhook_payload(body)

        if not setup.is_tradeable:
            reason = f"Grade {setup.grade.value} (score {setup.score:.2f}) — R:R {setup.risk_reward:.1f} — below threshold"
            _log_signal(
                signal_uuid, body, kz.status.value, setup.grade.value, setup.score,
                None, None, "REJECTED", reason, None,
            )
            logger.info("Signal REJECTED by scanner: %s", reason)
            return {"status": "rejected", "reason": reason, "grade": setup.grade.value, "score": setup.score}

        # Account Guard may require A-grade only (WARNING/CRITICAL state)
        if guard.min_grade_required == "A" and setup.grade.value != "A":
            reason = f"Account guard requires A-grade (state={guard.state.value}); this setup is grade {setup.grade.value}"
            _log_signal(
                signal_uuid, body, guard.state.value, setup.grade.value, setup.score,
                None, None, "REJECTED", reason, None,
            )
            logger.info("Signal REJECTED by guard grade-floor: %s", reason)
            return {"status": "guard_filtered", "reason": reason}

        # ── Agent 4: News Sentinel ── (check before risk so we don't waste a position slot check)
        news = check_news(ALPHA_VANTAGE_KEY)
        if news.status == SentinelStatus.HALT:
            _log_signal(
                signal_uuid, body, kz.status.value, setup.grade.value, setup.score,
                None, news.status.value, "REJECTED", news.reason, None,
            )
            logger.info("Signal REJECTED by news sentinel: %s", news.reason)
            return {"status": "rejected", "reason": news.reason, "news_status": news.status.value}

        # ── Agent 3: Risk Governor ──
        risk = check_risk(setup.instrument, setup.entry, setup.stop)
        if risk.status == RiskStatus.REJECTED:
            _log_signal(
                signal_uuid, body, kz.status.value, setup.grade.value, setup.score,
                risk.status.value, news.status.value, "REJECTED", risk.reason, None,
            )
            logger.info("Signal REJECTED by risk governor: %s", risk.reason)
            return {"status": "rejected", "reason": risk.reason}

        # ── All agents GREEN — execute ──
        base_contracts = risk.suggested_size or 1
        # Apply Account Guard size multiplier (1.0 SAFE, 0.5 WARNING, 0.33 CRITICAL)
        contracts = max(1, int(round(base_contracts * guard.size_multiplier)))
        if contracts < base_contracts:
            logger.info(
                "Guard scaled size: %d → %d (multiplier=%.2f, state=%s)",
                base_contracts, contracts, guard.size_multiplier, guard.state.value
            )
        order_id = None
        brokers_fired: list[str] = []
        last_error: Optional[str] = None

        # TopStep → Tradovate
        if _TRADOVATE_ENABLED:
            from tradovate_bridge import OrderAction, place_order
            action = OrderAction.BUY if setup.direction.value == "bullish" else OrderAction.SELL
            result = place_order(
                symbol=setup.instrument.replace("1!", "H5"),
                action=action,
                quantity=contracts,
                bracket_stop=setup.stop,
                bracket_target=setup.target,
            )
            if result.success:
                order_id = str(result.order_id)
                brokers_fired.append("tradovate")
                logger.info("Tradovate order: %s x%d id=%s", setup.instrument, contracts, order_id)
            else:
                last_error = f"Tradovate: {result.message}"
                logger.error("Tradovate order failed: %s", result.message)

        # Lucid → Rithmic
        if _RITHMIC_ENABLED:
            from rithmic_bridge import OrderAction as RAction, place_order as rplace
            raction = RAction.BUY if setup.direction.value == "bullish" else RAction.SELL
            rresult = rplace(
                symbol=setup.instrument,
                action=raction,
                quantity=contracts,
                bracket_stop=setup.stop,
                bracket_target=setup.target,
            )
            if rresult.success:
                if not order_id:
                    order_id = str(rresult.order_id)
                brokers_fired.append("rithmic")
                logger.info("Rithmic order: %s x%d id=%s", setup.instrument, contracts, rresult.order_id)
            else:
                last_error = (last_error or "") + f" | Rithmic: {rresult.message}"
                logger.error("Rithmic order failed: %s", rresult.message)

        if not _TRADOVATE_ENABLED and not _RITHMIC_ENABLED:
            logger.info("No broker configured — paper trade: %s %s x%d", setup.direction.value, setup.instrument, contracts)
        elif not brokers_fired and last_error:
            return {"status": "error", "reason": f"All brokers failed: {last_error}"}

        trade_id = _log_trade(signal_uuid, setup, contracts, order_id, news.status.value)

        _log_signal(
            signal_uuid, body, kz.status.value, setup.grade.value, setup.score,
            risk.status.value, news.status.value, "EXECUTED", None, trade_id,
        )

        # Telegram notification
        emoji = "🟢" if setup.direction.value == "bullish" else "🔴"
        brokers_str = " + ".join(b.upper() for b in brokers_fired) if brokers_fired else "PAPER"
        msg = (
            f"{emoji} *ICT TRADE EXECUTED* [{brokers_str}]\n"
            f"Setup: {setup.setup_type.value} | {setup.direction.value.upper()}\n"
            f"Instrument: {setup.instrument} x{contracts}\n"
            f"Entry: {setup.entry} | Stop: {setup.stop} | Target: {setup.target}\n"
            f"Grade: {setup.grade.value} | Score: {setup.score:.2f} | R:R: {setup.risk_reward:.1f}x\n"
            f"Session: {kz.session.upper()} | News: {news.status.value}\n"
            f"Confluence: {', '.join(setup.confluence_factors[:2])}"
        )
        _send_telegram(msg)

        return {
            "status": "executed",
            "trade_id": trade_id,
            "setup": setup.setup_type.value,
            "direction": setup.direction.value,
            "grade": setup.grade.value,
            "score": setup.score,
            "contracts": contracts,
            "order_id": order_id,
            "brokers": brokers_fired,
        }

    @app.get("/ict/status")
    async def get_ict_status():
        """Agent + risk dashboard data."""
        kz = check_killzone()
        risk = get_risk_dashboard()
        news = check_news(ALPHA_VANTAGE_KEY) if True else None
        return {
            "killzone": {
                "status": kz.status.value,
                "session": kz.session,
                "reason": kz.reason,
                "minutes_to_next": kz.minutes_to_next,
                "schedule": get_session_schedule(),
            },
            "risk": risk,
            "guard": get_guard_dashboard(),
            "session_rules": get_session_dashboard(),
            "monthly": get_monthly_progress(),
            "news": {
                "status": news.status.value if news else "UNKNOWN",
                "reason": news.reason if news else "",
                "headlines": news.headlines[:3] if news else [],
            },
            "setup_weights": get_setup_weights(),
            "tradovate_connected": _TRADOVATE_ENABLED,
            "rithmic_connected": _RITHMIC_ENABLED,
        }

    @app.get("/ict/signals")
    async def get_ict_signals(limit: int = 50):
        """Recent signal log (all signals, including rejected)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT signal_uuid, received_at, payload, killzone_status, scanner_grade,
                       scanner_score, risk_status, news_status, final_decision, rejection_reason
                FROM ict_signals
                ORDER BY received_at DESC
                LIMIT ?
            """, (limit,))
            cols = [d[0] for d in cursor.description]
            rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        return {"signals": rows}

    @app.get("/ict/trades")
    async def get_ict_trades(limit: int = 100, status: str = ""):
        """Trade history."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute("""
                    SELECT * FROM ict_trades WHERE status = ? ORDER BY opened_at DESC LIMIT ?
                """, (status, limit))
            else:
                cursor.execute("SELECT * FROM ict_trades ORDER BY opened_at DESC LIMIT ?", (limit,))
            cols = [d[0] for d in cursor.description]
            rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        return {"trades": rows}

    @app.post("/ict/backtest")
    async def run_backtest_endpoint(req: BacktestRequest):
        """Run a backtest and return results."""
        from backtesting import run_backtest
        result = run_backtest(
            instrument=req.instrument,
            start_date=req.start_date,
            end_date=req.end_date,
            setups=req.setups,
            killzone_filter=req.killzone_filter,
            min_grade=req.min_grade,
        )
        return {
            "instrument": result.instrument,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "total_trades": result.total_trades,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": round(result.win_rate * 100, 1),
            "avg_rr": result.avg_rr,
            "total_pnl": result.total_pnl,
            "max_drawdown": result.max_drawdown,
            "sharpe_ratio": result.sharpe_ratio,
            "by_setup": result.by_setup,
            "by_session": result.by_session,
            "trades": [
                {
                    "entry_time": t.entry_time,
                    "exit_time": t.exit_time,
                    "setup_type": t.setup_type,
                    "direction": t.direction,
                    "instrument": t.instrument,
                    "entry": t.entry,
                    "exit_price": t.exit_price,
                    "pnl": t.pnl,
                    "outcome": t.outcome,
                    "rr": t.rr,
                    "grade": t.grade,
                    "session": t.session,
                }
                for t in result.trades
            ],
        }

    @app.get("/ict/weights")
    async def get_weights():
        return get_setup_weights()

    @app.post("/ict/weights")
    async def update_weights(weights: WeightsUpdate):
        new_weights = {
            "FVG": weights.FVG,
            "IFVG": weights.IFVG,
            "ORDER_BLOCK": weights.ORDER_BLOCK,
            "LIQUIDITY_GRAB": weights.LIQUIDITY_GRAB,
            "SMT_DIVERGENCE": weights.SMT_DIVERGENCE,
        }
        load_setup_weights(new_weights)
        return {"status": "updated", "weights": new_weights}

    @app.post("/ict/close")
    async def close_position(symbol: str):
        """Manually close an open position."""
        if not _TRADOVATE_ENABLED:
            raise HTTPException(status_code=400, detail="Tradovate not configured")
        result = tvb.close_position(symbol)
        return {"success": result.success, "message": result.message}

    logger.info("ICT routes registered")
