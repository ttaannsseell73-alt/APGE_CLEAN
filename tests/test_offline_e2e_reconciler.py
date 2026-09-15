import pytest
from decimal import Decimal

from apge.persistence import Persistence
from apge.reconciliation import Reconciler
from apge.binance_adapter import BinanceAdapter
from apge.simulator import OrderState

class MockAdapter:
    def __init__(self, positions=None, open_orders=None, query_orders=None):
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.query_orders = query_orders or {}

    def get_positions(self):
        return self.positions

    def get_open_orders(self, symbol=None):
        return self.open_orders

    def query_order(self, symbol, orig_client_order_id):
        if orig_client_order_id in self.query_orders:
            if isinstance(self.query_orders[orig_client_order_id], Exception):
                raise self.query_orders[orig_client_order_id]
            return self.query_orders[orig_client_order_id]
        raise Exception("Order does not exist")

    def _map_order_state(self, status):
        mapping = {
            "NEW": OrderState.OPEN,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
        }
        return mapping.get(status, OrderState.UNKNOWN)

def test_startup_reconciliation_clean():
    db = Persistence()
    adapter = MockAdapter()
    reconciler = Reconciler(db, adapter)

    assert reconciler.resolve_state("BTCUSDT") == True
    db.close()

def test_reconciliation_local_only_open():
    db = Persistence()
    db.save_intent("1", "BTCUSDT", "BUY", Decimal("1.0"), Decimal("100"), OrderState.OPEN)

    # Local says OPEN, exchange says CANCELED (-2013 or doesn't exist)
    adapter = MockAdapter(query_orders={"1": {"status": "CANCELED", "clientOrderId": "1", "executedQty": "0", "orderId": "ext1"}})
    reconciler = Reconciler(db, adapter)

    assert reconciler.resolve_state("BTCUSDT") == True
    intent = db.get_intent("1")
    assert intent["status"] == "CANCELED"
    db.close()

def test_reconciliation_exchange_only_order_halts():
    db = Persistence()
    # Empty DB, but exchange has an open order
    adapter = MockAdapter(open_orders=[{"clientOrderId": "ext_only", "status": "NEW", "executedQty": "0"}])
    reconciler = Reconciler(db, adapter)

    assert reconciler.resolve_state("BTCUSDT") == False # Should halt to prevent taking over unknown state
    db.close()

def test_reconciliation_corrupt_state_halts():
    db = Persistence()
    db.save_intent("1", "BTCUSDT", "BUY", Decimal("1.0"), Decimal("100"), OrderState.OPEN)

    # Exchange returns completely unexpected error
    adapter = MockAdapter(query_orders={"1": Exception("API Gateway Error 502")})
    reconciler = Reconciler(db, adapter)

    assert reconciler.resolve_state("BTCUSDT") == False
    db.close()
