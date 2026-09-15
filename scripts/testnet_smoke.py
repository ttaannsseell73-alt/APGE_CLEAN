import argparse
import os
import sys
import logging

from apge.binance_adapter import BinanceAdapter
from apge.bot_runner import RequestsTransport

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("smoke")

def main():
    parser = argparse.ArgumentParser(description="Testnet Validation Smoke Tool")
    parser.add_argument("--stage", type=str, choices=["A", "B", "C"], required=True, help="Stage to test (A: public, B: read-only, C: order)")
    parser.add_argument("--symbol", type=str, default="BTCUSDT")
    args = parser.parse_args()

    api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")

    if args.stage in ["B", "C"] and (not api_key or not api_secret):
        logger.error("Stages B and C require BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET")
        sys.exit(1)

    adapter = BinanceAdapter(
        transport=RequestsTransport(),
        http_url="https://testnet.binancefuture.com",
        ws_url="wss://fstream.binancefuture.com",
        api_key=api_key,
        api_secret=api_secret
    )

    if args.stage == "A":
        logger.info("Stage A: Public connectivity")
        try:
            time_resp = adapter.get_server_time()
            logger.info(f"Server time: {time_resp}")
            info = adapter.get_exchange_info()
            logger.info(f"Exchange info retrieved. Rate limits: {info.get('rateLimits')}")
        except Exception as e:
            logger.error(f"Stage A failed: {e}")

    elif args.stage == "B":
        logger.info("Stage B: Authenticated read-only")
        try:
            positions = adapter.get_positions()
            logger.info(f"Positions: {len(positions)} loaded.")
            open_orders = adapter.get_open_orders(symbol=args.symbol)
            logger.info(f"Open orders for {args.symbol}: {len(open_orders)}")
        except Exception as e:
            logger.error(f"Stage B failed: {e}")

    elif args.stage == "C":
        logger.info("Stage C: Order smoke test")
        if input("DANGER: This will send a limit order. Proceed? (y/N): ").lower() != 'y':
            sys.exit(0)

        try:
            from decimal import Decimal
            import time
            cid = f"SMOKE_{int(time.time())}"
            # Send a tiny limit order far from price
            resp = adapter.submit_limit_order(
                symbol=args.symbol,
                side="BUY",
                quantity=Decimal("0.001"),
                price=Decimal("10000.0"),
                client_order_id=cid
            )
            logger.info(f"Submit response: {resp}")
            if "orderId" in resp:
                logger.info("Canceling...")
                cancel_resp = adapter.cancel_order(args.symbol, cid)
                logger.info(f"Cancel response: {cancel_resp}")
        except Exception as e:
            logger.error(f"Stage C failed: {e}")

if __name__ == "__main__":
    main()
