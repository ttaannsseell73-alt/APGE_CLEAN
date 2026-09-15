import pytest
from decimal import Decimal

from apge.persistence import Persistence
from apge.simulator import OrderState, RiskEngine, SystemState
from apge.execution_engine import ExecutionEngine
from apge.grid_strategy import OrderProposal

class MockAdapter:
    def submit_limit_order(self, **kwargs):
        if kwargs["price"] == Decimal("999.0"):
            return {"status": "REJECTED"}
        return {"status": "NEW", "orderId": "ext1", "clientOrderId": kwargs["client_order_id"]}

    def _map_order_state(self, status):
        mapping = {"NEW": OrderState.OPEN, "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED, "FILLED": OrderState.FILLED}
        # If REJECTED, it returns UNKNOWN (since mapping.get has OrderState.UNKNOWN as default)
        return mapping.get(status, OrderState.UNKNOWN)

@pytest.fixture
def engine_setup():
    db = Persistence()
    risk_engine = RiskEngine(position_limit=Decimal("5.0"))
    risk_engine.system_state = SystemState.OPERATIONAL
    adapter = MockAdapter()
    engine = ExecutionEngine(db, adapter, risk_engine)
    yield engine, db
    db.close()

def test_submit_accepted(engine_setup):
    engine, db = engine_setup
    proposal = OrderProposal("BUY", Decimal("100.0"), Decimal("1.0"))
    cid = engine.execute_proposal("BTCUSDT", proposal)

    assert cid is not None
    intent = db.get_intent(cid)
    assert intent["status"] == "OPEN"
    assert intent["exchange_order_id"] == "ext1"

def test_definite_rejection(engine_setup):
    engine, db = engine_setup
    # Trigger rejection
    proposal = OrderProposal("BUY", Decimal("999.0"), Decimal("1.0"))
    cid = engine.execute_proposal("BTCUSDT", proposal)

    assert cid is None
    # We still want to make sure it was marked UNKNOWN in db (per memory strict requirement)
    intents = db.get_all_intents()
    assert intents[-1]["status"] == "UNKNOWN"

def test_partial_and_full_fills(engine_setup):
    engine, db = engine_setup
    proposal = OrderProposal("BUY", Decimal("100.0"), Decimal("2.0"))
    cid = engine.execute_proposal("BTCUSDT", proposal)

    # 1. Simulate partial fill via handle_order_update
    engine.handle_order_update({
        "client_order_id": cid,
        "execution_type": "TRADE",
        "trade_id": "trade1",
        "last_filled_qty": Decimal("1.0"),
        "last_filled_price": Decimal("100.0"),
        "mapped_state": OrderState.PARTIALLY_FILLED, "order_status": "PARTIALLY_FILLED"
    })

    intent = db.get_intent(cid)
    assert intent["status"] == "PARTIALLY_FILLED"
    assert Decimal(intent["filled_quantity"]) == Decimal("1.0")

    # 2. Simulate duplicate partial fill (idempotency check)
    engine.handle_order_update({
        "client_order_id": cid,
        "execution_type": "TRADE",
        "trade_id": "trade1", # Same trade ID
        "last_filled_qty": Decimal("1.0"),
        "last_filled_price": Decimal("100.0"),
        "mapped_state": OrderState.PARTIALLY_FILLED, "order_status": "PARTIALLY_FILLED"
    })

    intent = db.get_intent(cid)
    # Shouldn't double count
    assert Decimal(intent["filled_quantity"]) == Decimal("1.0")

    # 3. Full fill
    engine.handle_order_update({
        "client_order_id": cid,
        "execution_type": "TRADE",
        "trade_id": "trade2",
        "last_filled_qty": Decimal("1.0"),
        "last_filled_price": Decimal("100.0"),
        "mapped_state": OrderState.FILLED, "order_status": "FILLED"
    })

    intent = db.get_intent(cid)
    assert intent["status"] == "FILLED"
    assert Decimal(intent["filled_quantity"]) == Decimal("2.0")
