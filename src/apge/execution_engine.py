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

    def _generate_client_order_id(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> str:
        # Make deterministic based on intent, so retrying the exact same intent yields the same CID.
        # This prevents duplicate orders on retry.
        unique_string = f"{symbol}_{side}_{quantity}_{price}_{self.risk_engine.get_time()}"
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
        """
        Attempts to cancel an order. Returns True if cancellation was requested.
        """
        intent = self.persistence.get_intent(client_order_id)
        if not intent:
            return False

        if intent["status"] in ("FILLED", "CANCELED", "REJECTED"):
            return False

        response = self.adapter.cancel_order(symbol, client_order_id)
        raw_status = response.get("status", "UNKNOWN")

        if "orderId" in response:
            mapped_state = self.adapter._map_order_state(raw_status)
            self.persistence.update_intent_status(client_order_id, mapped_state, str(response["orderId"]), raw_status=raw_status)
            return True

        return False

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
