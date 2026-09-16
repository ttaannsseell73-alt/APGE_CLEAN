from decimal import Decimal

import pytest

from apge.adaptive_runtime import AdaptiveCycleConfig, AdaptiveGridExecutor, ExchangeConstraints
from apge.execution_engine import ExecutionEngine
from apge.persistence import Persistence
from apge.simulator import OrderState, RiskEngine, SystemState

D = Decimal


class MockAdapter:
    def submit_limit_order(self, **kwargs):
        return {
            "status": "NEW",
            "orderId": f"ext_{kwargs['client_order_id']}",
            "clientOrderId": kwargs["client_order_id"],
            "executedQty": "0",
        }

    def cancel_order(self, symbol, cid):
        return {
            "status": "CANCELED",
            "orderId": f"cancel_{cid}",
            "clientOrderId": cid,
            "executedQty": "0",
        }

    @staticmethod
    def _map_order_state(status):
        return {
            "NEW": OrderState.OPEN,
            "CANCELED": OrderState.CANCELED,
        }.get(status, OrderState.UNKNOWN)


@pytest.fixture
def setup_runtime():
    db = Persistence()
    risk = RiskEngine(position_limit=D("10"), require_explicit_side=True)
    risk.system_state = SystemState.OPERATIONAL
    execution = ExecutionEngine(db, MockAdapter(), risk)
    executor = AdaptiveGridExecutor(
        symbol="BTCUSDT",
        constraints=ExchangeConstraints(
            tick_size=D("0.1"),
            step_size=D("1"),
            min_qty=D("1"),
            min_notional=D("5"),
            max_qty=D("10"),
        ),
        config=AdaptiveCycleConfig(
            base_size=D("1"),
            level_count=2,
            max_inventory=D("3"),
            min_history=4,
        ),
    )
    yield executor, execution, db, risk
    db.close()


def test_identical_adaptive_cycle_does_not_duplicate_orders(setup_runtime):
    executor, execution, db, _ = setup_runtime
    history = [D("100"), D("100.01"), D("99.99"), D("100")]

    first = executor.run_cycle(
        price_history=history,
        best_bid=D("100"),
        best_ask=D("101"),
        current_inventory=D("0"),
        funding_rate=D("0"),
        is_stale_data=False,
        execution_engine=execution,
    )
    assert first.submitted_orders > 0
    first_ids = first.active_order_ids

    second = executor.run_cycle(
        price_history=history,
        best_bid=D("100"),
        best_ask=D("101"),
        current_inventory=D("0"),
        funding_rate=D("0"),
        is_stale_data=False,
        execution_engine=execution,
    )
    assert second.submitted_orders == 0
    assert second.canceled_orders == 0
    assert second.active_order_ids == first_ids
    assert len(db.get_active_intents()) == len(first_ids)


def test_shock_cycle_cancels_entry_grid_and_creates_no_new_risk(setup_runtime):
    executor, execution, db, risk = setup_runtime
    normal = [D("100"), D("100.01"), D("99.99"), D("100")]
    created = executor.run_cycle(
        price_history=normal,
        best_bid=D("100"),
        best_ask=D("101"),
        current_inventory=D("0"),
        funding_rate=D("0"),
        is_stale_data=False,
        execution_engine=execution,
    )
    assert created.active_order_ids

    shock = [D("100"), D("100.01"), D("100"), D("101")]
    defensive = executor.run_cycle(
        price_history=shock,
        best_bid=D("100.9"),
        best_ask=D("101.1"),
        current_inventory=D("0"),
        funding_rate=D("0"),
        is_stale_data=False,
        execution_engine=execution,
    )
    assert defensive.plan is not None
    assert defensive.plan.market_regime.name == "SHOCK"
    assert defensive.submitted_orders == 0
    assert defensive.active_order_ids == tuple()
    assert db.get_active_intents() == []
    assert risk.reservations == 0


def test_missing_history_and_halted_state_fail_closed(setup_runtime):
    executor, execution, _, risk = setup_runtime
    missing = executor.run_cycle(
        price_history=[D("100"), D("100.1")],
        best_bid=D("100"),
        best_ask=D("101"),
        current_inventory=D("0"),
        funding_rate=D("0"),
        is_stale_data=False,
        execution_engine=execution,
    )
    assert missing.blocked_reason == "INSUFFICIENT_HISTORY"

    risk.system_state = SystemState.HALTED
    halted = executor.run_cycle(
        price_history=[D("100"), D("100.01"), D("100.02"), D("100.03")],
        best_bid=D("100"),
        best_ask=D("101"),
        current_inventory=D("0"),
        funding_rate=D("0"),
        is_stale_data=False,
        execution_engine=execution,
    )
    assert halted.blocked_reason == "HALTED"
    assert halted.submitted_orders == 0
