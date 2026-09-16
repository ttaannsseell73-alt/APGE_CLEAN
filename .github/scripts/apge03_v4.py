from pathlib import Path


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"{label}: expected 1 match, got {n}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Reconciler: one authoritative path for cumulative fill/status synchronization.
# ---------------------------------------------------------------------------
Path('src/apge/reconciliation.py').write_text('''from typing import Dict, Any, List, Optional
from decimal import Decimal

from apge.persistence import Persistence
from apge.binance_adapter import BinanceAdapter
from apge.simulator import OrderState, RiskEngine


class Reconciler:
    def __init__(self, persistence: Persistence, adapter: BinanceAdapter,
                 risk_engine: Optional[RiskEngine] = None):
        self.persistence = persistence
        self.adapter = adapter
        self.risk_engine = risk_engine
        self.last_position_amount = Decimal("0")

    def fetch_exchange_state(self, symbol: str) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        positions_response = self.adapter.get_positions()
        position = next((p for p in positions_response if p.get("symbol") == symbol), None)
        if position is None:
            position = {"symbol": symbol, "positionAmt": "0.0", "entryPrice": "0.0"}
        return position, self.adapter.get_open_orders(symbol=symbol)

    @staticmethod
    def _finite_decimal(value: Any) -> Optional[Decimal]:
        try:
            result = Decimal(str(value))
        except Exception:
            return None
        if result.is_nan() or result.is_infinite():
            return None
        return result

    def _sync_intent_from_order_info(self, intent: Dict[str, Any], order_info: Dict[str, Any]) -> bool:
        """Synchronize one local intent from an authoritative exchange snapshot.

        No synthetic fills are invented. REST cumulative executedQty is treated as
        authoritative state; independently observed websocket fills remain in the
        fills table and are used only for duplicate/late-fill conflict detection.
        """
        cid = intent["client_order_id"]
        symbol = intent["symbol"]

        if order_info.get("clientOrderId") not in (None, cid):
            return False
        if order_info.get("symbol") not in (None, symbol):
            return False

        mapped_state = self.adapter._map_order_state(str(order_info.get("status", "UNKNOWN")))
        if mapped_state == OrderState.UNKNOWN:
            return False

        qty = self._finite_decimal(intent.get("quantity"))
        persisted = self._finite_decimal(intent.get("filled_quantity", "0"))
        executed = self._finite_decimal(order_info.get("executedQty"))
        if qty is None or qty <= 0 or persisted is None or executed is None:
            return False
        if persisted < 0 or executed < 0 or persisted > qty or executed > qty:
            return False
        # Exchange cumulative quantity must never move backwards relative to
        # already-persisted authoritative state.
        if executed < persisted:
            return False

        if mapped_state == OrderState.OPEN and executed != 0:
            return False
        if mapped_state == OrderState.PARTIALLY_FILLED and not (Decimal("0") < executed < qty):
            return False
        if mapped_state == OrderState.FILLED and executed != qty:
            return False

        avg_price = self._finite_decimal(order_info.get("avgPrice", "0"))
        if avg_price is not None and avg_price < 0:
            return False
        self.persistence.sync_filled_quantity(
            cid, executed, avg_price if avg_price is not None else None)
        self.persistence.update_intent_status(
            cid,
            mapped_state,
            str(order_info["orderId"]) if order_info.get("orderId") is not None else intent.get("exchange_order_id"),
            raw_status=str(order_info.get("status", "UNKNOWN")),
        )
        return True

    def resolve_state(self, symbol: str) -> bool:
        """Reconcile persistence, exchange and RiskEngine; fail closed on ambiguity."""
        try:
            position, exchange_orders = self.fetch_exchange_state(symbol)
        except Exception:
            return False

        position_amount = self._finite_decimal(position.get("positionAmt", "0"))
        if position_amount is None:
            return False
        self.last_position_amount = position_amount

        # Duplicate/invalid client ids in one exchange snapshot are treated as
        # corrupt/ambiguous rather than silently overwritten by a dict.
        exchange_orders_by_cid: Dict[str, Dict[str, Any]] = {}
        for order in exchange_orders:
            cid = order.get("clientOrderId")
            if not cid or cid in exchange_orders_by_cid:
                return False
            exchange_orders_by_cid[cid] = order

        local_symbol_intents = [
            intent for intent in self.persistence.get_all_intents()
            if intent.get("symbol") == symbol
        ]

        # 1. Resolve UNKNOWN submissions individually.
        for intent in local_symbol_intents:
            if intent["status"] != "UNKNOWN":
                continue
            cid = intent["client_order_id"]
            try:
                order_info = self.adapter.query_order(symbol, cid)
            except Exception as exc:
                text = str(exc)
                if "-2013" in text or "Order does not exist" in text:
                    # For a submission whose outcome was UNKNOWN, an explicit
                    # exchange "does not exist" result is accepted only if we
                    # have never persisted a fill for it.
                    persisted = self._finite_decimal(intent.get("filled_quantity", "0"))
                    observed = self.persistence.get_recorded_fill_total(cid)
                    if persisted != Decimal("0") or observed != Decimal("0"):
                        return False
                    self.persistence.update_intent_status(cid, OrderState.CANCELED, raw_status="NOT_FOUND")
                    exchange_orders_by_cid.pop(cid, None)
                    continue
                return False

            if not self._sync_intent_from_order_info(intent, order_info):
                return False
            mapped = self.adapter._map_order_state(str(order_info.get("status", "UNKNOWN")))
            if mapped in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                exchange_orders_by_cid[cid] = order_info
            else:
                exchange_orders_by_cid.pop(cid, None)

        # 2. Every locally active intent must match an authoritative exchange
        # open-order snapshot or a direct query.
        active_intents = [
            intent for intent in self.persistence.get_active_intents()
            if intent.get("symbol") == symbol
        ]
        for intent in active_intents:
            cid = intent["client_order_id"]
            if cid in exchange_orders_by_cid:
                order_info = exchange_orders_by_cid[cid]
            else:
                try:
                    order_info = self.adapter.query_order(symbol, cid)
                except Exception:
                    return False
            if not self._sync_intent_from_order_info(intent, order_info):
                return False

        # Refresh local view after UNKNOWN/active synchronization.
        local_symbol_intents = [
            intent for intent in self.persistence.get_all_intents()
            if intent.get("symbol") == symbol
        ]
        local_cids = {intent["client_order_id"] for intent in local_symbol_intents}

        # 3. Exchange-only orders are not ours to cancel or assume safe.
        for cid in exchange_orders_by_cid:
            if cid not in local_cids:
                return False

        # 4. Rebuild RiskEngine from the same reconciled symbol snapshot.
        if self.risk_engine is not None:
            for intent in local_symbol_intents:
                intent["observed_filled_quantity"] = str(
                    self.persistence.get_recorded_fill_total(intent["client_order_id"]))
            if not self.risk_engine.rebuild_from_authoritative_state(
                    self.last_position_amount, local_symbol_intents):
                return False

        return True
''')


# ---------------------------------------------------------------------------
# RiskEngine resolve_order: complete authoritative state handling.
# ---------------------------------------------------------------------------
p = Path('src/apge/simulator.py')
s = p.read_text()
start = s.index('    def resolve_order(self, order_id: str, final_state: OrderState,\n')
end = s.index('    def resolve_conflict(self, order_id: str, final_filled: Decimal):\n', start)
replacement = '''    def resolve_order(self, order_id: str, final_state: OrderState,
                      final_filled: Decimal) -> bool:
        """Resolve an UNKNOWN order from authoritative exchange state."""
        with self._lock:
            if not self._is_valid_non_negative(final_filled):
                self._enter_reconciling()
                return False
            if order_id not in self.orders:
                self._enter_reconciling()
                return False

            order = self.orders[order_id]
            if order.state != OrderState.UNKNOWN:
                return order.state == final_state and order.filled_amount == final_filled
            if final_filled > order.initial_amount or final_filled < order.filled_amount:
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                self._bump_risk_version()
                return False

            if final_state == OrderState.OPEN:
                valid_state = final_filled == Decimal('0')
            elif final_state == OrderState.PARTIALLY_FILLED:
                valid_state = Decimal('0') < final_filled < order.initial_amount
            elif final_state == OrderState.FILLED:
                valid_state = final_filled == order.initial_amount
            elif final_state == OrderState.CANCELED:
                valid_state = Decimal('0') <= final_filled <= order.initial_amount
            else:
                valid_state = False

            if not valid_state:
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                self._bump_risk_version()
                return False

            diff = final_filled - order.filled_amount
            if diff > 0:
                order.filled_amount += diff
                self.current_position += self._position_delta(order, diff)
                self.reservations -= diff

            if final_state == OrderState.CANCELED:
                order.cancel_requested = True
                order.cancel_confirmed_total = final_filled
                remaining = order.initial_amount - order.filled_amount
                if remaining > 0:
                    self.reservations -= remaining

            order.state = final_state
            if self.reservations < 0:
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                return False
            self._bump_risk_version()
            return True

'''
s = s[:start] + replacement + s[end:]
p.write_text(s)


# ---------------------------------------------------------------------------
# BinanceAdapter: server time synchronization must affect signed timestamps.
# ---------------------------------------------------------------------------
p = Path('src/apge/binance_adapter.py')
s = p.read_text()
s = once(
    s,
    '        self.clock = clock or time.time\n',
    '        self.clock = clock or time.time\n        self.server_time_offset = 0\n',
    'adapter server offset init')
s = once(
    s,
    '            params_copy["timestamp"] = int(self.clock() * 1000)\n',
    '            params_copy["timestamp"] = int(self.clock() * 1000) + int(self.server_time_offset)\n',
    'adapter server offset use')
p.write_text(s)


# ---------------------------------------------------------------------------
# ExecutionEngine: authoritative submit acknowledgement and WS validation.
# ---------------------------------------------------------------------------
p = Path('src/apge/execution_engine.py')
s = p.read_text()
old = '''        # Parse success
        if "orderId" in response:
            mapped_state = self.adapter._map_order_state(raw_status)
            self.persistence.update_intent_status(cid, mapped_state, str(response["orderId"]), raw_status=raw_status)
            # Provide feedback to RiskEngine about the actual state to clear the UNKNOWN gate
            self.risk_engine.resolve_order(cid, mapped_state, Decimal("0.0"))
            return cid

        # It was rejected by the exchange
'''
new = '''        # Parse an authoritative acknowledgement. Even a response containing
        # orderId is fail-closed if its status/quantity is internally inconsistent.
        if "orderId" in response:
            mapped_state = self.adapter._map_order_state(raw_status)
            final_filled = self._authoritative_filled_qty(response.get("executedQty"))
            if mapped_state == OrderState.UNKNOWN or final_filled is None:
                self.persistence.update_intent_status(
                    cid, OrderState.UNKNOWN, str(response["orderId"]), raw_status=raw_status)
                self.risk_engine.restore_connection()
                return cid

            if not self.risk_engine.resolve_order(cid, mapped_state, final_filled):
                self.persistence.update_intent_status(
                    cid, OrderState.UNKNOWN, str(response["orderId"]), raw_status=raw_status)
                self.risk_engine.restore_connection()
                return cid

            if not self._sync_authoritative_fill_total(
                    cid, final_filled, response.get("avgPrice")):
                self.persistence.update_intent_status(
                    cid, OrderState.UNKNOWN, str(response["orderId"]), raw_status=raw_status)
                return cid

            self.persistence.update_intent_status(
                cid, mapped_state, str(response["orderId"]), raw_status=raw_status)
            return cid

        # It was rejected by the exchange
'''
s = once(s, old, new, 'authoritative submit response')
s = once(
    s,
    '        self.persistence.update_intent_status(cid, OrderState.UNKNOWN, raw_status=raw_status)\n        return None\n',
    '        self.persistence.update_intent_status(cid, OrderState.UNKNOWN, raw_status=raw_status)\n        self.risk_engine.restore_connection()\n        return None\n',
    'rejected submission fail closed')

# Add pre-validation before persistence mutation for TRADE events.
old = '''                if filled_qty > Decimal("0"):
                    # This handles duplicates internally by checking fill_id
                    fill_applied = self.persistence.add_fill(
'''
new = '''                if filled_qty > Decimal("0"):
                    try:
                        total_qty = Decimal(str(intent["quantity"]))
                        persisted_filled = Decimal(str(intent["filled_quantity"]))
                        cumulative = self._authoritative_filled_qty(update.get("accumulated_filled_qty"))
                    except Exception:
                        cumulative = None
                        total_qty = Decimal("-1")
                        persisted_filled = Decimal("-1")

                    expected_cumulative = persisted_filled + filled_qty
                    if (filled_qty.is_nan() or filled_qty.is_infinite() or
                            total_qty <= 0 or persisted_filled < 0 or
                            expected_cumulative > total_qty or cumulative is None or
                            cumulative != expected_cumulative):
                        self.persistence.update_intent_status(
                            cid, OrderState.UNKNOWN, intent.get("exchange_order_id"), raw_status=raw_status)
                        # Let RiskEngine record an overfill conflict when applicable,
                        # then force reconciliation for all other inconsistencies.
                        self.risk_engine.on_fill(
                            order_id=cid, fill_id=str(trade_id), amount=filled_qty)
                        self.risk_engine.restore_connection()
                        return

                    # This handles duplicates internally by checking fill_id
                    fill_applied = self.persistence.add_fill(
'''
s = once(s, old, new, 'ws trade cumulative validation')
p.write_text(s)


# ---------------------------------------------------------------------------
# Persistence add_fill: input bounds are enforced before any DB mutation.
# ---------------------------------------------------------------------------
p = Path('src/apge/persistence.py')
s = p.read_text()
start = s.index('    def add_fill(self, fill_id: str, client_order_id: str, quantity: Decimal, price: Decimal):\n')
end = s.index('    def sync_filled_quantity(', start)
replacement = '''    def add_fill(self, fill_id: str, client_order_id: str, quantity: Decimal, price: Decimal):
        with self.conn:
            if (not isinstance(quantity, Decimal) or quantity.is_nan() or
                    quantity.is_infinite() or quantity <= 0):
                return False
            if (not isinstance(price, Decimal) or price.is_nan() or
                    price.is_infinite() or price < 0):
                return False

            if self.conn.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)).fetchone():
                return False

            intent_row = self.conn.execute(
                "SELECT quantity, filled_quantity, average_price FROM intents WHERE client_order_id = ?",
                (client_order_id,)).fetchone()
            if not intent_row:
                return False

            order_qty = Decimal(intent_row['quantity'])
            old_filled = Decimal(intent_row['filled_quantity'])
            old_avg_price = Decimal(intent_row['average_price'])
            new_filled = old_filled + quantity
            if new_filled > order_qty:
                return False

            self.conn.execute("""
                INSERT INTO fills (fill_id, client_order_id, quantity, price)
                VALUES (?, ?, ?, ?)
            """, (fill_id, client_order_id, str(quantity), str(price)))

            total_value = (old_avg_price * old_filled) + (price * quantity)
            new_avg_price = total_value / new_filled if new_filled > 0 else Decimal('0')
            self.conn.execute("""
                UPDATE intents SET filled_quantity = ?, average_price = ?, updated_at = CURRENT_TIMESTAMP
                WHERE client_order_id = ?
            """, (str(new_filled), str(new_avg_price), client_order_id))
            return True

'''
s = s[:start] + replacement + s[end:]
p.write_text(s)


# ---------------------------------------------------------------------------
# Tests for the newly closed gaps.
# ---------------------------------------------------------------------------
p = Path('tests/test_apge03_hardening.py')
s = p.read_text()
if 'test_resolve_order_supports_partial_and_rejects_incoherent_state' not in s:
    s += '''\n\n
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
'''
p.write_text(s)

p = Path('tests/test_offline_e2e_reconciler.py')
s = p.read_text()
if 'test_reconciliation_cumulative_fill_does_not_double_count_existing_fill' not in s:
    s += '''\n\n
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
'''
p.write_text(s)
