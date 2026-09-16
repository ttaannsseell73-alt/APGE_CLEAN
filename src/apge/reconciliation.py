from typing import Dict, Any, List, Optional
from decimal import Decimal

from apge.persistence import Persistence
from apge.binance_adapter import BinanceAdapter
from apge.simulator import OrderState, RiskEngine

class Reconciler:
    def __init__(self, persistence: Persistence, adapter: BinanceAdapter, risk_engine: Optional[RiskEngine] = None):
        self.persistence = persistence
        self.adapter = adapter
        self.risk_engine = risk_engine
        self.last_position_amount = Decimal("0")

    def fetch_exchange_state(self, symbol: str) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Fetches current position and open orders for the symbol from the exchange.
        Raises exception on network failure.
        """
        positions_response = self.adapter.get_positions()
        # Find the specific position
        position = next((p for p in positions_response if p.get("symbol") == symbol), None)
        if position is None:
            # Construct a default zero position
            position = {
                "symbol": symbol,
                "positionAmt": "0.0",
                "entryPrice": "0.0"
            }

        open_orders_response = self.adapter.get_open_orders(symbol=symbol)

        return position, open_orders_response

    def resolve_state(self, symbol: str) -> bool:
        """
        Attempts to reconcile local state with exchange state.
        Returns True if successful, False if a corrupt/unrecoverable state is detected (should transition to HALTED).
        """
        try:
            position, exchange_orders = self.fetch_exchange_state(symbol)
        except Exception:
            # If network fails, we can't reconcile
            return False

        try:
            self.last_position_amount = Decimal(str(position.get("positionAmt", "0")))
            if self.last_position_amount.is_nan() or self.last_position_amount.is_infinite():
                return False
        except Exception:
            return False

        exchange_orders_by_cid = {order["clientOrderId"]: order for order in exchange_orders}

        # 1. Handle UNKNOWN orders first by explicitly querying them
        all_intents = self.persistence.get_all_intents()
        for intent in all_intents:
            if intent["status"] == "UNKNOWN":
                cid = intent["client_order_id"]
                try:
                    order_info = self.adapter.query_order(symbol, cid)
                    # If query successful, it exists. Update status based on Binance status
                    mapped_state = self.adapter._map_order_state(order_info["status"])
                    if mapped_state == OrderState.UNKNOWN:
                        return False

                    # Update fill information if any
                    executed_qty = Decimal(str(order_info.get("executedQty", "0")))
                    if executed_qty > Decimal("0"):
                        # Just append a singular fill for the total amount as a reconciliation simplification.
                        # Real event processing handles multiple fills cleanly.
                        self.persistence.add_fill(f"reconcile_fill_{cid}", cid, executed_qty, Decimal(str(order_info.get("avgPrice", "0"))))

                    self.persistence.update_intent_status(cid, mapped_state, str(order_info.get("orderId")))
                    # Update local exchange_orders dict if it's still open
                    if mapped_state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                        exchange_orders_by_cid[cid] = order_info
                except Exception as e:
                    # Depending on error, it might be that the order doesn't exist
                    # For Binance, "Order does not exist" is a specific code (-2013)
                    error_msg = str(e)
                    if "-2013" in error_msg or "Order does not exist" in error_msg:
                        # Order was not placed, safely mark as CANCELED or REJECTED
                        self.persistence.update_intent_status(cid, OrderState.CANCELED)
                    else:
                        # Unexpected error, cannot reconcile safely
                        return False

        # 2. Check all active local intents against exchange
        active_intents = self.persistence.get_active_intents()
        for intent in active_intents:
            cid = intent["client_order_id"]
            if cid not in exchange_orders_by_cid:
                # Local thinks it's open, but exchange doesn't have it
                # We should query it to know its final state (filled vs canceled)
                try:
                    order_info = self.adapter.query_order(symbol, cid)
                    mapped_state = self.adapter._map_order_state(order_info["status"])
                    if mapped_state == OrderState.UNKNOWN:
                        return False

                    executed_qty = Decimal(str(order_info.get("executedQty", "0")))
                    current_filled = Decimal(intent["filled_quantity"])
                    if executed_qty > current_filled:
                        diff = executed_qty - current_filled
                        self.persistence.add_fill(f"reconcile_fill_missing_{cid}", cid, diff, Decimal(str(order_info.get("avgPrice", "0"))))

                    self.persistence.update_intent_status(cid, mapped_state, str(order_info.get("orderId")))
                except Exception as e:
                    error_msg = str(e)
                    if "-2013" in error_msg or "Order does not exist" in error_msg:
                        # Local thinks it's open, but it never existed? This is a corrupt state
                        # OR it was canceled locally but db write failed.
                        # We will fail closed/halt.
                        return False
                    else:
                        return False
            else:
                # It is on exchange, and we think it's open. Sync quantities.
                order_info = exchange_orders_by_cid[cid]
                mapped_state = self.adapter._map_order_state(order_info["status"])
                executed_qty = Decimal(str(order_info.get("executedQty", "0")))
                current_filled = Decimal(intent["filled_quantity"])

                if executed_qty > current_filled:
                    diff = executed_qty - current_filled
                    self.persistence.add_fill(f"reconcile_fill_active_{cid}", cid, diff, Decimal(str(order_info.get("avgPrice", "0"))))

                self.persistence.update_intent_status(cid, mapped_state, str(order_info.get("orderId")))

        # 3. Check for exchange-only orders (orders on exchange that we don't track)
        local_cids = {intent["client_order_id"] for intent in self.persistence.get_all_intents()}
        for cid, order in exchange_orders_by_cid.items():
            if cid not in local_cids:
                return False

        # 4. Rebuild the in-memory RiskEngine from the exact reconciled snapshot.
        if self.risk_engine is not None:
            intents = self.persistence.get_all_intents()
            for intent in intents:
                intent["observed_filled_quantity"] = str(
                    self.persistence.get_recorded_fill_total(intent["client_order_id"]))
            if not self.risk_engine.rebuild_from_authoritative_state(
                    self.last_position_amount, intents):
                return False

        return True
