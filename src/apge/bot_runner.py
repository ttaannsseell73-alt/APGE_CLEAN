import argparse
import os
import sys
import time
import logging
from decimal import Decimal

from apge.persistence import Persistence
from apge.binance_adapter import BinanceAdapter, Transport
from apge.simulator import RiskEngine, SystemState
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
    risk_engine.system_state = SystemState.INITIALIZING
    execution_engine = ExecutionEngine(db, adapter, risk_engine)
    reconciler = Reconciler(db, adapter)

    if not dry_run:
        # Start connectivity check
        if not runtime.sync_server_time():
            logger.error("Failed to sync server time. Halting.")
            sys.exit(1)

        if not runtime.load_exchange_info(args.symbol):
            logger.error("Failed to load exchange info. Halting.")
            sys.exit(1)

        # Reconcile on start
        risk_engine.system_state = SystemState.RECONCILING
        logger.info("Reconciling state...")
        if not reconciler.resolve_state(args.symbol):
            logger.error("Reconciliation failed (corrupt/unknown state). Halting.")
            risk_engine.system_state = SystemState.HALTED
            sys.exit(1)

        # Transition to operational
        risk_engine.system_state = SystemState.OPERATIONAL
        logger.info("System is OPERATIONAL. Starting grid loop.")

        # Here we would normally start websocket threads for user data and market data.
        # But this is just a structure for the bot runner.
        # We will loop for a short while as a smoke test, then exit.

        try:
            for _ in range(5):
                logger.info("Running grid cycle...")
                # Note: Requires best_bid/best_ask to be set from websockets to actually do anything
                runtime.run_grid_cycle(
                    symbol=args.symbol,
                    current_inventory=Decimal("0.0"),
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
        logger.info("Dry run completed. Components initialized.")

    db.close()

if __name__ == "__main__":
    main()
