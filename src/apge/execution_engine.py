import uuid
from typing import Dict, Any, List, Optional
from decimal import Decimal
import logging
import hashlib

from apge.persistence import Persistence
from apge.binance_adapter import BinanceAdapter
from apge.simulator import RiskEngine, OrderState, SystemState
from apge.grid_strategy import OrderProposal

logger = logging.getLogger(__name__)

class ExecutionEngine:
    """
    Execution layer that converts approved OrderProposal objects into exchange intents.
    Manages deterministic clientOrderId, intent tracking, submit/cancel state machine.
    """
    def __init__(self, persistence: Persistence, adapter: BinanceAdapter, risk_engine: RiskEngine):
        self.persistence = persistence
        self.adapter = adapter
        self.risk_engine = risk_engine

    @staticmethod
    def _authoritative_filled_qty(value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            qty = Decimal(str(value))
        except Exception:
            return None
        if qty.is_nan() or qty.is_infinite() or qty < Decimal("0"):
            return None
        return qty

    def _sync_authoritative_fill_total(self, client_order_id: str, final_filled: Decimal, average_price: Any = None) -> bool:
        intent = self.persistence.get_intent(client_order_id)
        if not intent:
            return False
        try:
            persisted = Decimal(str(intent.get("filled_quantity", "0")))
        except Exception:
            self.risk_engine.restore_connection()
            return False
        if final_filled < persisted:
            self.risk_engine.restore_connection()
            return False
        avg = None
        if average_price is not None:
            try:
                candidate = Decimal(str(average_price))
                if not candidate.is_nan() and not candidate.is_infinite() and candidate >= 0:
                    avg = candidate
            except Exception:
                avg = None
        self.persistence.sync_filled_quantity(client_order_id, final_filled, avg)
        return True

    def _generate_client_order_id(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> str:
        # Make deterministic based on intent and the number of PRIOR terminal orders matching this intent.
        # This prevents duplicate orders on retry (because the active order count wouldn't change if the previous failed to submit),
        # but allows new identical intents if a previous one filled or was canceled.
        intents = self.persistence.get_all_intents()
        terminal_count = sum(1 for i in intents
                             if i["symbol"] == symbol
                             and i["side"] == side
                             and Decimal(i["quantity"]) == quantity
                             and Decimal(i["price"]) == price
                             and i["status"] in ("FILLED", "CANCELED", "REJECTED"))

        unique_string = f"{symbol}_{side}_{quantity}_{price}_{terminal_count}"
        hashed = hashlib.sha256(unique_string.encode('utf-8')).hexdigest()[:20]
        return f"APGE_{hashed}"

    def execute_proposal(self, symbol: str, proposal: OrderProposal) -> Optional[str]:
        """
        Attempts to execute a proposal by obtaining Risk Engine approval and submitting.
        Returns the client_order_id if accepted (or UNKNOWN), None if rejected by risk or network.
        """
        if self.risk_engine.system_state != SystemState.OPERATIONAL:
            logger.warning("Attempted to execute proposal while not OPERATIONAL.")
            return None

        # 1. Ask Risk Engine for approval (MUST NOT BE BYPASSED)
        approval = self.risk_engine.request_approval(amount=proposal.quantity, symbol=symbol, side=proposal.side)
        if not approval:
            logger.warning(f"RiskEngine rejected proposal: {symbol} {proposal.side} {proposal.quantity}")
            return None

        cid = self._generate_client_order_id(symbol, proposal.side, proposal.quantity, proposal.price)

        # If we already have an active intent with this CID, it means we are retrying a submission
        # that perhaps returned UNKNOWN and we are now trying again?
        # Actually, if it's UNKNOWN, the system is in RECONCILING so we wouldn't reach here.
        # But if it's identical intent, we just return the CID if it exists to avoid re-submitting duplicate to adapter.
        existing_intent = self.persistence.get_intent(cid)
        if existing_intent:
            logger.info(f"Intent {cid} already exists locally. Skipping submission to avoid duplicates.")
            # Consume the approval anyway so it doesn't hang around, but we don't submit.
            self.risk_engine.consume_approval_and_submit(approval, cid, symbol=symbol, side=proposal.side)
            return cid

        # 2. Consume approval atomically with order creation in risk engine
        if not self.risk_engine.consume_approval_and_submit(approval, cid, symbol=symbol, side=proposal.side):
            logger.warning(f"RiskEngine failed to consume approval for {cid}")
            return None

        # Save intent BEFORE network call
        self.persistence.save_intent(
            client_order_id=cid,
            symbol=symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            price=proposal.price,
            status=OrderState.OPEN
        )

        # Submit to exchange
        response = self.adapter.submit_limit_order(
            symbol=symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            price=proposal.price,
            client_order_id=cid
        )

        raw_status = response.get("status", "UNKNOWN")

        # Adapter returns UNKNOWN on timeout
        if raw_status == "UNKNOWN":
            logger.warning(f"Submit timeout for {cid}, setting to UNKNOWN")
            self.persistence.update_intent_status(cid, OrderState.UNKNOWN, raw_status=raw_status)
            # If UNKNOWN, we MUST transition system state to RECONCILING to stop risk-increasing activity
            self.risk_engine.system_state = SystemState.RECONCILING
            # We return the cid so the caller knows it was attempted.
            return cid

        # Parse success
        if "orderId" in response:
            mapped_state = self.adapter._map_order_state(raw_status)
            self.persistence.update_intent_status(cid, mapped_state, str(response["orderId"]), raw_status=raw_status)
            # Provide feedback to RiskEngine about the actual state to clear the UNKNOWN gate
            self.risk_engine.resolve_order(cid, mapped_state, Decimal("0.0"))
            return cid

        # It was rejected by the exchange
        logger.warning(f"Order rejected by exchange: {response}")
        # Note: We must preserve order-state semantics. Never map REJECTED to CANCELED.
        # But Simulator OrderState does not have REJECTED.
        # The prompt says: "Preserve order-state semantics strictly: never map REJECTED to CANCELED. Map to UNKNOWN or preserve raw status if the target enum lacks a specific REJECTED state."
        # So we map to UNKNOWN since OrderState doesn't have REJECTED.
        self.persistence.update_intent_status(cid, OrderState.UNKNOWN, raw_status=raw_status)
        return None

    def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        """Request cancel; terminal local state requires an authoritative cumulative fill total."""
        intent = self.persistence.get_intent(client_order_id)
        if not intent:
            return False
        if intent["status"] in ("FILLED", "CANCELED", "REJECTED"):
            return False

        self.risk_engine.request_cancel(client_order_id)
        try:
            response = self.adapter.cancel_order(symbol, client_order_id)
        except Exception:
            self.risk_engine.restore_connection()
            raise

        raw_status = response.get("status", "UNKNOWN")
        if "orderId" not in response:
            self.risk_engine.restore_connection()
            self.persistence.update_intent_status(client_order_id, OrderState.UNKNOWN, raw_status=raw_status)
            return False

        exchange_order_id = str(response["orderId"])
        mapped_state = self.adapter._map_order_state(raw_status)
        if mapped_state != OrderState.CANCELED:
            self.persistence.update_intent_status(
                client_order_id, OrderState.UNKNOWN, exchange_order_id, raw_status=raw_status)
            self.risk_engine.restore_connection()
            return False

        final_filled = self._authoritative_filled_qty(response.get("executedQty"))
        if final_filled is None:
            self.persistence.update_intent_status(
                client_order_id, OrderState.UNKNOWN, exchange_order_id, raw_status=raw_status)
            self.risk_engine.restore_connection()
            return True

        if not self.risk_engine.on_cancel_confirmed(client_order_id, final_filled):
            self.persistence.update_intent_status(
                client_order_id, OrderState.UNKNOWN, exchange_order_id, raw_status=raw_status)
            return True

        if not self._sync_authoritative_fill_total(
                client_order_id, final_filled, response.get("avgPrice")):
            self.persistence.update_intent_status(
                client_order_id, OrderState.UNKNOWN, exchange_order_id, raw_status=raw_status)
            return True

        self.persistence.update_intent_status(
            client_order_id, OrderState.CANCELED, exchange_order_id, raw_status=raw_status)
        return True

    def handle_order_update(self, update: Dict[str, Any]):
        """
        Processes an ORDER_TRADE_UPDATE websocket event.
        Idempotent handling of fills and cancel races.
        """
        cid = update["client_order_id"]
        intent = self.persistence.get_intent(cid)

        if not intent:
            # We don't know about this order, ignore it or log it
            logger.warning(f"Received update for unknown order: {cid}")
            return

        mapped_state = update["mapped_state"]
        raw_status = update["order_status"]

        # If it's a fill, apply it safely
        if update["execution_type"] == "TRADE":
            trade_id = update.get("trade_id")
            if trade_id:
                filled_qty = update["last_filled_qty"]
                filled_price = update["last_filled_price"]

                if filled_qty > Decimal("0"):
                    # This handles duplicates internally by checking fill_id
                    fill_applied = self.persistence.add_fill(
                        fill_id=f"ws_fill_{trade_id}",
                        client_order_id=cid,
                        quantity=filled_qty,
                        price=filled_price
                    )
                    if fill_applied:
                        logger.info(f"Applied fill {filled_qty} @ {filled_price} for {cid}")
                        # Tell risk engine to release risk for this fill amount
                        self.risk_engine.on_fill(order_id=cid, fill_id=str(trade_id), amount=filled_qty)

        # A CANCELED websocket update is terminal only with a consistent
        # authoritative cumulative fill quantity.
        if mapped_state == OrderState.CANCELED:
            final_filled = self._authoritative_filled_qty(
                update.get("accumulated_filled_qty"))
            if final_filled is None:
                self.persistence.update_intent_status(
                    cid, OrderState.UNKNOWN, intent.get("exchange_order_id"), raw_status=raw_status)
                self.risk_engine.restore_connection()
                return
            if not self.risk_engine.on_cancel_confirmed(cid, final_filled):
                self.persistence.update_intent_status(
                    cid, OrderState.UNKNOWN, intent.get("exchange_order_id"), raw_status=raw_status)
                return
            if not self._sync_authoritative_fill_total(
                    cid, final_filled, update.get("average_price")):
                self.persistence.update_intent_status(
                    cid, OrderState.UNKNOWN, intent.get("exchange_order_id"), raw_status=raw_status)
                return
            self.persistence.update_intent_status(
                cid, OrderState.CANCELED, intent.get("exchange_order_id"), raw_status=raw_status)
            return

        # Regardless of fill, update the final order state
        # But handle race condition: if it was already FILLED locally, a delayed CANCELED shouldn't overwrite it
        # Actually Binance guarantees order events are sequential per order.
        # But just in case, don't revert terminal states to non-terminal states.

        current_status = intent["status"]
        if current_status == "FILLED" and mapped_state != OrderState.FILLED:
            # Cannot un-fill an order
            pass
        elif current_status == "CANCELED" and mapped_state == OrderState.FILLED:
            # Race condition: We thought it was canceled, but it actually filled.
            # Update to FILLED to reflect reality.
            self.persistence.update_intent_status(cid, mapped_state, intent.get("exchange_order_id"), raw_status=raw_status)
        else:
            # Normal update
            self.persistence.update_intent_status(cid, mapped_state, intent.get("exchange_order_id"), raw_status=raw_status)
