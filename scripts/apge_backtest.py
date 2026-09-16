#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from apge.backtest import AdaptiveBacktester, parse_binance_klines

PUBLIC_FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"


def fetch_public_klines(symbol: str, interval: str, limit: int):
    import requests

    if limit < 10 or limit > 1500:
        raise ValueError("limit must be between 10 and 1500")
    response = requests.get(
        PUBLIC_FUTURES_URL,
        params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("unexpected Binance public kline response")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="APGE V1 deterministic research backtest")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--input-json", type=Path, default=None,
                        help="Optional Binance-style kline JSON file; skips network fetch.")
    parser.add_argument("--output", type=Path, default=Path("apge_backtest_result.json"))
    args = parser.parse_args()

    if args.input_json is not None:
        rows = json.loads(args.input_json.read_text())
    else:
        rows = fetch_public_klines(args.symbol, args.interval, args.limit)

    candles = parse_binance_klines(rows)
    result, fills = AdaptiveBacktester().run(candles)
    payload = {
        "symbol": args.symbol.upper(),
        "interval": args.interval,
        "research_only": True,
        "orders_sent": False,
        "result": result.to_dict(),
        "fills_preview": [
            {
                "timestamp_ms": f.timestamp_ms,
                "side": f.side,
                "price": str(f.price),
                "quantity": str(f.quantity),
                "fee": str(f.fee),
                "regime": f.regime,
            }
            for f in fills[:25]
        ],
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
