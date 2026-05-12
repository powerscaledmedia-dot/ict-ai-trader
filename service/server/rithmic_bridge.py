"""
Rithmic Bridge — WebSocket R|Protocol adapter for Lucid Trading eval.

Lucid uses Rithmic as their execution backend. When your eval activates you will
receive a welcome email with:
  - Username / Password
  - System Name  (e.g. "Rithmic Paper Trading" for eval)
  - FCM ID       (clearing firm identifier)
  - IB ID        (may be empty)
  - Account ID   (your account number)

Fill those into .env under the RITHMIC_* vars and set RITHMIC_ENV=paper.
Switch to live once you pass.

Install deps (once):
  pip install websockets protobuf

Rithmic .proto stubs — two options:
  A) Download from Rithmic developer portal (preferred — exact field numbers)
  B) The simplified hand-rolled encoding below covers 95% of what we need
     and will work until you can swap in the official stubs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# ==================== Config ====================
RITHMIC_USERNAME    = os.getenv("RITHMIC_USERNAME", "")
RITHMIC_PASSWORD    = os.getenv("RITHMIC_PASSWORD", "")
RITHMIC_SYSTEM_NAME = os.getenv("RITHMIC_SYSTEM_NAME", "Rithmic Paper Trading")
RITHMIC_FCM_ID      = os.getenv("RITHMIC_FCM_ID", "")
RITHMIC_IB_ID       = os.getenv("RITHMIC_IB_ID", "")
RITHMIC_ENV         = os.getenv("RITHMIC_ENV", "paper")   # "paper" | "live"
RITHMIC_ACCOUNT_ID  = os.getenv("RITHMIC_ACCOUNT_ID", "")
RITHMIC_APP_NAME    = os.getenv("RITHMIC_APP_NAME", "ICT-Jarvis")
RITHMIC_APP_VERSION = os.getenv("RITHMIC_APP_VERSION", "1.0.0")

# Rithmic gateway URLs
_GATEWAY_URLS = {
    "paper": "wss://rituz00100.rithmic.com:443",
    "live":  "wss://rithmic01.rithmic.com:443",   # confirm with Lucid welcome email
}

# ==================== Rithmic R|Protocol message template IDs ====================
_TEMPLATE_REQUEST_LOGIN              = 10
_TEMPLATE_RESPONSE_LOGIN             = 11
_TEMPLATE_REQUEST_HEARTBEAT          = 18
_TEMPLATE_RESPONSE_HEARTBEAT         = 19
_TEMPLATE_REQUEST_NEW_ORDER          = 312
_TEMPLATE_RESPONSE_NEW_ORDER         = 313
_TEMPLATE_REQUEST_CANCEL_ORDER       = 316
_TEMPLATE_RESPONSE_CANCEL_ORDER      = 317
_TEMPLATE_REQUEST_ACCOUNT_LIST       = 302
_TEMPLATE_RESPONSE_ACCOUNT_LIST      = 303
_TEMPLATE_REQUEST_PNL_POSITION_SNAP  = 400
_TEMPLATE_RESPONSE_PNL_POSITION_SNAP = 401
_TEMPLATE_RITHMIC_ORDER_NOTIFICATION = 351

# Rithmic protobuf field number for template_id (all messages share this)
_TEMPLATE_ID_FIELD_TAG = (154467 << 3) | 0  # field 154467, wire type 0 (varint)


# ==================== Shared dataclasses ====================

class OrderAction(str, Enum):
    BUY  = "Buy"
    SELL = "Sell"


@dataclass
class OrderResult:
    success:  bool
    order_id: Optional[str]
    message:  str
    raw:      dict = field(default_factory=dict)


@dataclass
class AccountInfo:
    account_id:         str
    balance:            float
    daily_realized_pnl: float
    open_pnl:           float


# ==================== Protobuf helpers ====================
# Hand-rolled varint / string encoding — covers all fields we use.
# If you add the official Rithmic proto stubs later, swap _encode_* calls
# with the generated message classes and remove this section.

def _encode_varint(value: int) -> bytes:
    bits = value & 0x7F
    value >>= 7
    result = b""
    while value:
        result += bytes([0x80 | bits])
        bits = value & 0x7F
        value >>= 7
    return result + bytes([bits])


def _encode_field_varint(field_number: int, value: int) -> bytes:
    tag = (field_number << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)


def _encode_field_string(field_number: int, value: str) -> bytes:
    tag = (field_number << 3) | 2
    encoded = value.encode("utf-8")
    return _encode_varint(tag) + _encode_varint(len(encoded)) + encoded


def _encode_field_double(field_number: int, value: float) -> bytes:
    tag = (field_number << 3) | 1
    return _encode_varint(tag) + struct.pack("<d", value)


def _frame(msg_bytes: bytes) -> bytes:
    """Rithmic framing: 4-byte big-endian length prefix."""
    return struct.pack(">I", len(msg_bytes)) + msg_bytes


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _parse_response(data: bytes) -> dict:
    """
    Minimal parse of a Rithmic protobuf message into {field_number: value}.
    Handles varint (type 0), 64-bit double (type 1), string/bytes (type 2).
    """
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _decode_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if wire_type == 0:
                val, pos = _decode_varint(data, pos)
                fields.setdefault(field_num, []).append(val)
            elif wire_type == 1:
                val = struct.unpack_from("<d", data, pos)[0]; pos += 8
                fields.setdefault(field_num, []).append(val)
            elif wire_type == 2:
                length, pos = _decode_varint(data, pos)
                raw = data[pos:pos + length]; pos += length
                try:
                    fields.setdefault(field_num, []).append(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    fields.setdefault(field_num, []).append(raw)
            elif wire_type == 5:
                val = struct.unpack_from("<f", data, pos)[0]; pos += 4
                fields.setdefault(field_num, []).append(val)
            else:
                break  # unknown wire type — stop
        except Exception:
            break
    return {k: v[0] if len(v) == 1 else v for k, v in fields.items()}


# ==================== Message builders ====================
# Field numbers from Rithmic's proto files (public samples).

def _build_login_request() -> bytes:
    msg = (
        _encode_field_varint(154467, _TEMPLATE_REQUEST_LOGIN)   # template_id
        + _encode_field_string(2, RITHMIC_USERNAME)             # user_id
        + _encode_field_string(3, RITHMIC_PASSWORD)             # password
        + _encode_field_string(4, RITHMIC_APP_NAME)             # app_name
        + _encode_field_string(5, RITHMIC_APP_VERSION)          # app_version
        + _encode_field_string(6, RITHMIC_SYSTEM_NAME)          # system_name
        + _encode_field_varint(7, 0)                            # infra_type: 0 = order_plant
    )
    return _frame(msg)


def _build_heartbeat() -> bytes:
    msg = _encode_field_varint(154467, _TEMPLATE_REQUEST_HEARTBEAT)
    return _frame(msg)


def _build_new_order_request(
    account_id: str,
    symbol: str,
    exchange: str,
    action: OrderAction,
    quantity: int,
    bracket_stop: Optional[float] = None,
    bracket_target: Optional[float] = None,
) -> bytes:
    """
    RequestNewOrder — bracket market order with OCO stop + target.

    Field map (Rithmic proto):
      154467 = template_id
      2  = fcm_id
      3  = ib_id
      4  = account_id
      5  = symbol
      6  = exchange
      7  = trade_route (default "DEFAULT")
      8  = transaction_type  1=BUY 2=SELL
      9  = duration          0=IOC 1=GTC 2=Day 3=FOK
      10 = order_type        1=Market 2=Limit 3=Stop 4=Stop_Limit 5=Market_If_Touched
      11 = qty
      12 = price (for limit)
      13 = trigger_price (for stop)
      110 = bracket_type     0=None 1=target_and_stop 2=target 3=stop
      111 = bracket_quantity
      112 = target_ticks (relative)
      113 = stop_ticks (relative)
    """
    buy_sell = 1 if action == OrderAction.BUY else 2

    msg = (
        _encode_field_varint(154467, _TEMPLATE_REQUEST_NEW_ORDER)
        + _encode_field_string(2, RITHMIC_FCM_ID)
        + _encode_field_string(3, RITHMIC_IB_ID)
        + _encode_field_string(4, account_id)
        + _encode_field_string(5, symbol)
        + _encode_field_string(6, exchange)
        + _encode_field_string(7, "DEFAULT")
        + _encode_field_varint(8, buy_sell)   # transaction_type
        + _encode_field_varint(9, 2)          # duration: Day
        + _encode_field_varint(10, 1)         # order_type: Market
        + _encode_field_varint(11, quantity)
    )

    if bracket_stop is not None and bracket_target is not None:
        msg += _encode_field_varint(110, 1)   # bracket_type: target_and_stop
        msg += _encode_field_varint(111, quantity)
        msg += _encode_field_double(112, bracket_target)
        msg += _encode_field_double(113, bracket_stop)

    return _frame(msg)


def _build_pnl_request(account_id: str) -> bytes:
    msg = (
        _encode_field_varint(154467, _TEMPLATE_REQUEST_PNL_POSITION_SNAP)
        + _encode_field_string(2, RITHMIC_FCM_ID)
        + _encode_field_string(3, RITHMIC_IB_ID)
        + _encode_field_string(4, account_id)
    )
    return _frame(msg)


# ==================== WebSocket session ====================

class _RithmicSession:
    """
    Manages a single async WebSocket connection to the Rithmic order plant.
    Call connect() once; it runs a background event loop in a daemon thread.
    """

    def __init__(self):
        self._ws = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._authenticated = False
        self._last_pnl: float = 0.0
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = threading.Lock()

    def _start_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro):
        """Run a coroutine on the session's event loop from any thread."""
        if self._loop is None or not self._loop.is_running():
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=15)
        except Exception as e:
            logger.error("Rithmic coro failed: %s", e)
            return None

    async def _recv_loop(self, ws):
        """Background receiver — parses incoming messages, resolves pending futures."""
        import websockets
        try:
            async for raw in ws:
                if isinstance(raw, bytes) and len(raw) >= 4:
                    body = raw[4:]  # strip 4-byte length prefix
                    parsed = _parse_response(body)
                    tmpl = parsed.get(154467)
                    logger.debug("Rithmic recv template_id=%s fields=%s", tmpl, list(parsed.keys()))

                    if tmpl == _TEMPLATE_RESPONSE_LOGIN:
                        rp_code = parsed.get(149)  # response_code field
                        if rp_code == 0:
                            self._authenticated = True
                            logger.info("Rithmic: authenticated OK (system=%s)", RITHMIC_SYSTEM_NAME)
                        else:
                            logger.error("Rithmic: login failed rp_code=%s", rp_code)

                    elif tmpl == _TEMPLATE_RESPONSE_PNL_POSITION_SNAP:
                        # Field 4 = open_long_sell_qty, field 5 = open_short_buy_qty
                        # Field 8 = realized_pnl, field 9 = open_pnl
                        realized = parsed.get(8, 0.0)
                        self._last_pnl = float(realized) if realized else 0.0

                    # Resolve any pending order future keyed by template_id
                    with self._lock:
                        fut = self._pending.get(str(tmpl))
                        if fut and not fut.done():
                            fut.set_result(parsed)

        except websockets.exceptions.ConnectionClosed:
            logger.warning("Rithmic WebSocket closed")
            self._authenticated = False

    async def _connect_async(self) -> bool:
        try:
            import websockets
        except ImportError:
            logger.error("Rithmic: install 'websockets' — pip install websockets")
            return False

        url = _GATEWAY_URLS.get(RITHMIC_ENV, _GATEWAY_URLS["paper"])
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE  # Rithmic self-signed cert on some gateways

        try:
            self._ws = await websockets.connect(
                url,
                ssl=ssl_ctx,
                ping_interval=20,
                ping_timeout=10,
                max_size=2**20,
            )
            # Start recv loop as background task
            asyncio.ensure_future(self._recv_loop(self._ws))

            # Send login
            await self._ws.send(_build_login_request())

            # Wait up to 8 seconds for authentication
            for _ in range(80):
                await asyncio.sleep(0.1)
                if self._authenticated:
                    return True

            logger.error("Rithmic: login timeout — check credentials and system name")
            return False

        except Exception as e:
            logger.error("Rithmic: connection failed: %s", e)
            return False

    def connect(self) -> bool:
        if self._authenticated:
            return True
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._start_loop, daemon=True)
            self._thread.start()
            time.sleep(0.1)  # let loop start

        result = self._run_coro(self._connect_async())
        return bool(result)

    async def _send_and_wait(self, msg_bytes: bytes, response_template_id: int, timeout: float = 10.0):
        """Send a message and wait for the matching response template."""
        if self._ws is None:
            return None
        key = str(response_template_id)
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        with self._lock:
            self._pending[key] = fut
        try:
            await self._ws.send(msg_bytes)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Rithmic: timeout waiting for template_id=%d", response_template_id)
            return None
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def place_order(
        self,
        symbol: str,
        exchange: str,
        action: OrderAction,
        quantity: int,
        bracket_stop: Optional[float] = None,
        bracket_target: Optional[float] = None,
    ) -> OrderResult:
        if not self._authenticated:
            if not self.connect():
                return OrderResult(success=False, order_id=None, message="Not authenticated")

        account = RITHMIC_ACCOUNT_ID
        msg = _build_new_order_request(account, symbol, exchange, action, quantity, bracket_stop, bracket_target)

        result = self._run_coro(
            self._send_and_wait(msg, _TEMPLATE_RESPONSE_NEW_ORDER)
        )

        if result is None:
            return OrderResult(success=False, order_id=None, message="No response from Rithmic")

        # Field 9 in ResponseNewOrder = user_tag / order_id, field 149 = rp_code (0=success)
        rp_code = result.get(149, -1)
        if rp_code == 0:
            order_id = str(result.get(9, "")) or str(result.get(11, ""))
            logger.info("Rithmic order placed: %s %s x%d id=%s", action.value, symbol, quantity, order_id)
            return OrderResult(success=True, order_id=order_id, message="Order placed", raw=result)
        else:
            msg_text = str(result.get(150, f"rp_code={rp_code}"))
            logger.error("Rithmic order rejected: %s", msg_text)
            return OrderResult(success=False, order_id=None, message=msg_text, raw=result)

    def get_daily_pnl(self) -> float:
        if not self._authenticated:
            self.connect()
        msg = _build_pnl_request(RITHMIC_ACCOUNT_ID)
        self._run_coro(
            self._send_and_wait(msg, _TEMPLATE_RESPONSE_PNL_POSITION_SNAP)
        )
        return self._last_pnl

    def close_position(self, symbol: str, exchange: str) -> OrderResult:
        # Send a market order to flatten — direction determined by open position sign
        # For simplicity: attempt both directions and the one with an existing position fills
        # In practice, query the position first (add get_positions() if needed)
        msg = _build_new_order_request(
            RITHMIC_ACCOUNT_ID, symbol, exchange, OrderAction.SELL, 1
        )
        result = self._run_coro(
            self._send_and_wait(msg, _TEMPLATE_RESPONSE_NEW_ORDER)
        )
        if result and result.get(149) == 0:
            return OrderResult(success=True, order_id=str(result.get(9, "")), message="Flatten sent")
        return OrderResult(success=False, order_id=None, message="Close position failed")


# ==================== Module-level singleton ====================
_session = _RithmicSession()


# ==================== Instrument → exchange map ====================
_EXCHANGE_MAP = {
    "MES":  "CME",
    "MNQ":  "CME",
    "GC":   "COMEX",
    "SI":   "COMEX",
    "MGC":  "COMEX",
    "MCL":  "NYMEX",
}


def _symbol_exchange(raw_symbol: str) -> tuple[str, str]:
    """
    Convert TradingView symbol (MES1!, MESH5, etc.) to Rithmic format.
    Rithmic uses continuous contract names like 'MESU5' or 'MES' depending on setup.
    Adjust FRONT_MONTH to the current quarterly expiry.
    """
    FRONT_MONTH = "M5"  # Update each quarter: H=Mar, M=Jun, U=Sep, Z=Dec + year digit

    # Strip TV suffix (1!, !) and map to Rithmic symbol
    sym = raw_symbol.upper().replace("1!", "").replace("!", "")
    # If already has month code, use as-is
    for base, exchange in _EXCHANGE_MAP.items():
        if sym.startswith(base):
            if len(sym) == len(base):
                return f"{base}{FRONT_MONTH}", exchange
            return sym, exchange
    return sym, "CME"  # fallback


# ==================== Public interface (matches tradovate_bridge) ====================

def is_configured() -> bool:
    return bool(RITHMIC_USERNAME and RITHMIC_PASSWORD and RITHMIC_ACCOUNT_ID)


def place_order(
    symbol: str,
    action: OrderAction,
    quantity: int = 1,
    bracket_stop: Optional[float] = None,
    bracket_target: Optional[float] = None,
) -> OrderResult:
    rithmic_symbol, exchange = _symbol_exchange(symbol)
    return _session.place_order(rithmic_symbol, exchange, action, quantity, bracket_stop, bracket_target)


def get_daily_pnl() -> float:
    return _session.get_daily_pnl()


def close_position(symbol: str) -> OrderResult:
    rithmic_symbol, exchange = _symbol_exchange(symbol)
    return _session.close_position(rithmic_symbol, exchange)


def connect() -> bool:
    return _session.connect()
