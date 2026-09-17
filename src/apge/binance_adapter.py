import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol

from apge.simulator import OrderState


class Transport(Protocol):
    """Protocol for mockable HTTP interactions."""

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
        ...

    def post(self, url: str, data: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        ...

    def put(self, url: str, data: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        ...

    def delete(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        ...


class BinanceAdapter:
    """Binance USD-M Futures TESTNET exchange adapter.

    The adapter is deliberately pinned to TESTNET hosts. It exposes no market
    order, leverage-change or margin-mode mutation method. Read-only market-data
    methods are used by the deterministic adaptive controller.
    """

    _KLINE_INTERVALS = {
        "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"
    }

    def __init__(self, transport: Transport, http_url: str, ws_url: str, api_key: str, api_secret: str, clock=None):
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

    def _require_api_key(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("Binance TESTNET API key is required")

    @staticmethod
    def _validate_listen_key(listen_key: str) -> str:
        if not isinstance(listen_key, str) or not listen_key.strip():
            raise ValueError("listenKey must be a non-empty string")
        if len(listen_key) > 256 or any(ch.isspace() for ch in listen_key):
            raise ValueError("listenKey is malformed")
        return listen_key

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not symbol or not symbol.isalnum():
            raise ValueError("symbol must be a non-empty alphanumeric string")
        return symbol.upper()

    def _prepare_signed_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params_copy = dict(params)
        if "timestamp" not in params_copy:
            params_copy["timestamp"] = int(self.clock() * 1000) + int(self.server_time_offset)
        query_params = [(k, str(v)) for k, v in sorted(params_copy.items()) if v is not None]
        query_string = urllib.parse.urlencode(query_params, safe="")
        signature = self._generate_signature(query_string)
        signed_params = dict(query_params)
        signed_params["signature"] = signature
        return signed_params

    def get_server_time(self) -> Dict[str, Any]:
        return self.transport.get(f"{self.http_url}/fapi/v1/time")

    def get_exchange_info(self) -> Dict[str, Any]:
        return self.transport.get(f"{self.http_url}/fapi/v1/exchangeInfo")

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 100) -> List[List[Any]]:
        """Fetch read-only USD-M kline rows from the TESTNET REST host."""
        symbol = self._validate_symbol(symbol)
        if interval not in self._KLINE_INTERVALS:
            raise ValueError("unsupported kline interval")
        if not isinstance(limit, int) or limit < 2 or limit > 1500:
            raise ValueError("kline limit must be in [2, 1500]")
        response = self.transport.get(
            f"{self.http_url}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(response, list):
            raise ValueError("invalid kline response")
        return response

    def get_premium_index(self, symbol: str) -> Dict[str, Any]:
        """Fetch read-only mark/index/funding snapshot for a USD-M perpetual."""
        symbol = self._validate_symbol(symbol)
        response = self.transport.get(
            f"{self.http_url}/fapi/v1/premiumIndex", params={"symbol": symbol}
        )
        if not isinstance(response, dict):
            raise ValueError("invalid premium index response")
        return response

    def get_positions(self) -> List[Dict[str, Any]]:
        params = self._prepare_signed_params({})
        return self.transport.get(
            f"{self.http_url}/fapi/v2/positionRisk", params=params, headers=self._get_headers()
        )

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        params = self._prepare_signed_params(params)
        return self.transport.get(
            f"{self.http_url}/fapi/v1/openOrders", params=params, headers=self._get_headers()
        )

    def query_order(self, symbol: str, orig_client_order_id: str) -> Dict[str, Any]:
        params = self._prepare_signed_params({
            "symbol": symbol,
            "origClientOrderId": orig_client_order_id,
        })
        return self.transport.get(
            f"{self.http_url}/fapi/v1/order", params=params, headers=self._get_headers()
        )

    def start_user_data_stream(self) -> str:
        """Create an authenticated TESTNET user-data listen key."""
        self._require_api_key()
        response = self.transport.post(
            f"{self.http_url}/fapi/v1/listenKey", data={}, headers=self._get_headers()
        )
        listen_key = response.get("listenKey") if isinstance(response, dict) else None
        if not isinstance(listen_key, str) or not listen_key.strip():
            raise RuntimeError("Binance TESTNET did not return a valid listenKey")
        return self._validate_listen_key(listen_key)

    def keepalive_user_data_stream(self, listen_key: str) -> None:
        self._require_api_key()
        listen_key = self._validate_listen_key(listen_key)
        self.transport.put(
            f"{self.http_url}/fapi/v1/listenKey",
            data={"listenKey": listen_key},
            headers=self._get_headers(),
        )

    def close_user_data_stream(self, listen_key: str) -> None:
        self._require_api_key()
        listen_key = self._validate_listen_key(listen_key)
        self.transport.delete(
            f"{self.http_url}/fapi/v1/listenKey",
            params={"listenKey": listen_key},
            headers=self._get_headers(),
        )

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
        time_in_force: str = "GTC",
    ) -> Dict[str, Any]:
        """Submit a LIMIT order, returning UNKNOWN on transport timeout/loss."""
        params = self._prepare_signed_params({
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "quantity": str(quantity),
            "price": str(price),
            "newClientOrderId": client_order_id,
            "timeInForce": time_in_force,
        })
        try:
            return self.transport.post(
                f"{self.http_url}/fapi/v1/order", data=params, headers=self._get_headers()
            )
        except (TimeoutError, ConnectionError):
            return {"clientOrderId": client_order_id, "status": "UNKNOWN"}

    def cancel_order(self, symbol: str, orig_client_order_id: str) -> Dict[str, Any]:
        params = self._prepare_signed_params({
            "symbol": symbol,
            "origClientOrderId": orig_client_order_id,
        })
        return self.transport.delete(
            f"{self.http_url}/fapi/v1/order", params=params, headers=self._get_headers()
        )

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
        }

    def parse_account_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        update_data = payload["a"]
        balances = []
        for balance in update_data.get("B", []):
            balances.append({
                "asset": balance["a"],
                "wallet_balance": Decimal(str(balance["wb"])),
                "cross_wallet_balance": Decimal(str(balance["cw"])),
            })

        positions = []
        for position in update_data.get("P", []):
            positions.append({
                "symbol": position["s"],
                "position_amount": Decimal(str(position["pa"])),
                "entry_price": Decimal(str(position["ep"])),
                "unrealized_pnl": Decimal(str(position["up"])),
                "margin_type": position["mt"],
            })

        return {
            "reason": update_data.get("m"),
            "balances": balances,
            "positions": positions,
        }
