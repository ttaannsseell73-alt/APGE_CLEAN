from apge.simulator import OrderState
import pytest
from decimal import Decimal

from apge.testnet_runtime import TestnetRuntime
from apge.execution_engine import ExecutionEngine
from apge.persistence import Persistence
from apge.simulator import RiskEngine, SystemState
from apge.grid_strategy import MarketRegime

class MockAdapter:
    def submit_limit_order(self, **kwargs):
        return {"status": "NEW", "orderId": "ext_new", "clientOrderId": kwargs["client_order_id"]}
    def cancel_order(self, symbol, cid):
        return {"status": "CANCELED", "orderId": "ext_cancel", "clientOrderId": cid, "executedQty": "0"}
    def _map_order_state(self, status):
        mapping = {"NEW": 1, "CANCELED": 4} # Mocked Enum
        return mapping.get(status)

    def parse_book_ticker(self, payload):
        return {
            "symbol": payload["s"],
            "bid_price": Decimal(str(payload["b"])),
            "ask_price": Decimal(str(payload["a"]))
        }

@pytest.fixture
def runtime_setup():
    db = Persistence()
    adapter = MockAdapter()
    risk_engine = RiskEngine(position_limit=Decimal("100.0"))
    risk_engine.system_state = SystemState.OPERATIONAL
    execution_engine = ExecutionEngine(db, adapter, risk_engine)

    # Needs to match simulator enums
    from apge.simulator import OrderState
    adapter._map_order_state = lambda s: OrderState.OPEN if s == "NEW" else OrderState.CANCELED

    runtime = TestnetRuntime(adapter)
    runtime.tick_size = Decimal("0.1")
    runtime.step_size = Decimal("1.0")
    runtime.filters = {
        "minQty": Decimal("0.1"),
        "minNotional": Decimal("5.0"),
        "maxQty": Decimal("100.0")
    }
    runtime.attach_components(execution_engine, None, "BTCUSDT")

    yield runtime, execution_engine, db

    db.close()

def test_grid_bba_neutral(runtime_setup):
    runtime, execution_engine, db = runtime_setup

    # Set BBA
    runtime.handle_book_ticker({"s": "BTCUSDT", "b": "100.0", "a": "101.0", "u": 1})

    # Run cycle
    runtime.run_grid_cycle(
        grid_spacing=Decimal("1.0"),
        base_size=Decimal("1.0"),
        level_count=3,
        max_inventory=Decimal("5.0"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        execution_engine=execution_engine
    )

    intents = db.get_active_intents()
    # 3 bids, 3 asks
    assert len(intents) == 6
    bids = [i for i in intents if i["side"] == "BUY"]
    asks = [i for i in intents if i["side"] == "SELL"]
    assert len(bids) == 3
    assert len(asks) == 3

def test_stale_bba_forces_reduce_only(runtime_setup):
    runtime, execution_engine, db = runtime_setup

    runtime.handle_book_ticker({"s": "BTCUSDT", "b": "100.0", "a": "101.0", "u": 1})

    # Force stale
    runtime.bba_receive_timestamp = 0
    runtime.current_inventory = Decimal("2.0")

    runtime.run_grid_cycle(
        grid_spacing=Decimal("1.0"),
        base_size=Decimal("1.0"),
        level_count=3,
        max_inventory=Decimal("5.0"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        execution_engine=execution_engine
    )

    intents = db.get_active_intents()
    # Only SELLs allowed to reduce 2.0
    assert len(intents) > 0
    assert all(i["side"] == "SELL" for i in intents)

def test_crossed_bba_no_orders(runtime_setup):
    runtime, execution_engine, db = runtime_setup

    # Crossed book
    runtime.handle_book_ticker({"s": "BTCUSDT", "b": "102.0", "a": "101.0", "u": 1})

    runtime.run_grid_cycle(
        grid_spacing=Decimal("1.0"),
        base_size=Decimal("1.0"),
        level_count=3,
        max_inventory=Decimal("5.0"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        execution_engine=execution_engine
    )

    intents = db.get_active_intents()
    assert len(intents) == 0

def test_desired_vs_live_diffs(runtime_setup):
    runtime, execution_engine, db = runtime_setup

    runtime.handle_book_ticker({"s": "BTCUSDT", "b": "100.0", "a": "101.0", "u": 1})

    # First cycle creates 6 orders
    runtime.run_grid_cycle(
        grid_spacing=Decimal("1.0"),
        base_size=Decimal("1.0"),
        level_count=3,
        max_inventory=Decimal("5.0"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        execution_engine=execution_engine
    )

    intents1 = db.get_active_intents()
    assert len(intents1) == 6

    for intent in intents1:
        execution_engine.risk_engine.resolve_order(intent["client_order_id"], OrderState.OPEN, Decimal("0.0"))

    # Second cycle with same params should create NO new orders
    runtime.run_grid_cycle(
        grid_spacing=Decimal("1.0"),
        base_size=Decimal("1.0"),
        level_count=3,
        max_inventory=Decimal("5.0"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        execution_engine=execution_engine
    )

    intents2 = db.get_active_intents()
    assert len(intents2) == 6
    assert set(i["client_order_id"] for i in intents1) == set(i["client_order_id"] for i in intents2)

    # Third cycle changes spacing, should cancel all and recreate
    runtime.run_grid_cycle(
        grid_spacing=Decimal("2.0"),
        base_size=Decimal("1.0"),
        level_count=3,
        max_inventory=Decimal("5.0"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        execution_engine=execution_engine
    )

    intents3 = db.get_active_intents()
    assert len(intents3) == 6
    # None of the old orders should be active, except those that coincidentally map to the same price
    # (e.g. 100 - 2 = 98 which was generated by 100 - 1*2 in the old run)
    # The diff engine exactly matches side, price, qty. So overlapping prices are correctly kept.
    old_cids = set(i["client_order_id"] for i in intents1)
    new_cids = set(i["client_order_id"] for i in intents3)

    # Check that it did cancel the ones that don't match anymore
    canceled_intents = [i for i in db.get_all_intents() if i["status"] == "CANCELED"]
    assert len(canceled_intents) == 4 # 2 old orders match the new grid exactly, 4 are canceled.

def test_inventory_fill_triggers_counter_order(runtime_setup):
    runtime, execution_engine, db = runtime_setup
    runtime.filters = {"minQty": Decimal("0.1"), "minNotional": Decimal("5.0"), "maxQty": Decimal("100.0")}
    runtime.handle_book_ticker({"s": "BTCUSDT", "b": "100.0", "a": "101.0", "u": 1})

    # 1. Start with 0 inventory
    runtime.current_inventory = Decimal("0.0")
    runtime.run_grid_cycle(
        grid_spacing=Decimal("1.0"),
        base_size=Decimal("5.0"), # Max size per level
        level_count=2, # Will attempt 2 levels (10.0 total capacity needed)
        max_inventory=Decimal("5.0"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        execution_engine=execution_engine
    )

    intents1 = db.get_active_intents()
    # At inventory 0.0, capacity is 5.0. Only 1 level of 5.0 fits.
    assert len(intents1) == 2 # 1 BUY (5.0 @ 99), 1 SELL (5.0 @ 102)

    for intent in intents1:
        execution_engine.risk_engine.resolve_order(intent["client_order_id"], OrderState.OPEN, Decimal("0.0"))

    # 2. Simulate an actual BUY fill
    buy_intent = next(i for i in intents1 if i["side"] == "BUY")
    cid = buy_intent["client_order_id"]

    # Deliver the fill natively
    execution_engine.handle_order_update({
        "client_order_id": cid,
        "execution_type": "TRADE",
        "trade_id": "trade_buy_1",
        "last_filled_qty": Decimal("5.0"),
        "last_filled_price": Decimal("99.0"),
        "mapped_state": OrderState.FILLED,
        "order_status": "FILLED"
    })

    # Prove fill applied exactly once
    updated_buy_intent = db.get_intent(cid)
    assert updated_buy_intent["status"] == "FILLED"
    assert Decimal(updated_buy_intent["filled_quantity"]) == Decimal("5.0")
    fills = db.conn.execute("SELECT * FROM fills WHERE client_order_id = ?", (cid,)).fetchall()
    assert len(fills) == 1

    # Simulate corresponding ACCOUNT_UPDATE
    runtime.adapter.parse_account_update = lambda e: {"positions": [{"symbol": "BTCUSDT", "position_amount": Decimal("5.0")}]}
    runtime.handle_user_data_event({"e": "ACCOUNT_UPDATE"}, execution_engine)

    # Prove inventory changed due to event
    assert runtime.current_inventory == Decimal("5.0")

    # 3. Next grid cycle
    runtime.run_grid_cycle(
        grid_spacing=Decimal("1.0"),
        base_size=Decimal("5.0"),
        level_count=2,
        max_inventory=Decimal("5.0"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        execution_engine=execution_engine
    )

    intents2 = db.get_active_intents()
    buys = [i for i in intents2 if i["side"] == "BUY"]
    sells = [i for i in intents2 if i["side"] == "SELL"]

    # No buys due to max long
    assert len(buys) == 0

    # Old sell was 1 level at 102.0.
    # Now short capacity is 5.0 (max) + 5.0 (inv) = 10.0.
    # Level count is 2. Both levels should spawn! (5.0 @ 102, 5.0 @ 103)
    assert len(sells) == 2
    prices = {Decimal(s["price"]) for s in sells}
    assert prices == {Decimal("102.0"), Decimal("103.0")}

    # The 103.0 order is definitively a newly proposed counter-exposure created because inventory increased
    # The assertion proves it cannot pass merely because the original pre-fill SELL order still exists.
