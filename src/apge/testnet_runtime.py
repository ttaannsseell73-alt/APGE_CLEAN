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
    """TESTNET WebSocket transport with fail-closed reconnect signaling."""

    def __init__(self, ws_url: str, runtime: 'TestnetRuntime', listen_key: str = ""):
        self.ws_url = ws_url
        self.runtime = runtime
        self.listen_key = listen_key
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run_loop(self):
        try:
            import websocket
            has_ws = True
        except ImportError:
            has_ws = False

        while self._running:
            if not has_ws:
                logger.warning("websocket-client not installed; event loop cannot connect.")
                self.runtime.handle_connection_loss()
                time.sleep(1)
                continue
            try:
                ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                logger.info("Connecting to WS: %s", self.ws_url)
                ws.run_forever(ping_interval=60, ping_timeout=10)
            except Exception as exc:
                logger.error("WebSocket error: %s", exc)

            if self._running:
                self.runtime.handle_connection_loss()
                time.sleep(1)
                # Merely leaving run_forever does not prove a successful
                # reconnect. Reconciliation is triggered by the owner only
                # after connectivity has actually been re-established.

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            event_type = data.get("e")
            if event_type == "bookTicker":
                self.runtime.handle_book_ticker(data)
            elif event_type in ("ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE"):
                self.runtime.handle_user_data_event(data, self.runtime.execution_engine)
        except Exception as exc:
            logger.error("Error parsing WS message: %s", exc)

    def _on_error(self, ws, error):
        logger.error("WS Error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        logger.info("WS Closed: %s %s", close_status_code, close_msg)


class TestnetRuntime:
    """Binance Futures TESTNET runtime around the accepted adapter."""

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
            local_before = int(time.time() * 1000)
            response = self.adapter.get_server_time()
            local_after = int(time.time() * 1000)
            server_time = response["serverTime"]
            latency = (local_after - local_before) // 2
            self.server_time_offset = server_time - (local_before + latency)
            self.adapter.server_time_offset = self.server_time_offset
            return True
        except Exception as exc:
            logger.error("Failed to sync server time: %s", exc)
            return False

    def load_exchange_info(self, symbol: str) -> bool:
        try:
            exchange_info = self.adapter.get_exchange_info()
            symbol_info = next(
                (row for row in exchange_info.get("symbols", []) if row["symbol"] == symbol),
                None,
            )
            if not symbol_info:
                return False
            for filt in symbol_info.get("filters", []):
                kind = filt["filterType"]
                if kind == "PRICE_FILTER":
                    self.tick_size = Decimal(filt["tickSize"])
                    self.filters["maxPrice"] = Decimal(filt["maxPrice"])
                    self.filters["minPrice"] = Decimal(filt["minPrice"])
                elif kind == "LOT_SIZE":
                    self.step_size = Decimal(filt["stepSize"])
                    self.filters["maxQty"] = Decimal(filt["maxQty"])
                    self.filters["minQty"] = Decimal(filt["minQty"])
                elif kind == "MIN_NOTIONAL":
                    self.filters["minNotional"] = Decimal(filt["notional"])
            return bool(self.tick_size and self.step_size)
        except Exception as exc:
            logger.error("Failed to load exchange info: %s", exc)
            return False

    def handle_book_ticker(self, event: Dict[str, Any]):
        self.is_connected = True
        self.bba_receive_timestamp = int(time.time() * 1000)
        parsed = self.adapter.parse_book_ticker(event)
        self.best_bid = parsed["bid_price"]
        self.best_ask = parsed["ask_price"]
        self.bba_timestamp = self.bba_receive_timestamp

    def is_stale_data(self) -> bool:
        if not self.is_connected or self.best_bid is None or self.best_ask is None:
            return True
        return (
            int(time.time() * 1000) - self.bba_receive_timestamp
            > self.stale_data_threshold_ms
        )

    def handle_connection_loss(self):
        self.is_connected = False
        if self.execution_engine:
            self.execution_engine.risk_engine.lose_connection()

    def handle_reconnect(self):
        """Call only after transport connectivity is verifiably restored."""
        if self.execution_engine and self.reconciler:
            self.execution_engine.risk_engine.restore_connection()
            success = self.reconciler.resolve_state(self.symbol)
            if success:
                self.current_inventory = self.reconciler.last_position_amount
                self.execution_engine.risk_engine.complete_reconciliation()
                if self.execution_engine.risk_engine.system_state != SystemState.OPERATIONAL:
                    self.execution_engine.risk_engine.system_state = SystemState.HALTED
            else:
                self.execution_engine.risk_engine.system_state = SystemState.HALTED

    def sync_inventory(self) -> bool:
        try:
            positions = self.adapter.get_positions()
            pos = next((p for p in positions if p.get("symbol") == self.symbol), None)
            self.current_inventory = Decimal(
                str(pos.get("positionAmt", "0.0")) if pos else "0.0"
            )
            return True
        except Exception as exc:
            logger.error("Failed to sync inventory: %s", exc)
            return False

    def start_event_loops(self, listen_key: str):
        if not listen_key:
            raise ValueError("listen_key is required; dummy user-data streams are forbidden")
        ws_domain = self.adapter.ws_url
        market_ws = TestnetWebsocketTransport(
            f"{ws_domain}/ws/{self.symbol.lower()}@bookTicker", self)
        user_ws = TestnetWebsocketTransport(
            f"{ws_domain}/ws/{listen_key}", self, listen_key)
        self.ws_transports.extend([market_ws, user_ws])
        for ws in self.ws_transports:
            ws.start()

    def handle_user_data_event(self, event: Dict[str, Any], execution_engine: Any):
        event_type = event.get("e")
        if event_type == "ACCOUNT_UPDATE":
            parsed = self.adapter.parse_account_update(event)
            for pos in parsed.get("positions", []):
                if pos["symbol"] == self.symbol:
                    self.current_inventory = Decimal(str(pos["position_amount"]))
        elif event_type == "ORDER_TRADE_UPDATE":
            parsed = self.adapter.parse_order_trade_update(event)
            execution_engine.handle_order_update(parsed)

    def run_grid_cycle(
        self,
        grid_spacing: Decimal,
        base_size: Decimal,
        level_count: int,
        max_inventory: Decimal,
        system_state: SystemState,
        market_regime: MarketRegime,
        execution_engine: Any,
    ):
        """Run one deterministic desired-vs-live grid cycle."""
        actual_state = execution_engine.risk_engine.system_state
        if system_state != actual_state:
            logger.warning("Caller/system RiskEngine state mismatch; grid cycle blocked.")
            return

        if (
            not self.tick_size
            or not self.step_size
            or "minQty" not in self.filters
            or "minNotional" not in self.filters
        ):
            return

        bb = self.best_bid or Decimal("0")
        ba = self.best_ask or Decimal("0")
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
            system_state=actual_state,
            market_regime=market_regime,
            is_stale_data=self.is_stale_data(),
        )

        valid = []
        for proposal in proposals:
            if proposal.quantity < self.filters["minQty"]:
                continue
            if "maxQty" in self.filters and proposal.quantity > self.filters["maxQty"]:
                continue
            if proposal.price * proposal.quantity < self.filters["minNotional"]:
                continue
            valid.append(proposal)
        proposals = valid

        active_orders = [
            row
            for row in execution_engine.persistence.get_active_intents()
            if row["symbol"] == self.symbol
        ]
        desired = {
            (p.side, p.price, p.quantity, bool(p.reduce_only)): p
            for p in proposals
        }
        current: Dict[tuple, list[str]] = {}
        for intent in active_orders:
            key = (
                intent["side"],
                Decimal(intent["price"]),
                Decimal(intent["quantity"]),
                bool(int(intent.get("reduce_only", 0))),
            )
            current.setdefault(key, []).append(intent["client_order_id"])

        desired_set = set(desired)
        current_set = set(current)

        for key in sorted(current_set - desired_set, key=lambda x: (x[0], x[1], x[2], x[3])):
            for cid in sorted(current[key]):
                execution_engine.cancel_order(self.symbol, cid)

        if execution_engine.risk_engine.system_state != SystemState.OPERATIONAL:
            return

        for key in sorted(desired_set - current_set, key=lambda x: (x[0], x[1], x[2], x[3])):
            execution_engine.execute_proposal(self.symbol, desired[key])
