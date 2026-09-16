import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, Optional, Protocol

from apge.simulator import OrderState


class Transport(Protocol):
    """Protocol for mockable HTTP/WS interactions."""

    def get(self, url: str, params: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        ...

    def post(self, url: str, data: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        ...

    def delete(self, url: str, params: Optional[Dict[str, Any]] = None,
               headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        ...


class BinanceAdapter:
    """Binance USD-M Futures TESTNET exchange adapter.

    The adapter refuses non-TESTNET hosts by construction. V1 order placement is
    LIMIT-only; market orders and leverage/margin-mode mutation are intentionally
    absent from this interface.
    """

    def __init__(self, transport: Transport, http_url: str, ws_url: str,
                 api_key: str, api_secret: str, clock=None):
        parsed_http = urllib.parse.urlparse(http_url)
        parsed_ws = urllib.parse.urlparse(ws_url)

        if parsed_http.scheme != "https":
            raise ValueError(f"Invalid HTTP URL scheme: {http_url}. Must be https.")
        if parsed_ws.scheme != "wss":
            raise ValueError(f"Invalid WS URL scheme: {ws_url}. Must be wss.")
        if parsed_http.username or parsed_http.password or parsed_ws.username or parsed_ws.password:
            raise ValueError("URL userinfo spoofing is forbidden.")
        if parsed_http.hostname != "testnet.binancefuture.com":
            raise ValueError(f"Invalid HTTP URL: {http_url}. TESTNET ONLY.")
        if parsed_ws.hostname != "fstream.binancefuture.com":
            raise ValueError(f"Invalid WS URL: {ws_url}. TESTNET ONLY.")

        self.transport = transport
        self.http_url = http_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.clock = clock or time.time
        self.server_time_offset = 0

    def _generate_signature(self, query_string: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _get_headers(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self.api_key}

    def _prepare_signed_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params_copy = dict(params)
        if "timestamp" not in params_copy:
            params_copy["timestamp"] = int(self.clock() * 1000) + int(self.server_time_offset)
        query_params = [
            (k, str(v)) for k, v in sorted(params_copy.items()) if v is not None
        ]
        query_string = urllib.parse.urlencode(query_params, safe="")
        signature = self._generate_signature(query_string)
        signed_params = dict(query_params)
        signed_params["signature"] = signature
        return signed_params

    def get_server_time(self) -> Dict[str, Any]:
        return self.transport.get(f"{self.http_url}/fapi/v1/time")

    def get_exchange_info(self) -> Dict[str, Any]:
        return self.transport.get(f"{self.http_url}/fapi/v1/exchangeInfo")

    def get_positions(self) -> Dict[str, Any]:
        url = f"{self.http_url}/fapi/v2/positionRisk"
        params = self._prepare_signed_params({})
        return self.transport.get(url, params=params, headers=self._get_headers())

    def get_position_mode(self) -> Dict[str, Any]:
        """Return Binance dual-side position mode state.

        APGE V1 requires one-way mode for unambiguous reduce-only semantics.
        """
        url = f"{self.http_url}/fapi/v1/positionSide/dual"
        params = self._prepare_signed_params({})
        return self.transport.get(url, params=params, headers=self._get_headers())

    def get_open_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        url = f"{self.http_url}/fapi/v1/openOrders"
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        params = self._prepare_signed_params(params)
        return self.transport.get(url, params=params, headers=self._get_headers())

    def query_order(self, symbol: str, orig_client_order_id: str) -> Dict[str, Any]:
        url = f"{self.http_url}/fapi/v1/order"
        params = self._prepare_signed_params({
            "symbol": symbol,
            "origClientOrderId": orig_client_order_id,
        })
        return self.transport.get(url, params=params, headers=self._get_headers())

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """Submit a LIMIT order, returning UNKNOWN on transport ambiguity."""
        url = f"{self.http_url}/fapi/v1/order"
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "quantity": str(quantity),
            "price": str(price),
            "newClientOrderId": client_order_id,
            "timeInForce": time_in_force,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        params = self._prepare_signed_params(params)

        try:
            return self.transport.post(url, data=params, headers=self._get_headers())
        except (TimeoutError, ConnectionError):
            return {"clientOrderId": client_order_id, "status": "UNKNOWN"}

    def cancel_order(self, symbol: str, orig_client_order_id: str) -> Dict[str, Any]:
        url = f"{self.http_url}/fapi/v1/order"
        params = self._prepare_signed_params({
            "symbol": symbol,
            "origClientOrderId": orig_client_order_id,
        })
        return self.transport.delete(url, params=params, headers=self._get_headers())

    def _map_order_state(self, status: str) -> OrderState:
        mapping = {
            "NEW": OrderState.OPEN,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
        }
        return mapping.get(status, OrderState.UNKNOWN)

    def parse_book_ticker(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": payload["s"],
            "bid_price": Decimal(str(payload["b"])),
            "bid_qty": Decimal(str(payload["B"])),
            "ask_price": Decimal(str(payload["a"])),
            "ask_qty": Decimal(str(payload["A"])),
            "update_id": payload["u"],
        }

    def parse_order_trade_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        order_info = payload["o"]
        return {
            "symbol": order_info["s"],
            "client_order_id": order_info["c"],
            "side": order_info["S"],
            "order_type": order_info["o"],
            "time_in_force": order_info["f"],
            "original_qty": Decimal(str(order_info["q"])),
            "original_price": Decimal(str(order_info["p"])),
            "average_price": Decimal(str(order_info["ap"])),
            "execution_type": order_info["x"],
            "order_status": order_info["X"],
            "mapped_state": self._map_order_state(order_info["X"]),
            "last_filled_qty": Decimal(str(order_info["l"])),
            "accumulated_filled_qty": Decimal(str(order_info["z"])),
            "last_filled_price": Decimal(str(order_info["L"])),
            "trade_id": str(order_info.get("t", "")),
            "reduce_only": bool(order_info.get("R", False)),
        }

    def parse_account_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        update_data = payload["a"]
        balances = []
        for b in update_data.get("B", []):
            balances.append({
                "asset": b["a"],
                "wallet_balance": Decimal(str(b["wb"])),
                "cross_wallet_balance": Decimal(str(b["cw"])),
            })

        positions = []
        for p in update_data.get("P", []):
            positions.append({
                "symbol": p["s"],
                "position_amount": Decimal(str(p["pa"])),
                "entry_price": Decimal(str(p["ep"])),
                "unrealized_pnl": Decimal(str(p["up"])),
                "margin_type": p["mt"],
            })

        return {
            "reason": update_data.get("m"),
            "balances": balances,
            "positions": positions,
        }
