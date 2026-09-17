from decimal import Decimal

from apge.adaptive_controller import run_adaptive_grid_cycle
from apge.execution_engine import ExecutionEngine
from apge.grid_lifecycle import apply_grid_diff
from apge.grid_strategy import OrderProposal
from apge.market_regime import Candle
from apge.persistence import Persistence
from apge.simulator import OrderState, RiskEngine, SystemState


D = Decimal


class Adapter:
    def __init__(self):
        self.submits = []
        self.cancels = []

    def submit_limit_order(self, **kwargs):
        self.submits.append(kwargs)
        return {
            "status": "NEW",
            "orderId": f"ex-{kwargs['client_order_id']}",
            "executedQty": "0",
            "avgPrice": "0",
        }

    def cancel_order(self, symbol, cid):
        self.cancels.append((symbol, cid))
        return {"status": "CANCELED", "orderId": f"ex-{cid}", "executedQty": "0"}

    def _map_order_state(self, status):
        return {
            "NEW": OrderState.OPEN,
            "CANCELED": OrderState.CANCELED,
        }.get(status, OrderState.UNKNOWN)


class Runtime:
    def __init__(self, inventory=D("0"), stale=False):
        self.best_bid = D("100")
        self.best_ask = D("101")
        self.current_inventory = inventory
        self.tick_size = D("0.1")
        self.step_size = D("0.1")
        self.filters = {
            "minQty": D("0.1"),
            "maxQty": D("10"),
            "minNotional": D("1"),
        }
        self.symbol = "BTCUSDT"
        self._stale = stale

    def is_stale_data(self):
        return self._stale


def _candle(open_price, close_price, pad=D("0.002")):
    open_price = D(str(open_price))
    close_price = D(str(close_price))
    return Candle(
        open=open_price,
        high=max(open_price, close_price) * (D("1") + pad),
        low=min(open_price, close_price) * (D("1") - pad),
        close=close_price,
    )


def _trend(step=D("0.0003"), count=20):
    result = []
    price = D("100")
    for _ in range(count):
        close = price * (D("1") + step)
        result.append(_candle(price, close))
        price = close
    return result


def _engine(position=D("0")):
    db = Persistence()
    adapter = Adapter()
    risk = RiskEngine(D("4"), require_explicit_side=True)
    risk.current_position = position
    risk.system_state = SystemState.OPERATIONAL
    return db, adapter, risk, ExecutionEngine(db, adapter, risk)


def test_adaptive_cycle_places_bounded_asymmetric_grid():
    db, adapter, risk, engine = _engine()
    runtime = Runtime()
    result = run_adaptive_grid_cycle(
        runtime=runtime,
        execution_engine=engine,
        candles=_trend(),
        funding_rate=D("0"),
        nominal_base_size=D("1"),
        level_count=1,
        max_inventory=D("4"),
    )
    assert not result.lifecycle.blocked
    assert len(adapter.submits) == 2
    quantities = {row["side"]: row["quantity"] for row in adapter.submits}
    assert quantities["BUY"] <= D("1")
    assert quantities["SELL"] < quantities["BUY"]
    assert risk.system_state == SystemState.OPERATIONAL
    db.close()


def test_invalid_market_data_fails_closed_without_submission():
    db, adapter, risk, engine = _engine()
    runtime = Runtime()
    result = run_adaptive_grid_cycle(
        runtime=runtime,
        execution_engine=engine,
        candles=_trend(count=5),
        funding_rate=D("0"),
        nominal_base_size=D("1"),
        level_count=1,
        max_inventory=D("4"),
    )
    assert result.lifecycle.blocked
    assert adapter.submits == []
    assert risk.system_state == SystemState.RECONCILING
    assert db.get_active_intents() == []
    db.close()


def test_shock_with_long_inventory_only_generates_reducing_sell():
    db, adapter, risk, engine = _engine(D("1"))
    runtime = Runtime(inventory=D("1"))
    candles = [_candle("100", "100") for _ in range(19)]
    candles.append(Candle(D("100"), D("105.5"), D("99.9"), D("105")))
    result = run_adaptive_grid_cycle(
        runtime=runtime,
        execution_engine=engine,
        candles=candles,
        funding_rate=D("-0.0001"),
        nominal_base_size=D("1"),
        level_count=2,
        max_inventory=D("4"),
    )
    assert not result.lifecycle.blocked
    assert adapter.submits
    assert all(row["side"] == "SELL" for row in adapter.submits)
    assert sum((row["quantity"] for row in adapter.submits), D("0")) <= D("1")
    db.close()


def test_duplicate_active_local_grid_intent_forces_reconciliation():
    db, adapter, risk, engine = _engine()
    db.save_intent("APGE_A", "BTCUSDT", "BUY", D("1"), D("99"), OrderState.OPEN)
    db.save_intent("APGE_B", "BTCUSDT", "BUY", D("1"), D("99"), OrderState.OPEN)
    result = apply_grid_diff(
        "BTCUSDT",
        [OrderProposal("BUY", D("99"), D("1"))],
        engine,
    )
    assert result.blocked
    assert "duplicate active" in result.blocked_reason
    assert risk.system_state == SystemState.RECONCILING
    assert adapter.submits == []
    db.close()
