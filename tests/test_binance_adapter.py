import pytest
from decimal import Decimal
from typing import Any, Dict, Optional

from apge.binance_adapter import BinanceAdapter
from apge.simulator import OrderState


class MockTransport:
    def __init__(self):
        self.requests = []
        self.responses = []
        self.should_timeout = False

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        self.requests.append(("GET", url, params, headers))
        if self.should_timeout:
            raise TimeoutError("Mock timeout")
        return self.responses.pop(0) if self.responses else {}

    def post(self, url: str, data: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        self.requests.append(("POST", url, data, headers))
        if self.should_timeout:
            raise TimeoutError("Mock timeout")
        return self.responses.pop(0) if self.responses else {}

    def delete(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        self.requests.append(("DELETE", url, params, headers))
        if self.should_timeout:
            raise TimeoutError("Mock timeout")
        return self.responses.pop(0) if self.responses else {}


def test_rejects_production_urls():
    transport = MockTransport()

    with pytest.raises(ValueError, match="Production URLs are strictly forbidden"):
        BinanceAdapter(
            transport,
            http_url="https://fapi.binance.com",
            ws_url="wss://stream.binancefuture.com",
            api_key="test",
            api_secret="test"
        )

    with pytest.raises(ValueError, match="Production URLs are strictly forbidden"):
        BinanceAdapter(
            transport,
            http_url="https://testnet.binancefuture.com",
            ws_url="wss://fapi.binance.com",
            api_key="test",
            api_secret="test"
        )

def test_deterministic_signature():
    transport = MockTransport()
    adapter = BinanceAdapter(
        transport,
        http_url="https://testnet.binancefuture.com",
        ws_url="wss://stream.binancefuture.com",
        api_key="test_key",
        api_secret="test_secret"
    )

    params = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "timestamp": 1600000000000
    }

    signed = adapter._prepare_signed_params(params)
    assert "signature" in signed
    assert str(signed["timestamp"]) == "1600000000000"
    assert len(signed["signature"]) == 64

def _setup_adapter(transport: MockTransport) -> BinanceAdapter:
    return BinanceAdapter(
        transport,
        http_url="https://testnet.binancefuture.com",
        ws_url="wss://stream.binancefuture.com",
        api_key="test_key",
        api_secret="test_secret"
    )

def test_get_server_time():
    transport = MockTransport()
    adapter = _setup_adapter(transport)

    transport.responses.append({"serverTime": 123456789})
    res = adapter.get_server_time()

    assert res == {"serverTime": 123456789}
    assert transport.requests[0][0] == "GET"
    assert transport.requests[0][1] == "https://testnet.binancefuture.com/fapi/v1/time"

def test_get_exchange_info():
    transport = MockTransport()
    adapter = _setup_adapter(transport)

    transport.responses.append({"symbols": [{"symbol": "BTCUSDT"}]})
    res = adapter.get_exchange_info()

    assert res["symbols"][0]["symbol"] == "BTCUSDT"
    assert transport.requests[0][0] == "GET"
    assert transport.requests[0][1] == "https://testnet.binancefuture.com/fapi/v1/exchangeInfo"

def test_get_positions():
    transport = MockTransport()
    adapter = _setup_adapter(transport)

    transport.responses.append([{"symbol": "BTCUSDT", "positionAmt": "1.0"}])
    res = adapter.get_positions()

    assert len(res) == 1
    assert res[0]["symbol"] == "BTCUSDT"
    assert transport.requests[0][0] == "GET"
    assert transport.requests[0][1] == "https://testnet.binancefuture.com/fapi/v2/positionRisk"
    assert "signature" in transport.requests[0][2]
    assert transport.requests[0][3]["X-MBX-APIKEY"] == "test_key"

def test_get_open_orders():
    transport = MockTransport()
    adapter = _setup_adapter(transport)

    transport.responses.append([{"symbol": "BTCUSDT", "clientOrderId": "test_id"}])
    res = adapter.get_open_orders("BTCUSDT")

    assert len(res) == 1
    assert transport.requests[0][0] == "GET"
    assert transport.requests[0][1] == "https://testnet.binancefuture.com/fapi/v1/openOrders"
    assert transport.requests[0][2]["symbol"] == "BTCUSDT"
    assert "signature" in transport.requests[0][2]

def test_query_order():
    transport = MockTransport()
    adapter = _setup_adapter(transport)

    transport.responses.append({"symbol": "BTCUSDT", "status": "FILLED"})
    res = adapter.query_order("BTCUSDT", "test_client_id")

    assert res["status"] == "FILLED"
    assert transport.requests[0][0] == "GET"
    assert transport.requests[0][1] == "https://testnet.binancefuture.com/fapi/v1/order"
    assert transport.requests[0][2]["origClientOrderId"] == "test_client_id"

def test_submit_limit_order():
    transport = MockTransport()
    adapter = _setup_adapter(transport)

    transport.responses.append({"symbol": "BTCUSDT", "status": "NEW"})
    res = adapter.submit_limit_order("BTCUSDT", "BUY", Decimal("1.5"), Decimal("50000.0"), "client_id")

    assert res["status"] == "NEW"
    assert transport.requests[0][0] == "POST"
    assert transport.requests[0][1] == "https://testnet.binancefuture.com/fapi/v1/order"

    req_data = transport.requests[0][2]
    assert req_data["quantity"] == "1.5"
    assert req_data["price"] == "50000.0"
    assert req_data["type"] == "LIMIT"

def test_submit_order_timeout_returns_unknown():
    transport = MockTransport()
    transport.should_timeout = True
    adapter = _setup_adapter(transport)

    res = adapter.submit_limit_order("BTCUSDT", "BUY", Decimal("1.5"), Decimal("50000.0"), "client_id")

    assert res["clientOrderId"] == "client_id"
    assert res["status"] == "UNKNOWN"

def test_cancel_order():
    transport = MockTransport()
    adapter = _setup_adapter(transport)

    transport.responses.append({"symbol": "BTCUSDT", "status": "CANCELED"})
    res = adapter.cancel_order("BTCUSDT", "client_id")

    assert res["status"] == "CANCELED"
    assert transport.requests[0][0] == "DELETE"
    assert transport.requests[0][1] == "https://testnet.binancefuture.com/fapi/v1/order"
    assert transport.requests[0][2]["origClientOrderId"] == "client_id"

def test_parse_book_ticker():
    adapter = _setup_adapter(MockTransport())
    payload = {
        "u": 400900217,
        "s": "BNBUSDT",
        "b": "25.3519",
        "B": "31.21",
        "a": "25.3652",
        "A": "40.66"
    }

    parsed = adapter.parse_book_ticker(payload)
    assert parsed["symbol"] == "BNBUSDT"
    assert parsed["bid_price"] == Decimal("25.3519")
    assert parsed["bid_qty"] == Decimal("31.21")
    assert parsed["ask_price"] == Decimal("25.3652")
    assert parsed["ask_qty"] == Decimal("40.66")
    assert parsed["update_id"] == 400900217

def test_parse_order_trade_update():
    adapter = _setup_adapter(MockTransport())
    payload = {
        "e": "ORDER_TRADE_UPDATE",
        "T": 1568879465651,
        "E": 1568879465658,
        "o": {
            "s": "BTCUSDT",
            "c": "TEST",
            "S": "SELL",
            "o": "LIMIT",
            "f": "GTC",
            "q": "0.001",
            "p": "9900",
            "ap": "9900",
            "sp": "0",
            "x": "TRADE",
            "X": "FILLED",
            "i": 8886774,
            "l": "0.001",
            "z": "0.001",
            "L": "9900",
            "N": "USDT",
            "n": "0",
            "T": 1568879465651,
            "t": 29653,
            "b": "0",
            "a": "9.9",
            "m": False,
            "R": False,
            "wt": "CONTRACT_PRICE",
            "ot": "LIMIT",
            "ps": "BOTH",
            "cp": False,
            "rp": "0",
            "pP": False,
            "si": 0,
            "ss": 0
        }
    }

    parsed = adapter.parse_order_trade_update(payload)
    assert parsed["symbol"] == "BTCUSDT"
    assert parsed["client_order_id"] == "TEST"
    assert parsed["side"] == "SELL"
    assert parsed["original_qty"] == Decimal("0.001")
    assert parsed["original_price"] == Decimal("9900")
    assert parsed["average_price"] == Decimal("9900")
    assert parsed["mapped_state"] == OrderState.FILLED
    assert parsed["last_filled_qty"] == Decimal("0.001")
    assert parsed["accumulated_filled_qty"] == Decimal("0.001")
    assert parsed["last_filled_price"] == Decimal("9900")
    assert parsed["trade_id"] == "29653"

def test_parse_account_update():
    adapter = _setup_adapter(MockTransport())
    payload = {
        "e": "ACCOUNT_UPDATE",
        "T": 1564745749361,
        "E": 1564745749386,
        "a": {
            "m": "ORDER",
            "B": [
                {
                    "a": "USDT",
                    "wb": "122624.12345678",
                    "cw": "122624.12345678",
                    "bc": "50.12345678"
                }
            ],
            "P": [
                {
                    "s": "BTCUSDT",
                    "pa": "1.23",
                    "ep": "10000.0",
                    "cr": "200",
                    "up": "-10.0",
                    "mt": "isolated",
                    "iw": "0.00000000",
                    "ps": "BOTH",
                    "ma": "USDT"
                }
            ]
        }
    }

    parsed = adapter.parse_account_update(payload)
    assert parsed["reason"] == "ORDER"

    assert len(parsed["balances"]) == 1
    assert parsed["balances"][0]["asset"] == "USDT"
    assert parsed["balances"][0]["wallet_balance"] == Decimal("122624.12345678")
    assert parsed["balances"][0]["cross_wallet_balance"] == Decimal("122624.12345678")

    assert len(parsed["positions"]) == 1
    assert parsed["positions"][0]["symbol"] == "BTCUSDT"
    assert parsed["positions"][0]["position_amount"] == Decimal("1.23")
    assert parsed["positions"][0]["entry_price"] == Decimal("10000.0")
    assert parsed["positions"][0]["unrealized_pnl"] == Decimal("-10.0")
    assert parsed["positions"][0]["margin_type"] == "isolated"
