import pytest
from decimal import Decimal

from apge.persistence import Persistence
from apge.simulator import OrderState, RiskEngine, SystemState
from apge.execution_engine import ExecutionEngine
from apge.grid_strategy import OrderProposal

class MockTimeoutAdapter:
    def submit_limit_order(self, **kwargs):
        # Simulate network timeout returning UNKNOWN
        return {"status": "UNKNOWN", "clientOrderId": kwargs["client_order_id"]}

    def _map_order_state(self, status):
        mapping = {"FILLED": OrderState.FILLED, "CANCELED": OrderState.CANCELED}
        return mapping.get(status, OrderState.UNKNOWN)

@pytest.fixture
def engine_setup():
    db = Persistence()
    risk_engine = RiskEngine(position_limit=Decimal("5.0"))
    risk_engine.system_state = SystemState.OPERATIONAL
    adapter = MockTimeoutAdapter()
    engine = ExecutionEngine(db, adapter, risk_engine)
    yield engine, db
    db.close()

def test_unknown_submission_reconcile_query(engine_setup):
    engine, db = engine_setup
    proposal = OrderProposal("BUY", Decimal("100.0"), Decimal("1.0"))
    cid = engine.execute_proposal("BTCUSDT", proposal)

    # 1. State must be UNKNOWN
    intent = db.get_intent(cid)
    assert intent["status"] == "UNKNOWN"

    # 2. Later, a reconciliation query finds it
    from apge.reconciliation import Reconciler

    # We patch adapter for the reconciler to return successfully on query
    class QueryAdapter(MockTimeoutAdapter):
        def query_order(self, symbol, orig_cid):
            if orig_cid == cid:
                return {"status": "FILLED", "orderId": "resolved123", "executedQty": "1.0", "avgPrice": "100.0"}
            raise Exception("Order does not exist")
        def get_positions(self): return []
        def get_open_orders(self, symbol=None): return []

    reconciler = Reconciler(db, QueryAdapter())
    success = reconciler.resolve_state("BTCUSDT")
    assert success == True

    # Intent should be updated
    updated_intent = db.get_intent(cid)
    assert updated_intent["status"] == "FILLED"
    assert Decimal(updated_intent["filled_quantity"]) == Decimal("1.0")

def test_unknown_submission_query_absent(engine_setup):
    engine, db = engine_setup
    proposal = OrderProposal("BUY", Decimal("100.0"), Decimal("1.0"))
    cid = engine.execute_proposal("BTCUSDT", proposal)

    from apge.reconciliation import Reconciler

    # Patch adapter for reconciler: returns -2013 (Order does not exist)
    class MissingQueryAdapter(MockTimeoutAdapter):
        def query_order(self, symbol, orig_cid):
            raise Exception("Order does not exist -2013")
        def get_positions(self): return []
        def get_open_orders(self, symbol=None): return []

    reconciler = Reconciler(db, MissingQueryAdapter())
    success = reconciler.resolve_state("BTCUSDT")
    assert success == True

    # Since it never existed, safe to mark as CANCELED
    updated_intent = db.get_intent(cid)
    assert updated_intent["status"] == "CANCELED"

def test_cancel_fill_race_condition(engine_setup):
    # Tests that if we mark something as canceled locally, but it filled on exchange,
    # the websocket trade update properly overrides the canceled state.
    engine, db = engine_setup

    # Manually setup a locally canceled order
    cid = "race1"
    db.save_intent(cid, "BTCUSDT", "BUY", Decimal("1.0"), Decimal("100"), OrderState.CANCELED)

    # A websocket fill for persistence state that has no matching RiskEngine
    # order is an inconsistent restart/race snapshot and must fail closed.
    engine.handle_order_update({
        "client_order_id": cid,
        "execution_type": "TRADE",
        "trade_id": "trade1",
        "last_filled_qty": Decimal("1.0"),
        "last_filled_price": Decimal("100.0"),
        "accumulated_filled_qty": Decimal("1.0"),
        "mapped_state": OrderState.FILLED, "order_status": "FILLED"
    })

    intent = db.get_intent(cid)
    assert intent["status"] == "UNKNOWN"
    assert Decimal(intent["filled_quantity"]) == Decimal("0")
    assert engine.risk_engine.system_state == SystemState.RECONCILING

def test_unknown_blocks_risk_increasing_orders(engine_setup):
    engine, db = engine_setup

    # 1. Trigger an UNKNOWN state
    proposal1 = OrderProposal("BUY", Decimal("100.0"), Decimal("1.0"))
    cid1 = engine.execute_proposal("BTCUSDT", proposal1)
    assert cid1 is not None
    assert engine.risk_engine.system_state == SystemState.RECONCILING

    # 2. Try to submit a second risk-increasing order
    proposal2 = OrderProposal("BUY", Decimal("99.0"), Decimal("1.0"))
    cid2 = engine.execute_proposal("BTCUSDT", proposal2)

    # Since UNKNOWN orders exist, the RiskEngine gate must reject it outright.
    assert cid2 is None

def test_deterministic_client_order_id(engine_setup):
    engine, db = engine_setup

    # 1. Ask for a CID for a specific intent
    cid1 = engine._generate_client_order_id("BTCUSDT", "BUY", Decimal("1.0"), Decimal("100.0"))

    # 2. Time has passed, state hasn't changed. Should get EXACT SAME CID.
    cid2 = engine._generate_client_order_id("BTCUSDT", "BUY", Decimal("1.0"), Decimal("100.0"))
    assert cid1 == cid2
    assert cid1.startswith("APGE_")

    # 3. If we submit it, the execution engine should catch the duplicate on retry
    proposal = OrderProposal("BUY", Decimal("100.0"), Decimal("1.0"))
    engine.execute_proposal("BTCUSDT", proposal)

    # The mock returns UNKNOWN, making the system RECONCILING. Let's resolve it to OPEN so we can submit again.
    engine.risk_engine.system_state = SystemState.OPERATIONAL
    engine.risk_engine.resolve_order(cid1, OrderState.OPEN, Decimal("0.0"))
    db.update_intent_status(cid1, OrderState.OPEN, "ext1", "NEW")

    # Now it's active. Generating again should still give same CID.
    cid3 = engine._generate_client_order_id("BTCUSDT", "BUY", Decimal("1.0"), Decimal("100.0"))
    assert cid1 == cid3

    # Executing the exact same proposal again should NOT call submit_limit_order (mock would throw or count)
    # The execute_proposal method logs "Skipping submission to avoid duplicates" and returns cid.
    # Note: execute_proposal consumes the approval FIRST. So if it gets skipped, the approval is thrown away.
    # It must return the CID. Let's try it.
    cid4 = engine.execute_proposal("BTCUSDT", proposal)
    assert cid4 == cid1

    # Intents table should still only have 1 row
    assert len(db.get_all_intents()) == 1

    # 4. If we mark it terminal, we get a NEW CID so we can trade again at this level
    db.update_intent_status(cid1, OrderState.FILLED, "ext1", "FILLED")
    cid5 = engine._generate_client_order_id("BTCUSDT", "BUY", Decimal("1.0"), Decimal("100.0"))
    assert cid5 != cid1
