import csv
import io
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from apge.backtest import BacktestConfig, run_backtest
from apge.binance_public_data import candles_only, parse_usdm_klines

PUBLIC_ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/daily/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
DAYS_REQUIRED = 3
MAX_LOOKBACK_DAYS = 10


def _archive_url(day) -> str:
    date_text = day.isoformat()
    return (
        f"{PUBLIC_ARCHIVE_ROOT}/{SYMBOL}/{INTERVAL}/"
        f"{SYMBOL}-{INTERVAL}-{date_text}.zip"
    )


def _read_zip_rows(content: bytes):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError("unexpected Binance Vision archive layout")
        raw = archive.read(names[0]).decode("utf-8")

    rows = []
    for row in csv.reader(io.StringIO(raw)):
        if not row:
            continue
        try:
            int(row[0])
        except Exception:
            # Binance Vision may include a CSV header in some datasets.
            if str(row[0]).lower() in {"open_time", "opentime"}:
                continue
            raise RuntimeError("invalid Binance Vision CSV row")
        rows.append(row)
    return rows


def _fetch_rows():
    import requests

    today = datetime.now(timezone.utc).date()
    collected = []
    sources = []
    for offset in range(1, MAX_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=offset)
        url = _archive_url(day)
        response = requests.get(url, timeout=20)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        day_rows = _read_zip_rows(response.content)
        if not day_rows:
            continue
        collected.extend(day_rows)
        sources.append(url)
        if len(sources) >= DAYS_REQUIRED:
            break

    if len(sources) < DAYS_REQUIRED:
        raise RuntimeError(
            f"only {len(sources)} Binance Vision daily archives available; "
            f"required {DAYS_REQUIRED}"
        )

    collected.sort(key=lambda row: int(row[0]))
    return collected, sources


def _finite_decimal(value: Decimal) -> bool:
    return isinstance(value, Decimal) and not value.is_nan() and not value.is_infinite()


def main() -> int:
    result = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "source": "Binance Vision USD-M Futures public daily archives",
        "read_only": True,
        "authenticated": False,
        "orders_submitted": 0,
    }
    try:
        rows, sources = _fetch_rows()
        # Archive files contain completed historical days, so no open candle needs
        # to be removed here.
        timed = parse_usdm_klines(rows, drop_last=False)
        candles = candles_only(timed)
        if len(candles) < 500:
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
            "archives": sources,
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
