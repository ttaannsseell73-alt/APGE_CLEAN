from decimal import Decimal

from apge.execution_engine import ExecutionEngine
from apge.grid_strategy import MarketRegime, OrderProposal
from apge.persistence import Persistence
from apge.simulator import OrderState, RiskEngine, SystemState
from apge.testnet_runtime import TestnetRuntime


def _tracked_order(engine: RiskEngine, order_id: str, side: str, amount: str = "2"):
    qty = Decimal(amount)
    approval = engine.request_approval(qty, symbol="BTCUSDT", side=side)
    assert approval is not None
    assert engine.consume_approval_and_submit(
        approval, order_id, symbol="BTCUSDT", side=side)
    engine.resolve_order(order_id, OrderState.OPEN, Decimal("0"))
    return engine.orders[order_id]


def test_signed_buy_and_sell_full_fills():
    buy = RiskEngine(Decimal("10"), require_explicit_side=True)
    _tracked_order(buy, "buy-1", "BUY")
    buy.on_fill("buy-1", "b1", Decimal("2"))
    assert buy.current_position == Decimal("2")
    assert buy.reservations == Decimal("0")

    sell = RiskEngine(Decimal("10"), require_explicit_side=True)
    _tracked_order(sell, "sell-1", "SELL")
    sell.on_fill("sell-1", "s1", Decimal("2"))
    assert sell.current_position == Decimal("-2")
    assert sell.reservations == Decimal("0")


def test_signed_partial_fill_and_duplicate_idempotency():
    engine = RiskEngine(Decimal("10"), require_explicit_side=True)
    _tracked_order(engine, "sell-1", "SELL", "4")
    engine.on_fill("sell-1", "s1", Decimal("1.5"))
    engine.on_fill("sell-1", "s1", Decimal("1.5"))
    assert engine.current_position == Decimal("-1.5")
    assert engine.reservations == Decimal("2.5")


def test_strict_side_rejects_missing_or_invalid_side():
    engine = RiskEngine(Decimal("10"), require_explicit_side=True)
    assert engine.request_approval(Decimal("1"), symbol="BTCUSDT") is None
    assert engine.request_approval(Decimal("1"), symbol="BTCUSDT", side="HOLD") is None
    assert engine.reservations == Decimal("0")


def test_overfill_keeps_worst_case_risk_and_reconciles():
    engine = RiskEngine(Decimal("10"), require_explicit_side=True)
    _tracked_order(engine, "buy-1", "BUY", "2")
    engine.on_fill("buy-1", "too-much", Decimal("3"))
    assert engine.system_state == SystemState.RECONCILING
    assert engine.current_position == Decimal("0")
    assert engine.reservations == Decimal("2")


class CancelAdapter:
    def __init__(self, cancel_response):
        self.cancel_response = dict(cancel_response)

    def submit_limit_order(self, **kwargs):
        return {"status": "NEW", "orderId": "ext-new", "clientOrderId": kwargs["client_order_id"]}

    def cancel_order(self, symbol, cid):
        response = dict(self.cancel_response)
        response.setdefault("clientOrderId", cid)
        return response

    def _map_order_state(self, status):
        return {
            "NEW": OrderState.OPEN,
            "CANCELED": OrderState.CANCELED,
            "FILLED": OrderState.FILLED,
        }.get(status, OrderState.UNKNOWN)


def _engine_with_order(cancel_response, side="BUY", qty="2"):
    db = Persistence()
    risk = RiskEngine(Decimal("20"), require_explicit_side=True)
    adapter = CancelAdapter(cancel_response)
    execution = ExecutionEngine(db, adapter, risk)
    cid = execution.execute_proposal(
        "BTCUSDT", OrderProposal(side, Decimal("100"), Decimal(qty)))
    assert cid
    return db, risk, execution, cid


def test_rest_zero_fill_cancel_releases_remaining_reservation():
    db, risk, execution, cid = _engine_with_order(
        {"status": "CANCELED", "orderId": "ext-cancel", "executedQty": "0"})
    try:
        assert risk.reservations == Decimal("2")
        assert execution.cancel_order("BTCUSDT", cid)
        assert risk.orders[cid].state == OrderState.CANCELED
        assert risk.reservations == Decimal("0")
        assert risk.system_state == SystemState.OPERATIONAL
    finally:
        db.close()


def test_partial_sell_fill_then_cancel_is_signed_and_releases_only_remainder():
    db, risk, execution, cid = _engine_with_order(
        {"status": "CANCELED", "orderId": "ext-cancel", "executedQty": "1"},
        side="SELL", qty="2")
    try:
        execution.handle_order_update({
            "client_order_id": cid,
            "execution_type": "TRADE",
            "trade_id": "sell-fill",
            "last_filled_qty": Decimal("1"),
            "last_filled_price": Decimal("100"),
            "accumulated_filled_qty": Decimal("1"),
            "mapped_state": OrderState.PARTIALLY_FILLED,
            "order_status": "PARTIALLY_FILLED",
        })
        assert risk.current_position == Decimal("-1")
        assert risk.reservations == Decimal("1")
        assert execution.cancel_order("BTCUSDT", cid)
        assert risk.current_position == Decimal("-1")
        assert risk.reservations == Decimal("0")
    finally:
        db.close()


def test_missing_authoritative_cancel_total_fails_closed():
    db, risk, execution, cid = _engine_with_order(
        {"status": "CANCELED", "orderId": "ext-cancel"})
    try:
        assert execution.cancel_order("BTCUSDT", cid)
        assert risk.system_state == SystemState.RECONCILING
        assert risk.reservations == Decimal("2")
    finally:
        db.close()


def test_websocket_cancel_uses_accumulated_filled_qty_and_is_idempotent():
    db, risk, execution, cid = _engine_with_order(
        {"status": "CANCELED", "orderId": "unused", "executedQty": "0"})
    try:
        update = {
            "client_order_id": cid,
            "execution_type": "CANCELED",
            "trade_id": "",
            "last_filled_qty": Decimal("0"),
            "last_filled_price": Decimal("0"),
            "accumulated_filled_qty": Decimal("0"),
            "mapped_state": OrderState.CANCELED,
            "order_status": "CANCELED",
        }
        execution.handle_order_update(update)
        execution.handle_order_update(update)
        assert risk.reservations == Decimal("0")
        assert risk.orders[cid].state == OrderState.CANCELED
    finally:
        db.close()


class GridAdapter(CancelAdapter):
    def __init__(self):
        super().__init__({"status": "CANCELED", "orderId": "cancelled", "executedQty": "0"})
        self.submissions = []

    def submit_limit_order(self, **kwargs):
        self.submissions.append(dict(kwargs))
        return {"status": "NEW", "orderId": f"ext-{len(self.submissions)}", "clientOrderId": kwargs["client_order_id"]}

    def parse_book_ticker(self, payload):
        return {
            "symbol": payload["s"],
            "bid_price": Decimal(str(payload["b"])),
            "ask_price": Decimal(str(payload["a"])),
        }


def test_deterministic_reprice_100_cycles_has_no_reservation_or_cid_leak():
    db = Persistence()
    adapter = GridAdapter()
    risk = RiskEngine(Decimal("1000"), require_explicit_side=True)
    execution = ExecutionEngine(db, adapter, risk)
    runtime = TestnetRuntime(adapter)
    runtime.tick_size = Decimal("0.1")
    runtime.step_size = Decimal("1")
    runtime.filters = {
        "minQty": Decimal("1"),
        "minNotional": Decimal("5"),
        "maxQty": Decimal("100"),
    }
    runtime.attach_components(execution, None, "BTCUSDT")

    try:
        seen_active_sets = []
        for i in range(100):
            bid = Decimal("100") + Decimal(i)
            ask = bid + Decimal("1")
            runtime.handle_book_ticker({"s": "BTCUSDT", "b": str(bid), "a": str(ask), "u": i})
            runtime.run_grid_cycle(
                grid_spacing=Decimal("1"),
                base_size=Decimal("1"),
                level_count=1,
                max_inventory=Decimal("10"),
                system_state=risk.system_state,
                market_regime=MarketRegime.NEUTRAL,
                execution_engine=execution,
            )

            assert risk.system_state == SystemState.OPERATIONAL
            assert risk.reservations >= Decimal("0")
            active = db.get_active_intents()
            active_ids = {row["client_order_id"] for row in active}
            assert len(active_ids) == len(active)
            assert risk.reservations == sum(
                (Decimal(row["quantity"]) for row in active), Decimal("0"))

            risk_active_ids = {
                oid for oid, order in risk.orders.items()
                if order.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED, OrderState.UNKNOWN)
            }
            assert risk_active_ids == active_ids
            seen_active_sets.append(active_ids)

        assert all(len(ids) == 2 for ids in seen_active_sets)
    finally:
        db.close()
