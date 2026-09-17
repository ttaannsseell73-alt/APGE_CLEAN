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
    """TESTNET WebSocket loop with fail-closed reconnect semantics."""

    def __init__(self, ws_url: str, runtime: "TestnetRuntime", listen_key: str = ""):
        self.ws_url = ws_url
        self.runtime = runtime
        self.listen_key = listen_key
        self.stream_name = "user" if listen_key else "market"
        self._running = False
        self._thread = None
        self._ws = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run_loop(self):
        try:
            import websocket
        except ImportError:
            logger.error("websocket-client is required for TESTNET event loops")
            self.runtime.handle_stream_loss(self.stream_name)
            return

        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                logger.info("Connecting TESTNET %s stream", self.stream_name)
                self._ws.run_forever(ping_interval=60, ping_timeout=10)
            except Exception as exc:
                logger.error("WebSocket %s exception: %s", self.stream_name, exc)
                self.runtime.handle_stream_loss(self.stream_name)
            finally:
                self._ws = None

            if self._running:
                self.runtime.handle_stream_loss(self.stream_name)
                time.sleep(1)

    def _on_open(self, ws):
        self.runtime.handle_stream_open(self.stream_name)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            event_type = data.get("e")
            if event_type == "bookTicker":
                self.runtime.handle_book_ticker(data)
            elif event_type in ("ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE"):
                self.runtime.handle_user_data_event(data, self.runtime.execution_engine)
        except Exception as exc:
            logger.error("Invalid %s websocket event: %s", self.stream_name, exc)
            self.runtime.handle_data_uncertainty()

    def _on_error(self, ws, error):
        logger.error("WS %s error: %s", self.stream_name, error)
        self.runtime.handle_stream_loss(self.stream_name)

    def _on_close(self, ws, close_status_code, close_msg):
        logger.info("WS %s closed: %s %s", self.stream_name, close_status_code, close_msg)
        if self._running:
            self.runtime.handle_stream_loss(self.stream_name)


class TestnetRuntime:
    """Binance TESTNET runtime and data-health boundary."""
    __test__ = False

    def __init__(self, adapter: BinanceAdapter):
        self.adapter = adapter
        self.server_time_offset: int = 0
        self.tick_size: Optional[Decimal] = None
        self.step_size: Optional[Decimal] = None
        self.filters: Dict[str, Any] = {}

        self.best_bid: Optional[Decimal] = None
        self.best_ask: Optional[Decimal] = None
        self.bba_timestamp: int = 0
        self.bba_receive_timestamp: int = 0
        self.stale_data_threshold_ms: int = 5000
        self.is_connected: bool = False
        self.stream_health: Dict[str, bool] = {"market": False, "user": False}

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
        try:
            local_time_before = int(time.time() * 1000)
            server_response = self.adapter.get_server_time()
            local_time_after = int(time.time() * 1000)
            server_time = int(server_response["serverTime"])
            latency = (local_time_after - local_time_before) // 2
            self.server_time_offset = server_time - (local_time_before + latency)
            self.adapter.server_time_offset = self.server_time_offset
            logger.info("Server time synchronized. Offset: %sms", self.server_time_offset)
            return True
        except Exception as exc:
            logger.error("Failed to sync server time: %s", exc)
            return False

    def load_exchange_info(self, symbol: str) -> bool:
        try:
            exchange_info = self.adapter.get_exchange_info()
            symbol_info = next((s for s in exchange_info.get("symbols", []) if s["symbol"] == symbol), None)
            if not symbol_info:
                return False

            filters: Dict[str, Any] = {}
            tick_size = None
            step_size = None
            for item in symbol_info.get("filters", []):
                if item["filterType"] == "PRICE_FILTER":
                    tick_size = Decimal(item["tickSize"])
                    filters["maxPrice"] = Decimal(item["maxPrice"])
                    filters["minPrice"] = Decimal(item["minPrice"])
                elif item["filterType"] == "LOT_SIZE":
                    step_size = Decimal(item["stepSize"])
                    filters["maxQty"] = Decimal(item["maxQty"])
                    filters["minQty"] = Decimal(item["minQty"])
                elif item["filterType"] == "MIN_NOTIONAL":
                    filters["minNotional"] = Decimal(item["notional"])

            if not tick_size or not step_size or "minQty" not in filters or "minNotional" not in filters:
                return False
            if tick_size <= 0 or step_size <= 0 or filters["minQty"] < 0 or filters["minNotional"] < 0:
                return False

            self.tick_size = tick_size
            self.step_size = step_size
            self.filters = filters
            return True
        except Exception as exc:
            logger.error("Failed to load exchange info: %s", exc)
            return False

    def handle_book_ticker(self, event: Dict[str, Any]):
        parsed = self.adapter.parse_book_ticker(event)
        if self.symbol and parsed["symbol"] != self.symbol:
            raise ValueError("bookTicker symbol mismatch")
        if not self.symbol:
            self.symbol = parsed["symbol"]
        if parsed["bid_price"] <= 0 or parsed["ask_price"] <= 0:
            raise ValueError("bookTicker prices must be positive")
        # Crossed books are preserved as data and rejected by GridStrategy. This
        # keeps the runtime fail-closed without making market-data ingestion throw.
        self.bba_receive_timestamp = int(time.time() * 1000)
        self.best_bid = parsed["bid_price"]
        self.best_ask = parsed["ask_price"]
        self.bba_timestamp = self.bba_receive_timestamp
        self.stream_health["market"] = True
        self.is_connected = True

    def is_stale_data(self) -> bool:
        if not self.is_connected or self.best_bid is None or self.best_ask is None:
            return True
        return (int(time.time() * 1000) - self.bba_receive_timestamp) > self.stale_data_threshold_ms

    def handle_stream_open(self, stream_name: str):
        if stream_name not in self.stream_health:
            self.handle_data_uncertainty()
            return
        self.stream_health[stream_name] = True
        if stream_name == "market":
            self.is_connected = True
        if (all(self.stream_health.values()) and self.execution_engine and
                self.execution_engine.risk_engine.system_state in (
                    SystemState.CONNECTION_LOST, SystemState.RECONCILING)):
            self.handle_reconnect()

    def handle_stream_loss(self, stream_name: str):
        if stream_name in self.stream_health:
            self.stream_health[stream_name] = False
        self.handle_connection_loss()

    def handle_data_uncertainty(self):
        self.is_connected = False
        if self.execution_engine:
            self.execution_engine.risk_engine.restore_connection()

    def handle_connection_loss(self):
        self.is_connected = False
        if self.execution_engine:
            risk = self.execution_engine.risk_engine
            if risk.system_state != SystemState.HALTED:
                risk.system_state = SystemState.CONNECTION_LOST

    def handle_reconnect(self):
        if not all(self.stream_health.values()):
            return
        if self.execution_engine and self.reconciler:
            risk = self.execution_engine.risk_engine
            if risk.system_state == SystemState.HALTED:
                return
            risk.system_state = SystemState.RECONCILING
            success = self.reconciler.resolve_state(self.symbol)
            if success:
                self.current_inventory = self.reconciler.last_position_amount
                risk.complete_reconciliation()
                if risk.system_state != SystemState.OPERATIONAL:
                    risk.system_state = SystemState.HALTED
            else:
                risk.system_state = SystemState.HALTED

    def sync_inventory(self) -> bool:
        try:
            positions = self.adapter.get_positions()
            pos = next((p for p in positions if p.get("symbol") == self.symbol), None)
            value = Decimal(str(pos.get("positionAmt", "0.0"))) if pos else Decimal("0")
            if value.is_nan() or value.is_infinite():
                raise ValueError("invalid position")
            self.current_inventory = value
            return True
        except Exception as exc:
            logger.error("Failed to sync inventory: %s", exc)
            self.handle_data_uncertainty()
            return False

    def start_event_loops(self, listen_key: str):
        if self.ws_transports:
            raise RuntimeError("event loops already started")
        listen_key = self.adapter._validate_listen_key(listen_key)
        ws_domain = self.adapter.ws_url
        self.stream_health = {"market": False, "user": False}
        self.is_connected = False
        # Once asynchronous event ingestion begins, both streams must establish
        # and an authoritative reconciliation must complete before new exposure.
        if self.execution_engine and self.execution_engine.risk_engine.system_state == SystemState.OPERATIONAL:
            self.execution_engine.risk_engine.restore_connection()

        market_ws = TestnetWebsocketTransport(
            f"{ws_domain}/ws/{self.symbol.lower()}@bookTicker", self
        )
        user_ws = TestnetWebsocketTransport(
            f"{ws_domain}/ws/{listen_key}", self, listen_key
        )
        self.ws_transports = [market_ws, user_ws]
        for ws in self.ws_transports:
            ws.start()

    def stop_event_loops(self):
        transports = list(self.ws_transports)
        self.ws_transports = []
        for ws in transports:
            ws.stop()
        self.stream_health = {"market": False, "user": False}
        self.is_connected = False

    def handle_user_data_event(self, event: Dict[str, Any], execution_engine: Any):
        event_type = event.get("e")
        if event_type == "ACCOUNT_UPDATE":
            parsed = self.adapter.parse_account_update(event)
            for pos in parsed.get("positions", []):
                if pos["symbol"] == self.symbol:
                    value = Decimal(str(pos["position_amount"]))
                    if value.is_nan() or value.is_infinite():
                        raise ValueError("invalid account inventory")
                    self.current_inventory = value
        elif event_type == "ORDER_TRADE_UPDATE":
            parsed = self.adapter.parse_order_trade_update(event)
            if self.symbol and parsed["symbol"] != self.symbol:
                raise ValueError("order update symbol mismatch")
            execution_engine.handle_order_update(parsed)
        else:
            raise ValueError(f"unsupported user data event: {event_type}")

    def run_grid_cycle(self,
                       grid_spacing: Decimal, base_size: Decimal, level_count: int,
                       max_inventory: Decimal, system_state: SystemState,
                       market_regime: MarketRegime, execution_engine: Any):
        bb = self.best_bid or Decimal("0")
        ba = self.best_ask or Decimal("0")

        if not self.tick_size or not self.step_size or "minQty" not in self.filters or "minNotional" not in self.filters:
            logger.warning("Cannot run grid cycle: missing required exchange filters. Failing closed.")
            return

        proposals = generate_grid_proposals(
            best_bid=bb,
            best_ask=ba,
            current_inventory=self.current_inventory,
            grid_spacing=grid_spacing,
            base_size=base_size,
            level_count=level_count,
            max_inventory=max_inventory,
            tick_size=self.tick_size,
            step_size=self.step_size,
            system_state=system_state,
            market_regime=market_regime,
            is_stale_data=self.is_stale_data(),
        )

        valid_proposals = []
        for proposal in proposals:
            if proposal.quantity < self.filters["minQty"]:
                continue
            if "maxQty" in self.filters and proposal.quantity > self.filters["maxQty"]:
                continue
            if proposal.price * proposal.quantity < self.filters["minNotional"]:
                continue
            valid_proposals.append(proposal)
        proposals = valid_proposals

        active_orders = [
            intent for intent in execution_engine.persistence.get_active_intents()
            if intent["symbol"] == self.symbol
        ]
        desired_set = {(p.side, p.price, p.quantity) for p in proposals}
        current_set = set()
        order_map = {}
        for intent in active_orders:
            key = (intent["side"], Decimal(intent["price"]), Decimal(intent["quantity"]))
            current_set.add(key)
            order_map.setdefault(key, []).append(intent["client_order_id"])

        to_create = desired_set - current_set
        to_cancel_keys = current_set - desired_set

        for key in sorted(to_cancel_keys, key=lambda x: (x[0], x[1], x[2])):
            for cid in sorted(order_map[key]):
                execution_engine.cancel_order(self.symbol, cid)

        if execution_engine.risk_engine.system_state != SystemState.OPERATIONAL:
            return

        for side, price, qty in sorted(to_create, key=lambda x: (x[0], x[1], x[2])):
            proposal = next(
                (p for p in proposals if p.side == side and p.price == price and p.quantity == qty),
                None,
            )
            if proposal:
                execution_engine.execute_proposal(self.symbol, proposal)
