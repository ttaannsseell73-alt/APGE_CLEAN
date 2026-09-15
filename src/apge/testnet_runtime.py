import time
import logging
from typing import Dict, Any, Optional
from decimal import Decimal

from apge.binance_adapter import BinanceAdapter
from apge.grid_strategy import generate_grid_proposals, MarketRegime
from apge.simulator import SystemState

logger = logging.getLogger(__name__)

class TestnetRuntime:
    """
    Exchange runtime around the accepted Binance adapter.
    Handles TESTNET validation, server time sync, loading exchange limits/filters.
    """
    def __init__(self, adapter: BinanceAdapter):
        self.adapter = adapter
        self.server_time_offset: int = 0
        self.tick_size: Optional[Decimal] = None
        self.step_size: Optional[Decimal] = None
        self.filters: Dict[str, Any] = {}

        # Market Data state
        self.best_bid: Optional[Decimal] = None
        self.best_ask: Optional[Decimal] = None
        self.bba_timestamp: int = 0
        self.bba_receive_timestamp: int = 0
        self.stale_data_threshold_ms: int = 5000
        self.is_connected: bool = False

    def sync_server_time(self) -> bool:
        """
        Synchronizes local clock with server time to determine offset.
        Returns True if successful, False if network error.
        """
        try:
            local_time_before = int(time.time() * 1000)
            server_response = self.adapter.get_server_time()
            local_time_after = int(time.time() * 1000)

            server_time = server_response["serverTime"]
            latency = (local_time_after - local_time_before) // 2

            self.server_time_offset = server_time - (local_time_before + latency)
            logger.info(f"Server time synchronized. Offset: {self.server_time_offset}ms")
            return True
        except Exception as e:
            logger.error(f"Failed to sync server time: {e}")
            return False

    def load_exchange_info(self, symbol: str) -> bool:
        """
        Loads tick size, step size, and other filters for the symbol.
        Returns True if successful, False if network error or symbol not found.
        """
        try:
            exchange_info = self.adapter.get_exchange_info()

            symbol_info = next((s for s in exchange_info.get("symbols", []) if s["symbol"] == symbol), None)
            if not symbol_info:
                logger.error(f"Symbol {symbol} not found in exchange info.")
                return False

            for f in symbol_info.get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    self.tick_size = Decimal(f["tickSize"])
                    self.filters["maxPrice"] = Decimal(f["maxPrice"])
                    self.filters["minPrice"] = Decimal(f["minPrice"])
                elif f["filterType"] == "LOT_SIZE":
                    self.step_size = Decimal(f["stepSize"])
                    self.filters["maxQty"] = Decimal(f["maxQty"])
                    self.filters["minQty"] = Decimal(f["minQty"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    self.filters["minNotional"] = Decimal(f["notional"])

            if not self.tick_size or not self.step_size:
                logger.error(f"Could not extract tickSize or stepSize for {symbol}.")
                return False

            logger.info(f"Loaded exchange info for {symbol}. TickSize: {self.tick_size}, StepSize: {self.step_size}")
            return True
        except Exception as e:
            logger.error(f"Failed to load exchange info: {e}")
            return False

    def handle_book_ticker(self, event: Dict[str, Any]):
        """
        Processes a bookTicker event to update best bid/ask.
        """
        self.is_connected = True
        self.bba_receive_timestamp = int(time.time() * 1000)

        parsed = self.adapter.parse_book_ticker(event)

        self.best_bid = parsed["bid_price"]
        self.best_ask = parsed["ask_price"]
        self.bba_timestamp = self.bba_receive_timestamp # In Binance, bookTicker doesn't have an event timestamp E, just update_id u. We use receive time.

    def is_stale_data(self) -> bool:
        """
        Checks if the market data is stale based on the threshold.
        """
        if not self.is_connected:
            return True

        if self.best_bid is None or self.best_ask is None:
            return True

        current_time = int(time.time() * 1000)
        return (current_time - self.bba_receive_timestamp) > self.stale_data_threshold_ms

    def handle_connection_loss(self):
        """
        Marks connection as lost, forcing stale state.
        """
        self.is_connected = False
        logger.warning("Connection lost. Market data is now considered stale.")

    def handle_user_data_event(self, event: Dict[str, Any], execution_engine: Any):
        """
        Idempotently processes user data events (ACCOUNT_UPDATE, ORDER_TRADE_UPDATE).
        """
        event_type = event.get("e")

        if event_type == "ACCOUNT_UPDATE":
            parsed = self.adapter.parse_account_update(event)
            # Typically, we update our local inventory/balance cache here.
            # We don't rely solely on this for reconciliation, but it keeps things fresh.
            # In V1, inventory is typically reconciled by get_positions(), but updates are fine.
            logger.info(f"Account update: {parsed['reason']}")

        elif event_type == "ORDER_TRADE_UPDATE":
            parsed = self.adapter.parse_order_trade_update(event)
            # Pass to execution engine to handle fill/cancel idempotency safely
            execution_engine.handle_order_update(parsed)

        else:
            logger.debug(f"Unhandled user data event type: {event_type}")

    def run_grid_cycle(self, symbol: str, current_inventory: Decimal,
                       grid_spacing: Decimal, base_size: Decimal, level_count: int,
                       max_inventory: Decimal, system_state: SystemState,
                       market_regime: MarketRegime, execution_engine: Any):
        """
        Runs one cycle of grid evaluation.
        Generates proposals, diffs against existing active orders, and executes necessary changes.
        """
        # If we have no market data, proposals will just return [] due to None values or state.
        bb = self.best_bid or Decimal("0")
        ba = self.best_ask or Decimal("0")

        # 1. Generate desired grid
        proposals = generate_grid_proposals(
            best_bid=bb,
            best_ask=ba,
            current_inventory=current_inventory,
            grid_spacing=grid_spacing,
            base_size=base_size,
            level_count=level_count,
            max_inventory=max_inventory,
            tick_size=self.tick_size or Decimal("0.1"),
            step_size=self.step_size or Decimal("0.001"),
            system_state=system_state,
            market_regime=market_regime,
            is_stale_data=self.is_stale_data()
        )

        # 2. Get currently tracked active orders from persistence
        active_intents = execution_engine.persistence.get_active_intents()
        active_orders = [i for i in active_intents if i["symbol"] == symbol]

        # 3. Diffing: minimal submit/cancel changes
        desired_set = set()
        for p in proposals:
            desired_set.add((p.side, p.price, p.quantity))

        current_set = set()
        order_map = {}
        for intent in active_orders:
            key = (intent["side"], Decimal(intent["price"]), Decimal(intent["quantity"]) - Decimal(intent["filled_quantity"]))
            # If an order is partially filled, we treat the remaining amount as the active key.
            # But grid proposals don't know about partial fills natively.
            # A simple approach for V1: if it's partially filled, we might cancel and recreate,
            # or just leave it. Let's just track the original quantity for exact matching to avoid churn.
            # Grid strategy requests specific amounts. If partial fill happened, it reduces inventory,
            # which shifts the grid up/down, naturally handling the counter order.
            # So matching exactly on original price and remaining quantity is safest to avoid churn.

            # Actually, to prevent churn on untouched orders, match on original price and original quantity
            key_exact = (intent["side"], Decimal(intent["price"]), Decimal(intent["quantity"]))
            current_set.add(key_exact)
            if key_exact not in order_map:
                order_map[key_exact] = []
            order_map[key_exact].append(intent["client_order_id"])

        to_create = desired_set - current_set
        to_cancel_keys = current_set - desired_set

        # 4. Execute Cancellations
        for key in to_cancel_keys:
            cids = order_map[key]
            for cid in cids:
                execution_engine.cancel_order(symbol, cid)

        # 5. Execute Creations
        for side, price, qty in to_create:
            # We reconstruct the proposal object
            # Note: For multiple identical proposals, Python sets dedup them.
            # generate_grid_proposals doesn't generate identical proposals.
            proposal = next((p for p in proposals if p.side == side and p.price == price and p.quantity == qty), None)
            if proposal:
                execution_engine.execute_proposal(symbol, proposal)
