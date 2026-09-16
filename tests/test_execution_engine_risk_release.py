from decimal import Decimal
from apge.execution_engine import ExecutionEngine
from apge.simulator import RiskEngine, OrderState, RiskApproval, SystemState
import pytest

class MockPersistence:
    def get_intent(self, cid):
        return {"status": "OPEN", "exchange_order_id": None}
    def add_fill(self, fill_id, client_order_id, quantity, price):
        return True # Mock successful fill application
    def update_intent_status(self, *args, **kwargs):
        pass

class MockAdapter:
    pass

def test_execution_engine_releases_risk_on_fill():
    risk_engine = RiskEngine(Decimal("100"))
    risk_engine.system_state = SystemState.OPERATIONAL

    # Actually register an approval and submit it to simulate real order flow
    approval = risk_engine.request_approval(Decimal("1.0"), symbol="BTCUSDT", side="BUY")
    risk_engine.consume_approval_and_submit(approval, "APGE_TEST", symbol="BTCUSDT", side="BUY")

    # Verify reservations incremented
    assert risk_engine.reservations == Decimal("1.0")

    engine = ExecutionEngine(
        adapter=MockAdapter(),
        persistence=MockPersistence(),
        risk_engine=risk_engine
    )

    fake_update = {
        "client_order_id": "APGE_TEST",
        "mapped_state": OrderState.FILLED,
        "order_status": "FILLED",
        "exchange_order_id": "123",
        "execution_type": "TRADE",
        "last_filled_qty": Decimal("1.0"),
        "last_filled_price": Decimal("70000"),
        "trade_id": "999"
    }

    engine.handle_order_update(fake_update)

    # Assert risk is released
    assert risk_engine.reservations == Decimal("0.0")
