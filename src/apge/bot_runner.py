import argparse
import os
import sys
import time
import logging
from decimal import Decimal

from apge.persistence import Persistence
from apge.binance_adapter import BinanceAdapter, Transport
from apge.simulator import RiskEngine, SystemState, OrderState
from apge.execution_engine import ExecutionEngine
from apge.testnet_runtime import TestnetRuntime
from apge.grid_strategy import MarketRegime
from apge.reconciliation import Reconciler

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("bot_runner")

class RequestsTransport(Transport):
    """Simple wrapper around requests to satisfy the Transport protocol."""
    def __init__(self):
        import requests
        self.session = requests.Session()

    def get(self, url, params=None, headers=None):
        r = self.session.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    def post(self, url, data=None, headers=None):
        r = self.session.post(url, data=data, headers=headers)
        r.raise_for_status()
        return r.json()

    def delete(self, url, params=None, headers=None):
        r = self.session.delete(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()


def main():
    parser = argparse.ArgumentParser(description="APGE V1 TESTNET Bot Runner")
    parser.add_argument("--dry-run", action="store_true", help="Run without sending orders (default).")
    parser.add_argument("--testnet", action="store_true", help="Target testnet (always true implicitly, flag for compatibility).")
    parser.add_argument("--allow-testnet-orders", action="store_true", help="Explicitly allow sending orders to TESTNET.")
    parser.add_argument("--symbol", type=str, default="BTCUSDT", help="Symbol to trade.")
    args = parser.parse_args()

    dry_run = not args.allow_testnet_orders if args.allow_testnet_orders else True

    if args.allow_testnet_orders:
        logger.warning("DANGER: TESTNET ORDERS ENABLED. The bot will send live testnet orders.")
    else:
        logger.info("Starting in DRY-RUN mode. No real orders will be sent.")

    # Load credentials
    api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")

    if args.allow_testnet_orders and (not api_key or not api_secret):
        logger.error("BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET must be set for live testnet orders.")
        sys.exit(1)

    db = Persistence("apge_testnet.db")
    transport = RequestsTransport()
    adapter = BinanceAdapter(
        transport=transport,
        http_url="https://testnet.binancefuture.com",
        ws_url="wss://fstream.binancefuture.com",
        api_key=api_key,
        api_secret=api_secret
    )

    # Initialize components
    runtime = TestnetRuntime(adapter)
    risk_engine = RiskEngine(position_limit=Decimal("5.0"))
    risk_engine.system_state = SystemState.RECONCILING
    execution_engine = ExecutionEngine(db, adapter, risk_engine)
    reconciler = Reconciler(db, adapter)
    runtime.attach_components(execution_engine, reconciler, args.symbol)

    if not dry_run:
        # Start connectivity check
        if not runtime.sync_server_time():
            logger.error("Failed to sync server time. Halting.")
            sys.exit(1)

        if not runtime.load_exchange_info(args.symbol):
            logger.error("Failed to load exchange info. Halting.")
            sys.exit(1)

        # Reconcile on start
        logger.info("Reconciling state...")
        logger.info("Reconciling state...")
        if not reconciler.resolve_state(args.symbol):
            logger.error("Reconciliation failed (corrupt/unknown state). Halting.")
            risk_engine.system_state = SystemState.HALTED
            sys.exit(1)

        # Transition to operational
        risk_engine.system_state = SystemState.OPERATIONAL
        logger.info("System is OPERATIONAL. Starting event loops.")

        # Start event loops
        runtime.start_event_loops("dummy_listen_key")

        # Get actual starting inventory from REST positions
        runtime.sync_inventory()

        try:
            for _ in range(5):
                logger.info("Running grid cycle...")
                # Fetch BBA via REST for the smoke loop since we mock WS
                try:
                    book = adapter.transport.get(f"{adapter.http_url}/fapi/v1/ticker/bookTicker", params={"symbol": args.symbol})
                    runtime.handle_book_ticker({"s": args.symbol, "b": book["bidPrice"], "B": book["bidQty"], "a": book["askPrice"], "A": book["askQty"], "u": 1})
                except Exception as e:
                    logger.warning(f"Failed to fetch market data: {e}")

                runtime.run_grid_cycle(
                    grid_spacing=Decimal("100"),
                    base_size=Decimal("0.01"),
                    level_count=3,
                    max_inventory=Decimal("5.0"),
                    system_state=risk_engine.system_state,
                    market_regime=MarketRegime.NEUTRAL,
                    execution_engine=execution_engine
                )
                time.sleep(2)
        except KeyboardInterrupt:
            logger.info("Shutting down...")

    else:
        logger.info("Starting in DRY-RUN mode. Components initialized.")
        risk_engine.system_state = SystemState.OPERATIONAL

        # Mock some exchange filters and data to allow the dry-run loop to proceed
        runtime.tick_size = Decimal("0.1")
        runtime.step_size = Decimal("0.001")
        runtime.filters = {"minQty": Decimal("0.001"), "minNotional": Decimal("5.0"), "maxQty": Decimal("100.0")}
        runtime.handle_book_ticker({"s": args.symbol, "b": "100.0", "B": "1", "a": "101.0", "A": "1", "u": 1})

        class DryRunMockAdapter:
            def submit_limit_order(self, **kwargs):
                logger.info(f"DRY RUN: Would submit {kwargs['side']} {kwargs['quantity']} @ {kwargs['price']}")
                return {"status": "NEW", "orderId": f"mock_{kwargs['client_order_id']}"}
            def cancel_order(self, symbol, cid):
                logger.info(f"DRY RUN: Would cancel {cid}")
                return {"status": "CANCELED", "orderId": f"mock_{cid}"}
            def _map_order_state(self, status):
                mapping = {"NEW": OrderState.OPEN, "CANCELED": OrderState.CANCELED}
                return mapping.get(status, OrderState.UNKNOWN)

        # Swap the adapter out for execution so it doesn't hit the network
        execution_engine.adapter = DryRunMockAdapter()

        try:
            for _ in range(5):
                logger.info("[DRY RUN] Running grid cycle...")
                # Resolve UNKNOWNs to prevent UNKNOWN gate from blocking loop
                for intent in db.get_active_intents():
                    if execution_engine.risk_engine.orders.get(intent["client_order_id"]):
                        if execution_engine.risk_engine.orders[intent["client_order_id"]].state == OrderState.UNKNOWN:
                            execution_engine.risk_engine.resolve_order(intent["client_order_id"], OrderState.OPEN, Decimal("0"))

                runtime.run_grid_cycle(
                    grid_spacing=Decimal("1.0"),
                    base_size=Decimal("0.1"),
                    level_count=3,
                    max_inventory=Decimal("5.0"),
                    system_state=risk_engine.system_state,
                    market_regime=MarketRegime.NEUTRAL,
                    execution_engine=execution_engine
                )
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")

        logger.info("Dry run completed deterministic cycles.")

    db.close()

if __name__ == "__main__":
    main()
