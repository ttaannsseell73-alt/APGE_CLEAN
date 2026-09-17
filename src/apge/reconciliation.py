from typing import Dict, Any, List, Optional
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
