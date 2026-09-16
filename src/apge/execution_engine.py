from typing import Dict, Any, Optional
from decimal import Decimal
import logging
import hashlib

from apge.persistence import Persistence
from apge.binance_adapter import BinanceAdapter
from apge.simulator import RiskEngine, OrderState, SystemState
from apge.grid_strategy import OrderProposal

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Risk-gated execution layer for deterministic APGE order intents."""

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

    @staticmethod
    def _bool_value(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1"):
                return True
            if normalized in ("false", "0"):
                return False
        return None

    def _sync_authoritative_fill_total(
        self,
        client_order_id: str,
        final_filled: Decimal,
        average_price: Any = None,
    ) -> bool:
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

    def _generate_client_order_id(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        reduce_only: bool = False,
    ) -> str:
        """Generate an idempotent CID with reduce-only semantics in its identity."""
        intents = self.persistence.get_all_intents()
        terminal_count = sum(
            1
            for intent in intents
            if intent["symbol"] == symbol
            and intent["side"] == side
            and Decimal(intent["quantity"]) == quantity
            and Decimal(intent["price"]) == price
            and bool(int(intent.get("reduce_only", 0))) == bool(reduce_only)
            and intent["status"] in ("FILLED", "CANCELED", "REJECTED")
        )
        unique_string = (
            f"{symbol}_{side}_{quantity}_{price}_{int(bool(reduce_only))}_{terminal_count}"
        )
        hashed = hashlib.sha256(unique_string.encode("utf-8")).hexdigest()[:20]
        return f"APGE_{hashed}"

    def execute_proposal(self, symbol: str, proposal: OrderProposal) -> Optional[str]:
        """Risk-approve, persist before network, then submit one LIMIT intent."""
        if self.risk_engine.system_state != SystemState.OPERATIONAL:
            logger.warning("Attempted to execute proposal while not OPERATIONAL.")
            return None

        approval = self.risk_engine.request_approval(
            amount=proposal.quantity,
            symbol=symbol,
            side=proposal.side,
        )
        if not approval:
            logger.warning(
                "RiskEngine rejected proposal: %s %s %s",
                symbol,
                proposal.side,
                proposal.quantity,
            )
            return None

        cid = self._generate_client_order_id(
            symbol,
            proposal.side,
            proposal.quantity,
            proposal.price,
            proposal.reduce_only,
        )

        existing_intent = self.persistence.get_intent(cid)
        if existing_intent:
            logger.info("Intent %s already exists locally; network submit skipped.", cid)
            self.risk_engine.consume_approval_and_submit(
                approval,
                cid,
                symbol=symbol,
                side=proposal.side,
            )
            return cid

        if not self.risk_engine.consume_approval_and_submit(
            approval,
            cid,
            symbol=symbol,
            side=proposal.side,
        ):
            logger.warning("RiskEngine failed to consume approval for %s", cid)
            return None

        self.persistence.save_intent(
            client_order_id=cid,
            symbol=symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            price=proposal.price,
            status=OrderState.OPEN,
            reduce_only=proposal.reduce_only,
        )

        response = self.adapter.submit_limit_order(
            symbol=symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            price=proposal.price,
            client_order_id=cid,
            reduce_only=proposal.reduce_only,
        )
        raw_status = response.get("status", "UNKNOWN")

        if raw_status == "UNKNOWN":
            logger.warning("Submit timeout for %s; entering reconciliation.", cid)
            self.persistence.update_intent_status(
                cid, OrderState.UNKNOWN, raw_status=raw_status)
            self.risk_engine.system_state = SystemState.RECONCILING
            return cid

        if "orderId" in response:
            mapped_state = self.adapter._map_order_state(raw_status)
            final_filled = self._authoritative_filled_qty(response.get("executedQty"))
            response_reduce_only = self._bool_value(response.get("reduceOnly"))
            if response_reduce_only is not None and response_reduce_only != proposal.reduce_only:
                self.persistence.update_intent_status(
                    cid,
                    OrderState.UNKNOWN,
                    str(response["orderId"]),
                    raw_status=raw_status,
                )
                self.risk_engine.restore_connection()
                return cid
            if mapped_state == OrderState.UNKNOWN or final_filled is None:
                self.persistence.update_intent_status(
                    cid,
                    OrderState.UNKNOWN,
                    str(response["orderId"]),
                    raw_status=raw_status,
                )
                self.risk_engine.restore_connection()
                return cid
            if not self.risk_engine.resolve_order(cid, mapped_state, final_filled):
                self.persistence.update_intent_status(
                    cid,
                    OrderState.UNKNOWN,
                    str(response["orderId"]),
                    raw_status=raw_status,
                )
                self.risk_engine.restore_connection()
                return cid
            if not self._sync_authoritative_fill_total(
                cid, final_filled, response.get("avgPrice")
            ):
                self.persistence.update_intent_status(
                    cid,
                    OrderState.UNKNOWN,
                    str(response["orderId"]),
                    raw_status=raw_status,
                )
                return cid
            self.persistence.update_intent_status(
                cid,
                mapped_state,
                str(response["orderId"]),
                raw_status=raw_status,
            )
            return cid

        logger.warning("Order rejected/ambiguous at exchange for CID %s", cid)
        self.persistence.update_intent_status(
            cid, OrderState.UNKNOWN, raw_status=raw_status)
        self.risk_engine.restore_connection()
        return None

    def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        """Request cancel; terminal local state requires authoritative cumulative fill."""
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
            self.persistence.update_intent_status(
                client_order_id, OrderState.UNKNOWN, raw_status=raw_status)
            return False

        exchange_order_id = str(response["orderId"])
        mapped_state = self.adapter._map_order_state(raw_status)
        if mapped_state != OrderState.CANCELED:
            self.persistence.update_intent_status(
                client_order_id,
                OrderState.UNKNOWN,
                exchange_order_id,
                raw_status=raw_status,
            )
            self.risk_engine.restore_connection()
            return False

        final_filled = self._authoritative_filled_qty(response.get("executedQty"))
        if final_filled is None:
            self.persistence.update_intent_status(
                client_order_id,
                OrderState.UNKNOWN,
                exchange_order_id,
                raw_status=raw_status,
            )
            self.risk_engine.restore_connection()
            return True

        if not self.risk_engine.on_cancel_confirmed(client_order_id, final_filled):
            self.persistence.update_intent_status(
                client_order_id,
                OrderState.UNKNOWN,
                exchange_order_id,
                raw_status=raw_status,
            )
            return True

        if not self._sync_authoritative_fill_total(
            client_order_id, final_filled, response.get("avgPrice")
        ):
            self.persistence.update_intent_status(
                client_order_id,
                OrderState.UNKNOWN,
                exchange_order_id,
                raw_status=raw_status,
            )
            return True

        self.persistence.update_intent_status(
            client_order_id,
            OrderState.CANCELED,
            exchange_order_id,
            raw_status=raw_status,
        )
        return True

    def handle_order_update(self, update: Dict[str, Any]):
        """Process an ORDER_TRADE_UPDATE idempotently and fail closed on mismatch."""
        cid = update["client_order_id"]
        intent = self.persistence.get_intent(cid)
        if not intent:
            logger.warning("Received update for unknown order: %s", cid)
            return

        update_reduce_only = self._bool_value(update.get("reduce_only"))
        persisted_reduce_only = bool(int(intent.get("reduce_only", 0)))
        if update_reduce_only is not None and update_reduce_only != persisted_reduce_only:
            self.persistence.update_intent_status(
                cid,
                OrderState.UNKNOWN,
                intent.get("exchange_order_id"),
                raw_status=str(update.get("order_status", "UNKNOWN")),
            )
            self.risk_engine.restore_connection()
            return

        mapped_state = update["mapped_state"]
        raw_status = update["order_status"]

        if update["execution_type"] == "TRADE":
            if cid not in self.risk_engine.orders:
                self.persistence.update_intent_status(
                    cid,
                    OrderState.UNKNOWN,
                    intent.get("exchange_order_id"),
                    raw_status=raw_status,
                )
                self.risk_engine.restore_connection()
                return
            trade_id = update.get("trade_id")
            if trade_id:
                filled_qty = update["last_filled_qty"]
                filled_price = update["last_filled_price"]
                if filled_qty > Decimal("0"):
                    persistence_fill_id = f"ws_fill_{trade_id}"
                    duplicate = self.persistence.has_fill(persistence_fill_id)
                    try:
                        total_qty = Decimal(str(intent["quantity"]))
                        persisted_filled = Decimal(str(intent["filled_quantity"]))
                        cumulative = self._authoritative_filled_qty(
                            update.get("accumulated_filled_qty"))
                    except Exception:
                        cumulative = None
                        total_qty = Decimal("-1")
                        persisted_filled = Decimal("-1")

                    expected_cumulative = (
                        persisted_filled if duplicate else persisted_filled + filled_qty
                    )
                    if (
                        filled_qty.is_nan()
                        or filled_qty.is_infinite()
                        or total_qty <= 0
                        or persisted_filled < 0
                        or expected_cumulative > total_qty
                        or cumulative is None
                        or cumulative != expected_cumulative
                    ):
                        self.persistence.update_intent_status(
                            cid,
                            OrderState.UNKNOWN,
                            intent.get("exchange_order_id"),
                            raw_status=raw_status,
                        )
                        if not duplicate:
                            self.risk_engine.on_fill(
                                order_id=cid,
                                fill_id=str(trade_id),
                                amount=filled_qty,
                            )
                        self.risk_engine.restore_connection()
                        return

                    if not duplicate:
                        fill_applied = self.persistence.add_fill(
                            fill_id=persistence_fill_id,
                            client_order_id=cid,
                            quantity=filled_qty,
                            price=filled_price,
                        )
                        if not fill_applied:
                            self.persistence.update_intent_status(
                                cid,
                                OrderState.UNKNOWN,
                                intent.get("exchange_order_id"),
                                raw_status=raw_status,
                            )
                            self.risk_engine.restore_connection()
                            return
                        self.risk_engine.on_fill(
                            order_id=cid,
                            fill_id=str(trade_id),
                            amount=filled_qty,
                        )

        if mapped_state == OrderState.CANCELED:
            final_filled = self._authoritative_filled_qty(
                update.get("accumulated_filled_qty"))
            if final_filled is None:
                self.persistence.update_intent_status(
                    cid,
                    OrderState.UNKNOWN,
                    intent.get("exchange_order_id"),
                    raw_status=raw_status,
                )
                self.risk_engine.restore_connection()
                return
            if not self.risk_engine.on_cancel_confirmed(cid, final_filled):
                self.persistence.update_intent_status(
                    cid,
                    OrderState.UNKNOWN,
                    intent.get("exchange_order_id"),
                    raw_status=raw_status,
                )
                return
            if not self._sync_authoritative_fill_total(
                cid, final_filled, update.get("average_price")
            ):
                self.persistence.update_intent_status(
                    cid,
                    OrderState.UNKNOWN,
                    intent.get("exchange_order_id"),
                    raw_status=raw_status,
                )
                return
            self.persistence.update_intent_status(
                cid,
                OrderState.CANCELED,
                intent.get("exchange_order_id"),
                raw_status=raw_status,
            )
            return

        current_status = intent["status"]
        if current_status == "FILLED" and mapped_state != OrderState.FILLED:
            return
        self.persistence.update_intent_status(
            cid,
            mapped_state,
            intent.get("exchange_order_id"),
            raw_status=raw_status,
        )
