"""
Tradovate Bridge — REST API adapter for TopStep / Lucid execution.

Handles auth token refresh, order placement, position queries, and P&L.
Supports both live (md.tradovate.com) and demo (demo.tradovate.com) environments.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ==================== Config ====================
TRADOVATE_ENV = os.getenv("TRADOVATE_ENV", "demo")  # "demo" or "live"
TRADOVATE_USER = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASS = os.getenv("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID = os.getenv("TRADOVATE_APP_ID", "Sample App")
TRADOVATE_APP_VERSION = os.getenv("TRADOVATE_APP_VERSION", "1.0.0")
TRADOVATE_CID = os.getenv("TRADOVATE_CID", "")       # Client ID (from Tradovate API settings)
TRADOVATE_SEC = os.getenv("TRADOVATE_SECRET", "")    # Client secret

_BASE_URLS = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}

# Token cache
_access_token: Optional[str] = None
_token_expires_at: float = 0.0
_account_id: Optional[int] = None
_account_spec: Optional[str] = None


class OrderAction(str, Enum):
    BUY = "Buy"
    SELL = "Sell"


class OrderType(str, Enum):
    MARKET = "Market"
    LIMIT = "Limit"
    STOP = "Stop"
    STOP_LIMIT = "StopLimit"


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[int]
    message: str
    raw: dict


@dataclass
class AccountInfo:
    account_id: int
    account_spec: str
    balance: float
    daily_realized_pnl: float
    open_pnl: float


def _base_url() -> str:
    return _BASE_URLS.get(TRADOVATE_ENV, _BASE_URLS["demo"])


def _is_token_valid() -> bool:
    return _access_token is not None and time.time() < _token_expires_at - 60


def authenticate() -> bool:
    """Authenticate and cache the access token. Returns True on success."""
    global _access_token, _token_expires_at, _account_id, _account_spec

    if not TRADOVATE_USER or not TRADOVATE_PASS:
        logger.error("Tradovate: TRADOVATE_USERNAME and TRADOVATE_PASSWORD must be set in .env")
        return False

    url = f"{_base_url()}/auth/accesstokenrequest"
    payload = {
        "name": TRADOVATE_USER,
        "password": TRADOVATE_PASS,
        "appId": TRADOVATE_APP_ID,
        "appVersion": TRADOVATE_APP_VERSION,
        "cid": int(TRADOVATE_CID) if TRADOVATE_CID else 0,
        "sec": TRADOVATE_SEC,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()

        if "errorText" in data:
            logger.error("Tradovate auth failed: %s", data["errorText"])
            return False

        _access_token = data.get("accessToken")
        expiry_str = data.get("expirationTime")
        if expiry_str:
            try:
                expiry_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                _token_expires_at = expiry_dt.timestamp()
            except ValueError:
                _token_expires_at = time.time() + 4800  # 80 min default

        # Cache first account
        accounts = data.get("accounts", [])
        if accounts:
            acc = accounts[0]
            _account_id = acc.get("id")
            _account_spec = acc.get("name")
            logger.info(
                "Tradovate authenticated: env=%s account=%s id=%s",
                TRADOVATE_ENV, _account_spec, _account_id,
            )

        return bool(_access_token)

    except Exception as e:
        logger.error("Tradovate auth exception: %s", e)
        return False


def _ensure_auth() -> bool:
    if _is_token_valid():
        return True
    return authenticate()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_access_token}",
        "Content-Type": "application/json",
    }


def get_account_info() -> Optional[AccountInfo]:
    """Fetch account balance and P&L."""
    if not _ensure_auth():
        return None

    try:
        url = f"{_base_url()}/cashbalance/getcashbalancesnapshot"
        resp = requests.post(url, json={"accountId": _account_id}, headers=_headers(), timeout=8)
        data = resp.json()

        return AccountInfo(
            account_id=_account_id,
            account_spec=_account_spec or "",
            balance=data.get("totalCashValue", 0.0),
            daily_realized_pnl=data.get("realizedPnL", 0.0),
            open_pnl=data.get("openPnL", 0.0),
        )
    except Exception as e:
        logger.error("Tradovate account info failed: %s", e)
        return None


def get_daily_pnl() -> float:
    """Returns today's realized P&L (negative = loss)."""
    info = get_account_info()
    return info.daily_realized_pnl if info else 0.0


def place_order(
    symbol: str,
    action: OrderAction,
    quantity: int = 1,
    order_type: OrderType = OrderType.MARKET,
    price: Optional[float] = None,
    stop_price: Optional[float] = None,
    bracket_stop: Optional[float] = None,
    bracket_target: Optional[float] = None,
) -> OrderResult:
    """
    Place an order through Tradovate.
    For market orders, price can be None.
    bracket_stop and bracket_target create OCO exit orders automatically.
    """
    if not _ensure_auth():
        return OrderResult(success=False, order_id=None, message="Auth failed", raw={})

    if not _account_spec:
        return OrderResult(success=False, order_id=None, message="No account configured", raw={})

    url = f"{_base_url()}/order/placeorder"

    payload: dict = {
        "accountSpec": _account_spec,
        "accountId": _account_id,
        "action": action.value,
        "symbol": symbol,
        "orderQty": quantity,
        "orderType": order_type.value,
        "isAutomated": True,
    }

    if price is not None:
        payload["price"] = price
    if stop_price is not None:
        payload["stopPrice"] = stop_price

    # OCO bracket for auto stop-loss + take-profit
    if bracket_stop is not None and bracket_target is not None:
        payload["bracket1"] = {
            "action": "Sell" if action == OrderAction.BUY else "Buy",
            "orderType": "Stop",
            "stopPrice": bracket_stop,
        }
        payload["bracket2"] = {
            "action": "Sell" if action == OrderAction.BUY else "Buy",
            "orderType": "Limit",
            "price": bracket_target,
        }

    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=10)
        data = resp.json()

        if resp.status_code != 200 or "failureReason" in data:
            msg = data.get("failureReason") or data.get("errorText") or "Unknown error"
            logger.error("Tradovate order failed: %s — payload: %s", msg, payload)
            return OrderResult(success=False, order_id=None, message=msg, raw=data)

        order_id = data.get("orderId") or (data.get("orderVersion", {}) or {}).get("orderId")
        logger.info(
            "Tradovate order placed: %s %s %s x%d — order_id=%s",
            TRADOVATE_ENV, action.value, symbol, quantity, order_id,
        )
        return OrderResult(success=True, order_id=order_id, message="Order placed", raw=data)

    except Exception as e:
        logger.error("Tradovate order exception: %s", e)
        return OrderResult(success=False, order_id=None, message=str(e), raw={})


def get_open_positions() -> list[dict]:
    """Returns list of open positions from Tradovate."""
    if not _ensure_auth():
        return []

    try:
        url = f"{_base_url()}/position/list"
        resp = requests.get(url, headers=_headers(), timeout=8)
        positions = resp.json()
        if isinstance(positions, list):
            return [p for p in positions if p.get("netPos", 0) != 0]
        return []
    except Exception as e:
        logger.error("Tradovate positions failed: %s", e)
        return []


def close_position(symbol: str) -> OrderResult:
    """Flatten an open position at market."""
    positions = get_open_positions()
    for pos in positions:
        if pos.get("contractId") and symbol.upper() in (pos.get("symbol") or "").upper():
            net = pos.get("netPos", 0)
            if net == 0:
                continue
            action = OrderAction.SELL if net > 0 else OrderAction.BUY
            return place_order(symbol, action, abs(net))

    return OrderResult(success=False, order_id=None, message=f"No open position found for {symbol}", raw={})


def is_configured() -> bool:
    """Returns True if Tradovate credentials are present."""
    return bool(TRADOVATE_USER and TRADOVATE_PASS)
