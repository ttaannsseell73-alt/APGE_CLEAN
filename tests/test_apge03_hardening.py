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
        return {"status": "NEW", "orderId": "ext-new", "clientOrderId": kwargs["client_order_id"], "executedQty": "0"}

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
        return {"status": "NEW", "orderId": f"ext-{len(self.submissions)}", "clientOrderId": kwargs["client_order_id"], "executedQty": "0"}

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



def test_side_aware_capacity_blocks_same_direction_overexposure():
    engine = RiskEngine(Decimal("10"), require_explicit_side=True)
    assert engine.rebuild_from_authoritative_state(Decimal("-9"), [])
    assert engine.request_approval(Decimal("2"), symbol="BTCUSDT", side="SELL") is None
    assert engine.request_approval(Decimal("1"), symbol="BTCUSDT", side="SELL") is not None

    other = RiskEngine(Decimal("10"), require_explicit_side=True)
    assert other.rebuild_from_authoritative_state(Decimal("9"), [])
    assert other.request_approval(Decimal("2"), symbol="BTCUSDT", side="BUY") is None
    assert other.request_approval(Decimal("1"), symbol="BTCUSDT", side="BUY") is not None


def test_opposite_pending_orders_are_not_netted_for_worst_case_limit():
    engine = RiskEngine(Decimal("10"), require_explicit_side=True)
    assert engine.rebuild_from_authoritative_state(Decimal("0"), [])
    buy = engine.request_approval(Decimal("8"), symbol="BTCUSDT", side="BUY")
    sell = engine.request_approval(Decimal("8"), symbol="BTCUSDT", side="SELL")
    assert buy is not None
    assert sell is not None
    assert engine.request_approval(Decimal("3"), symbol="BTCUSDT", side="BUY") is None
    assert engine.request_approval(Decimal("3"), symbol="BTCUSDT", side="SELL") is None


def test_cancel_authoritative_extra_fill_syncs_persistence_and_signed_risk():
    db, risk, execution, cid = _engine_with_order(
        {"status": "CANCELED", "orderId": "ext-cancel", "executedQty": "1.5", "avgPrice": "100"},
        side="SELL", qty="2")
    try:
        execution.handle_order_update({
            "client_order_id": cid,
            "execution_type": "TRADE",
            "trade_id": "seen-one",
            "last_filled_qty": Decimal("1"),
            "last_filled_price": Decimal("100"),
            "accumulated_filled_qty": Decimal("1"),
            "mapped_state": OrderState.PARTIALLY_FILLED,
            "order_status": "PARTIALLY_FILLED",
        })
        assert execution.cancel_order("BTCUSDT", cid)
        intent = db.get_intent(cid)
        assert intent["status"] == "CANCELED"
        assert Decimal(intent["filled_quantity"]) == Decimal("1.5")
        assert risk.current_position == Decimal("-1.5")
        assert risk.reservations == Decimal("0")
    finally:
        db.close()


def test_missing_cancel_total_keeps_persistence_nonterminal_and_risk_reserved():
    db, risk, execution, cid = _engine_with_order(
        {"status": "CANCELED", "orderId": "ext-cancel"})
    try:
        assert execution.cancel_order("BTCUSDT", cid)
        assert db.get_intent(cid)["status"] == "UNKNOWN"
        assert risk.system_state == SystemState.RECONCILING
        assert risk.reservations == Decimal("2")
        assert risk.orders[cid].state == OrderState.OPEN
    finally:
        db.close()


def test_contradictory_duplicate_cancel_fails_closed():
    engine = RiskEngine(Decimal("10"), require_explicit_side=True)
    _tracked_order(engine, "buy-1", "BUY", "2")
    assert engine.on_cancel_confirmed("buy-1", Decimal("0")) is True
    assert engine.on_cancel_confirmed("buy-1", Decimal("1")) is False
    assert engine.system_state == SystemState.RECONCILING
    assert "buy-1" in engine.unresolved_conflicts


def test_authoritative_rebuild_restores_position_orders_and_reservations():
    engine = RiskEngine(Decimal("10"), require_explicit_side=True)
    intents = [
        {"client_order_id": "b", "symbol": "BTCUSDT", "side": "BUY", "quantity": "2", "filled_quantity": "0", "observed_filled_quantity": "0", "status": "OPEN"},
        {"client_order_id": "s", "symbol": "BTCUSDT", "side": "SELL", "quantity": "3", "filled_quantity": "1", "observed_filled_quantity": "1", "status": "PARTIALLY_FILLED"},
        {"client_order_id": "c", "symbol": "BTCUSDT", "side": "BUY", "quantity": "1", "filled_quantity": "0.5", "observed_filled_quantity": "0.25", "status": "CANCELED"},
    ]
    assert engine.rebuild_from_authoritative_state(Decimal("-2"), intents)
    assert engine.current_position == Decimal("-2")
    assert engine.reservations == Decimal("4")
    assert engine.orders["b"].side == "BUY"
    assert engine.orders["s"].state == OrderState.PARTIALLY_FILLED
    assert engine.orders["c"].cancel_confirmed_total == Decimal("0.5")
    assert engine.orders["c"].seen_fills_sum == Decimal("0.25")



def test_resolve_order_supports_partial_and_rejects_incoherent_state():
    engine = RiskEngine(Decimal("10"), require_explicit_side=True)
    approval = engine.request_approval(Decimal("2"), symbol="BTCUSDT", side="SELL")
    assert approval is not None
    assert engine.consume_approval_and_submit(approval, "s", symbol="BTCUSDT", side="SELL")
    assert engine.resolve_order("s", OrderState.PARTIALLY_FILLED, Decimal("1")) is True
    assert engine.orders["s"].state == OrderState.PARTIALLY_FILLED
    assert engine.current_position == Decimal("-1")
    assert engine.reservations == Decimal("1")

    bad = RiskEngine(Decimal("10"), require_explicit_side=True)
    approval = bad.request_approval(Decimal("2"), symbol="BTCUSDT", side="BUY")
    assert approval is not None
    assert bad.consume_approval_and_submit(approval, "b", symbol="BTCUSDT", side="BUY")
    assert bad.resolve_order("b", OrderState.FILLED, Decimal("1")) is False
    assert bad.system_state == SystemState.RECONCILING
    assert bad.reservations == Decimal("2")


def test_server_time_offset_changes_signed_timestamp():
    from apge.binance_adapter import BinanceAdapter

    class NoopTransport:
        pass

    adapter = BinanceAdapter(
        NoopTransport(),
        "https://testnet.binancefuture.com",
        "wss://fstream.binancefuture.com",
        "k", "s", clock=lambda: 1000.0)
    adapter.server_time_offset = 250
    params = adapter._prepare_signed_params({"symbol": "BTCUSDT"})
    assert params["timestamp"] == "1000250"


def test_persistence_rejects_overfill_before_mutation():
    db = Persistence()
    try:
        db.save_intent("x", "BTCUSDT", "BUY", Decimal("1"), Decimal("100"), OrderState.OPEN)
        assert db.add_fill("f1", "x", Decimal("0.75"), Decimal("100")) is True
        assert db.add_fill("f2", "x", Decimal("0.50"), Decimal("100")) is False
        assert Decimal(db.get_intent("x")["filled_quantity"]) == Decimal("0.75")
        assert db.get_recorded_fill_total("x") == Decimal("0.75")
    finally:
        db.close()


def test_submit_ack_filled_applies_signed_position_and_releases_risk():
    class FilledAdapter(CancelAdapter):
        def __init__(self):
            super().__init__({"status": "CANCELED", "orderId": "unused", "executedQty": "0"})
        def submit_limit_order(self, **kwargs):
            return {"status": "FILLED", "orderId": "filled-now", "executedQty": str(kwargs["quantity"]), "avgPrice": str(kwargs["price"])}

    db = Persistence()
    risk = RiskEngine(Decimal("10"), require_explicit_side=True)
    execution = ExecutionEngine(db, FilledAdapter(), risk)
    try:
        cid = execution.execute_proposal("BTCUSDT", OrderProposal("SELL", Decimal("100"), Decimal("2")))
        assert cid is not None
        assert risk.current_position == Decimal("-2")
        assert risk.reservations == Decimal("0")
        assert risk.orders[cid].state == OrderState.FILLED
        assert db.get_intent(cid)["status"] == "FILLED"
        assert Decimal(db.get_intent(cid)["filled_quantity"]) == Decimal("2")
    finally:
        db.close()


def test_submit_ack_unknown_status_forces_reconciliation():
    class WeirdAdapter(CancelAdapter):
        def __init__(self):
            super().__init__({"status": "CANCELED", "orderId": "unused", "executedQty": "0"})
        def submit_limit_order(self, **kwargs):
            return {"status": "EXPIRED", "orderId": "weird", "executedQty": "0"}

    db = Persistence()
    risk = RiskEngine(Decimal("10"), require_explicit_side=True)
    execution = ExecutionEngine(db, WeirdAdapter(), risk)
    try:
        cid = execution.execute_proposal("BTCUSDT", OrderProposal("BUY", Decimal("100"), Decimal("1")))
        assert cid is not None
        assert risk.system_state == SystemState.RECONCILING
        assert db.get_intent(cid)["status"] == "UNKNOWN"
        assert risk.reservations == Decimal("1")
    finally:
        db.close()


def test_ws_trade_cumulative_mismatch_fails_closed_without_persistence_overcount():
    db, risk, execution, cid = _engine_with_order(
        {"status": "CANCELED", "orderId": "unused", "executedQty": "0"},
        side="BUY", qty="1")
    try:
        execution.handle_order_update({
            "client_order_id": cid,
            "execution_type": "TRADE",
            "trade_id": "bad-cumulative",
            "last_filled_qty": Decimal("0.6"),
            "last_filled_price": Decimal("100"),
            "accumulated_filled_qty": Decimal("0.8"),
            "mapped_state": OrderState.PARTIALLY_FILLED,
            "order_status": "PARTIALLY_FILLED",
        })
        assert risk.system_state == SystemState.RECONCILING
        assert Decimal(db.get_intent(cid)["filled_quantity"]) == Decimal("0")
        assert db.get_intent(cid)["status"] == "UNKNOWN"
    finally:
        db.close()
