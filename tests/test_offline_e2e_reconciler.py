import pytest
from decimal import Decimal

from apge.persistence import Persistence
from apge.reconciliation import Reconciler
from apge.binance_adapter import BinanceAdapter
from apge.simulator import OrderState, RiskEngine, SystemState

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



def test_reconciliation_rebuilds_risk_from_exchange_position_and_open_order():
    db = Persistence()
    db.save_intent("1", "BTCUSDT", "SELL", Decimal("2"), Decimal("100"), OrderState.OPEN)
    adapter = MockAdapter(
        positions=[{"symbol": "BTCUSDT", "positionAmt": "3", "entryPrice": "100"}],
        open_orders=[{"clientOrderId": "1", "status": "NEW", "executedQty": "0", "orderId": "ext1"}],
    )
    risk = RiskEngine(Decimal("10"), require_explicit_side=True)
    risk.system_state = SystemState.RECONCILING
    reconciler = Reconciler(db, adapter, risk)
    try:
        assert reconciler.resolve_state("BTCUSDT") is True
        assert reconciler.last_position_amount == Decimal("3")
        assert risk.current_position == Decimal("3")
        assert risk.reservations == Decimal("2")
        assert risk.orders["1"].side == "SELL"
        assert risk.orders["1"].state == OrderState.OPEN
        risk.complete_reconciliation()
        assert risk.system_state == SystemState.OPERATIONAL
    finally:
        db.close()


def test_reconciliation_rejects_snapshot_that_can_exceed_signed_limit():
    db = Persistence()
    db.save_intent("1", "BTCUSDT", "BUY", Decimal("2"), Decimal("100"), OrderState.OPEN)
    adapter = MockAdapter(
        positions=[{"symbol": "BTCUSDT", "positionAmt": "9", "entryPrice": "100"}],
        open_orders=[{"clientOrderId": "1", "status": "NEW", "executedQty": "0", "orderId": "ext1"}],
    )
    risk = RiskEngine(Decimal("10"), require_explicit_side=True)
    risk.system_state = SystemState.RECONCILING
    reconciler = Reconciler(db, adapter, risk)
    try:
        assert reconciler.resolve_state("BTCUSDT") is False
        assert risk.system_state == SystemState.RECONCILING
    finally:
        db.close()



def test_reconciliation_cumulative_fill_does_not_double_count_existing_fill():
    db = Persistence()
    db.save_intent("1", "BTCUSDT", "BUY", Decimal("2"), Decimal("100"), OrderState.UNKNOWN)
    assert db.add_fill("real-fill", "1", Decimal("1"), Decimal("100"))

    class Query(MockAdapter):
        def query_order(self, symbol, cid):
            return {"clientOrderId": cid, "symbol": symbol, "status": "PARTIALLY_FILLED", "executedQty": "1.5", "avgPrice": "100", "orderId": "ext1"}

    adapter = Query(
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1.5", "entryPrice": "100"}],
        open_orders=[])
    risk = RiskEngine(Decimal("10"), require_explicit_side=True)
    risk.system_state = SystemState.RECONCILING
    reconciler = Reconciler(db, adapter, risk)
    try:
        assert reconciler.resolve_state("BTCUSDT") is True
        assert Decimal(db.get_intent("1")["filled_quantity"]) == Decimal("1.5")
        assert db.get_recorded_fill_total("1") == Decimal("1")
        assert risk.current_position == Decimal("1.5")
        assert risk.reservations == Decimal("0.5")
    finally:
        db.close()


def test_reconciliation_rejects_backward_executed_quantity():
    db = Persistence()
    db.save_intent("1", "BTCUSDT", "BUY", Decimal("2"), Decimal("100"), OrderState.OPEN)
    db.sync_filled_quantity("1", Decimal("1"), Decimal("100"))
    adapter = MockAdapter(
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        open_orders=[{"clientOrderId": "1", "symbol": "BTCUSDT", "status": "PARTIALLY_FILLED", "executedQty": "0.5", "avgPrice": "100", "orderId": "ext1"}],
    )
    reconciler = Reconciler(db, adapter)
    try:
        assert reconciler.resolve_state("BTCUSDT") is False
        assert Decimal(db.get_intent("1")["filled_quantity"]) == Decimal("1")
    finally:
        db.close()
