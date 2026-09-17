import json
import math
import sys
from decimal import Decimal
from pathlib import Path

from apge.backtest import BacktestConfig, run_backtest
from apge.binance_public_data import candles_only, parse_usdm_klines

PUBLIC_FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
LIMIT = 1000


def _fetch_rows():
    import requests

    response = requests.get(
        PUBLIC_FUTURES_URL,
        params={"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("unexpected Binance public kline payload")
    return payload


def _finite_decimal(value: Decimal) -> bool:
    return isinstance(value, Decimal) and not value.is_nan() and not value.is_infinite()


def main() -> int:
    result = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "source": PUBLIC_FUTURES_URL,
        "read_only": True,
        "authenticated": False,
        "orders_submitted": 0,
    }
    try:
        rows = _fetch_rows()
        timed = parse_usdm_klines(rows, drop_last=True)
        candles = candles_only(timed)
        if len(candles) < 100:
            raise RuntimeError(f"insufficient closed public candles: {len(candles)}")

        config = BacktestConfig()
        backtest = run_backtest(candles, config=config)
        metrics = (
            backtest.final_equity,
            backtest.net_pnl,
            backtest.max_drawdown,
            backtest.max_abs_inventory,
            backtest.total_fees,
            backtest.total_funding,
            backtest.turnover,
        )
        if not all(_finite_decimal(value) for value in metrics):
            raise RuntimeError("non-finite backtest metric")
        if backtest.max_abs_inventory > config.max_inventory:
            raise RuntimeError("backtest exceeded hard inventory limit")
        if backtest.bars_processed <= 0:
            raise RuntimeError("backtest processed no bars")

        result.update({
            "closed_candles": len(candles),
            "first_open_time_ms": timed[0].open_time_ms,
            "last_close_time_ms": timed[-1].close_time_ms,
            "bars_processed": backtest.bars_processed,
            "fill_count": backtest.fill_count,
            "initial_equity": str(backtest.initial_equity),
            "final_equity": str(backtest.final_equity),
            "net_pnl": str(backtest.net_pnl),
            "max_drawdown": str(backtest.max_drawdown),
            "max_abs_inventory": str(backtest.max_abs_inventory),
            "max_inventory_limit": str(config.max_inventory),
            "total_fees": str(backtest.total_fees),
            "turnover": str(backtest.turnover),
            "status": "PUBLIC MARKET DATA VALIDATION PASS",
        })
        Path("apge_public_market_validation.json").write_text(
            json.dumps(result, indent=2, sort_keys=True)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        result["status"] = "FAIL_CLOSED"
        result["error"] = str(exc)
        Path("apge_public_market_validation.json").write_text(
            json.dumps(result, indent=2, sort_keys=True)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
