import time
import logging
import threading
import json
from typing import Dict, Any, Optional, List
from decimal import Decimal

from apge.binance_adapter import BinanceAdapter
from apge.grid_strategy import generate_grid_proposals, MarketRegime
from apge.simulator import SystemState

logger = logging.getLogger(__name__)

class TestnetWebsocketTransport:
    """
    Real TESTNET WebSocket runtime handling reconnects and dispatching events.
    """
    def __init__(self, ws_url: str, runtime: 'TestnetRuntime', listen_key: str = ""):
        self.ws_url = ws_url
        self.runtime = runtime
        self.listen_key = listen_key
        self._running = False
        self._thread = None

        # In a real environment, we'd use websocket-client or websockets.
        # But to avoid adding dependencies, and since we just need the runtime structure,
        # we provide the real reconnect loop structure. Tests will mock the actual socket read.

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run_loop(self):
        """
        Main websocket loop handling reconnects safely.
        """
        import socket
        try:
            import websocket
            HAS_WS = True
        except ImportError:
            HAS_WS = False

        while self._running:
            if not HAS_WS:
                logger.warning("websocket-client not installed. Mocking WS connection loop.")
                time.sleep(1)
                continue

            try:
                ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                logger.info(f"Connecting to WS: {self.ws_url}")
                ws.run_forever(ping_interval=60, ping_timeout=10)

            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            # If we exited run_forever, the socket closed.
            if self._running:
                logger.warning("WebSocket closed unexpectedly. Triggering connection loss.")
                self.runtime.handle_connection_loss()
                time.sleep(1) # Backoff
                logger.info("Attempting WebSocket reconnect...")

                # Reconnect successful, we must trigger reconciliation
                # Wait for the next loop iteration to actually connect,
                # but we will signal the runtime to reconcile once connected.
                self.runtime.handle_reconnect()

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if "e" in data:
                event_type = data["e"]
                if event_type == "bookTicker":
                    self.runtime.handle_book_ticker(data)
                elif event_type in ("ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE"):
                    self.runtime.handle_user_data_event(data, self.runtime.execution_engine)
        except Exception as e:
            logger.error(f"Error parsing WS message: {e}")

    def _on_error(self, ws, error):
        logger.error(f"WS Error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logger.info(f"WS Closed: {close_status_code} {close_msg}")


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

        # Dynamic Inventory State
        self.current_inventory: Decimal = Decimal("0.0")

        self.ws_transports: List[TestnetWebsocketTransport] = []
        self.execution_engine: Any = None
        self.reconciler: Any = None
        self.symbol: str = ""

    def attach_components(self, execution_engine: Any, reconciler: Any, symbol: str):
        self.execution_engine = execution_engine
        self.reconciler = reconciler
        self.symbol = symbol

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
            # Propagate offset to adapter so signed requests use it
            self.adapter.server_time_offset = self.server_time_offset
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
        if self.execution_engine:
            self.execution_engine.risk_engine.system_state = SystemState.CONNECTION_LOST

    def handle_reconnect(self):
        """
        Called when the websocket transport successfully reconnects.
        Must trigger reconciliation before returning to OPERATIONAL.
        """
        logger.info("Websocket reconnected. Triggering reconciliation.")
        if self.execution_engine and self.reconciler:
            self.execution_engine.risk_engine.system_state = SystemState.RECONCILING
            success = self.reconciler.resolve_state(self.symbol)
            if success:
                logger.info("Reconciliation complete. Returning to OPERATIONAL.")
                # Also refresh inventory
                self.sync_inventory()
                self.execution_engine.risk_engine.system_state = SystemState.OPERATIONAL
            else:
                logger.error("Reconciliation failed upon reconnect. HALTING.")
                self.execution_engine.risk_engine.system_state = SystemState.HALTED

    def sync_inventory(self):
        """
        Syncs inventory from REST API.
        """
        try:
            positions = self.adapter.get_positions()
            pos = next((p for p in positions if p.get("symbol") == self.symbol), None)
            if pos:
                self.current_inventory = Decimal(str(pos.get("positionAmt", "0.0")))
                logger.info(f"Inventory synced to {self.current_inventory}")
        except Exception as e:
            logger.error(f"Failed to sync inventory: {e}")

    def start_event_loops(self, listen_key: str):
        """
        Starts the underlying websocket transports for Market Data and User Data.
        """
        ws_domain = self.adapter.ws_url

        # 1. Market Data Stream
        market_stream = f"{ws_domain}/ws/{self.symbol.lower()}@bookTicker"
        market_ws = TestnetWebsocketTransport(market_stream, self)
        self.ws_transports.append(market_ws)

        # 2. User Data Stream
        user_stream = f"{ws_domain}/ws/{listen_key}"
        user_ws = TestnetWebsocketTransport(user_stream, self, listen_key)
        self.ws_transports.append(user_ws)

        for ws in self.ws_transports:
            ws.start()

    def handle_user_data_event(self, event: Dict[str, Any], execution_engine: Any):
        """
        Idempotently processes user data events (ACCOUNT_UPDATE, ORDER_TRADE_UPDATE).
        """
        event_type = event.get("e")

        if event_type == "ACCOUNT_UPDATE":
            parsed = self.adapter.parse_account_update(event)
            # Update real inventory cache dynamically
            for pos in parsed.get("positions", []):
                if pos["symbol"] == self.symbol:
                    self.current_inventory = Decimal(str(pos["position_amount"]))
                    logger.info(f"Account update: Inventory is now {self.current_inventory}")

        elif event_type == "ORDER_TRADE_UPDATE":
            parsed = self.adapter.parse_order_trade_update(event)
            # Pass to execution engine to handle fill/cancel idempotency safely
            execution_engine.handle_order_update(parsed)
            # Note: We rely on ACCOUNT_UPDATE to modify current_inventory for complete correctness,
            # as Binance sends ACCOUNT_UPDATE simultaneously with ORDER_TRADE_UPDATE.

        else:
            logger.debug(f"Unhandled user data event type: {event_type}")

    def run_grid_cycle(self,
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

        # 0. Enforce symbol filters
        if not self.tick_size or not self.step_size or "minQty" not in self.filters or "minNotional" not in self.filters:
            logger.warning("Cannot run grid cycle: missing required exchange filters. Failing closed.")
            return

        # 1. Generate desired grid
        proposals = generate_grid_proposals(
            best_bid=bb,
            best_ask=ba,
            current_inventory=self.current_inventory, # Dynamic state
            grid_spacing=grid_spacing,
            base_size=base_size,
            level_count=level_count,
            max_inventory=max_inventory,
            tick_size=self.tick_size,
            step_size=self.step_size,
            system_state=system_state,
            market_regime=market_regime,
            is_stale_data=self.is_stale_data()
        )

        # Enforce notional and qty limits natively before diffing
        valid_proposals = []
        for p in proposals:
            if p.quantity < self.filters["minQty"]:
                logger.debug(f"Proposal {p.side} rejected: {p.quantity} < minQty {self.filters['minQty']}")
                continue
            if "maxQty" in self.filters and p.quantity > self.filters["maxQty"]:
                logger.debug(f"Proposal {p.side} rejected: {p.quantity} > maxQty {self.filters['maxQty']}")
                continue
            if p.price * p.quantity < self.filters["minNotional"]:
                logger.debug(f"Proposal {p.side} rejected: notional {p.price * p.quantity} < minNotional {self.filters['minNotional']}")
                continue
            valid_proposals.append(p)

        proposals = valid_proposals

        # 2. Get currently tracked active orders from persistence
        active_intents = execution_engine.persistence.get_active_intents()
        active_orders = [i for i in active_intents if i["symbol"] == self.symbol]

        # 3. Diffing: minimal submit/cancel changes
        desired_set = set()
        for p in proposals:
            desired_set.add((p.side, p.price, p.quantity))

        current_set = set()
        order_map = {}
        for intent in active_orders:
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
                execution_engine.cancel_order(self.symbol, cid)

        # 5. Execute Creations
        for side, price, qty in to_create:
            proposal = next((p for p in proposals if p.side == side and p.price == price and p.quantity == qty), None)
            if proposal:
                execution_engine.execute_proposal(self.symbol, proposal)
