from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Any, List

from apge.market_regime import Candle
from apge.simulator import OrderState


@dataclass
class PaperOrder:
    order_id: int
    symbol: str
    client_order_id: str
    side: str
    quantity: Decimal
    price: Decimal
    status: str = "NEW"
    executed_qty: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")


class PaperAdapter:
    """Deterministic in-memory exchange adapter for forward paper validation.

    It intentionally mirrors only the subset of the BinanceAdapter contract used
    by ExecutionEngine/Reconciler. It never performs network I/O and never uses
    real credentials. Fills are generated from closed OHLC candles. If both sides
    are touched in one candle, a conservative deterministic rule allows only one
    side to fill, avoiding synthetic same-bar round-trip profit.
    """

    def __init__(self, symbol: str = "BTCUSDT", starting_position: Decimal = Decimal("0")):
        if not isinstance(starting_position, Decimal) or starting_position.is_nan() or starting_position.is_infinite():
            raise ValueError("starting_position must be a finite Decimal")
        self.symbol = symbol
        self.position = starting_position
        self.orders: Dict[str, PaperOrder] = {}
        self._next_order_id = 1
        self._next_trade_id = 1

    @staticmethod
    def _map_order_state(status: str) -> OrderState:
        mapping = {
            "NEW": OrderState.OPEN,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
        }
        return mapping.get(str(status), OrderState.UNKNOWN)

    @staticmethod
    def _as_exchange_order(order: PaperOrder) -> Dict[str, Any]:
        return {
            "symbol": order.symbol,
            "orderId": order.order_id,
            "clientOrderId": order.client_order_id,
            "side": order.side,
            "price": str(order.price),
            "origQty": str(order.quantity),
            "executedQty": str(order.executed_qty),
            "avgPrice": str(order.average_price),
            "status": order.status,
            "type": "LIMIT",
            "timeInForce": "GTC",
        }

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
        time_in_force: str = "GTC",
    ) -> Dict[str, Any]:
        if symbol != self.symbol:
            raise ValueError("paper adapter symbol mismatch")
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("invalid side")
        if time_in_force != "GTC":
            raise ValueError("paper adapter supports GTC only")
        if client_order_id in self.orders:
            raise ValueError("duplicate client order id")
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")

        order = PaperOrder(
            order_id=self._next_order_id,
            symbol=symbol,
            client_order_id=client_order_id,
            side=side,
            quantity=quantity,
            price=price,
        )
        self._next_order_id += 1
        self.orders[client_order_id] = order
        return self._as_exchange_order(order)

    def cancel_order(self, symbol: str, orig_client_order_id: str) -> Dict[str, Any]:
        if symbol != self.symbol:
            raise ValueError("paper adapter symbol mismatch")
        order = self.orders.get(orig_client_order_id)
        if order is None:
            raise ValueError("paper order does not exist")
        if order.status in ("FILLED", "CANCELED"):
            return self._as_exchange_order(order)
        order.status = "CANCELED"
        return self._as_exchange_order(order)

    def query_order(self, symbol: str, orig_client_order_id: str) -> Dict[str, Any]:
        if symbol != self.symbol:
            raise ValueError("paper adapter symbol mismatch")
        order = self.orders.get(orig_client_order_id)
        if order is None:
            raise ValueError("paper order does not exist")
        return self._as_exchange_order(order)

    def get_open_orders(self, symbol: str | None = None) -> List[Dict[str, Any]]:
        if symbol is not None and symbol != self.symbol:
            return []
        return [
            self._as_exchange_order(order)
            for order in self.orders.values()
            if order.status in ("NEW", "PARTIALLY_FILLED")
        ]

    def get_positions(self) -> List[Dict[str, Any]]:
        return [{
            "symbol": self.symbol,
            "positionAmt": str(self.position),
            "entryPrice": "0",
        }]

    def _touched(self, order: PaperOrder, candle: Candle) -> bool:
        if order.side == "BUY":
            return candle.low <= order.price
        return candle.high >= order.price

    def process_candle(self, candle: Candle) -> List[Dict[str, Any]]:
        values = (candle.open, candle.high, candle.low, candle.close)
        if any(v <= 0 or v.is_nan() or v.is_infinite() for v in values):
            raise ValueError("paper candle must contain finite positive prices")
        if candle.low > candle.high or not (candle.low <= candle.open <= candle.high) or not (candle.low <= candle.close <= candle.high):
            raise ValueError("invalid paper candle geometry")

        touched = [
            order for order in self.orders.values()
            if order.status == "NEW" and self._touched(order, candle)
        ]
        buys = [order for order in touched if order.side == "BUY"]
        sells = [order for order in touched if order.side == "SELL"]

        if buys and sells:
            if self.position > 0:
                selected_side = "BUY"
            elif self.position < 0:
                selected_side = "SELL"
            elif candle.close > candle.open:
                selected_side = "SELL"
            else:
                selected_side = "BUY"
            touched = [order for order in touched if order.side == selected_side]

        events: List[Dict[str, Any]] = []
        for order in sorted(touched, key=lambda item: item.client_order_id):
            order.executed_qty = order.quantity
            order.average_price = order.price
            order.status = "FILLED"
            if order.side == "BUY":
                self.position += order.quantity
            else:
                self.position -= order.quantity

            trade_id = f"paper-{self._next_trade_id}"
            self._next_trade_id += 1
            events.append({
                "symbol": order.symbol,
                "client_order_id": order.client_order_id,
                "side": order.side,
                "order_type": "LIMIT",
                "time_in_force": "GTC",
                "original_qty": order.quantity,
                "original_price": order.price,
                "average_price": order.average_price,
                "execution_type": "TRADE",
                "order_status": "FILLED",
                "mapped_state": OrderState.FILLED,
                "last_filled_qty": order.quantity,
                "accumulated_filled_qty": order.quantity,
                "last_filled_price": order.price,
                "trade_id": trade_id,
            })
        return events
