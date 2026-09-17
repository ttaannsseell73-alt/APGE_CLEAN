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

ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/daily/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
FOLD_DAYS = 7
FOLD_COUNT = 4
MAX_LOOKBACK_DAYS = 45


def _url(day) -> str:
    d = day.isoformat()
    return f"{ARCHIVE_ROOT}/{SYMBOL}/{INTERVAL}/{SYMBOL}-{INTERVAL}-{d}.zip"


def _rows_from_zip(content: bytes):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError("unexpected Binance Vision archive layout")
        text = archive.read(names[0]).decode("utf-8")
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        try:
            int(row[0])
        except Exception:
            if str(row[0]).lower() in {"open_time", "opentime"}:
                continue
            raise RuntimeError("invalid Binance Vision CSV row")
        rows.append(row)
    return rows


def _fetch_days(required_days: int):
    import requests

    today = datetime.now(timezone.utc).date()
    days = []
    for offset in range(1, MAX_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=offset)
        response = requests.get(_url(day), timeout=20)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        rows = _rows_from_zip(response.content)
        if rows:
            days.append((day, rows, _url(day)))
        if len(days) >= required_days:
            break
    if len(days) < required_days:
        raise RuntimeError(f"only {len(days)} daily archives found; required {required_days}")
    days.sort(key=lambda item: item[0])
    return days


def _finite(value: Decimal) -> bool:
    return not value.is_nan() and not value.is_infinite()


def main() -> int:
    result = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "fold_days": FOLD_DAYS,
        "fold_count": FOLD_COUNT,
        "read_only": True,
        "authenticated": False,
        "orders_submitted": 0,
        "source": "Binance Vision USD-M Futures public daily archives",
    }
    try:
        needed = FOLD_DAYS * FOLD_COUNT
        daily = _fetch_days(needed)
        config = BacktestConfig()
        folds = []
        aggregate_pnl = Decimal("0")
        aggregate_fees = Decimal("0")
        aggregate_turnover = Decimal("0")
        worst_drawdown = Decimal("0")
        max_inventory = Decimal("0")
        positive_folds = 0
        total_fills = 0
        total_bars = 0

        for fold_index in range(FOLD_COUNT):
            chunk = daily[fold_index * FOLD_DAYS:(fold_index + 1) * FOLD_DAYS]
            rows = []
            sources = []
            for _, day_rows, source in chunk:
                rows.extend(day_rows)
                sources.append(source)
            rows.sort(key=lambda row: int(row[0]))
            timed = parse_usdm_klines(rows, drop_last=False)
            candles = candles_only(timed)
            if len(candles) < 1000:
                raise RuntimeError(f"fold {fold_index} has insufficient candles: {len(candles)}")

            backtest = run_backtest(candles, config=config)
            metrics = (
                backtest.final_equity,
                backtest.net_pnl,
                backtest.max_drawdown,
                backtest.max_abs_inventory,
                backtest.total_fees,
                backtest.turnover,
            )
            if not all(_finite(value) for value in metrics):
                raise RuntimeError(f"fold {fold_index} produced non-finite metrics")
            if backtest.max_abs_inventory > config.max_inventory:
                raise RuntimeError(f"fold {fold_index} exceeded inventory limit")

            if backtest.net_pnl > 0:
                positive_folds += 1
            aggregate_pnl += backtest.net_pnl
            aggregate_fees += backtest.total_fees
            aggregate_turnover += backtest.turnover
            worst_drawdown = max(worst_drawdown, backtest.max_drawdown)
            max_inventory = max(max_inventory, backtest.max_abs_inventory)
            total_fills += backtest.fill_count
            total_bars += backtest.bars_processed

            folds.append({
                "fold": fold_index + 1,
                "start_day": chunk[0][0].isoformat(),
                "end_day": chunk[-1][0].isoformat(),
                "closed_candles": len(candles),
                "bars_processed": backtest.bars_processed,
                "fill_count": backtest.fill_count,
                "net_pnl": str(backtest.net_pnl),
                "max_drawdown": str(backtest.max_drawdown),
                "max_abs_inventory": str(backtest.max_abs_inventory),
                "total_fees": str(backtest.total_fees),
                "turnover": str(backtest.turnover),
                "sources": sources,
            })

        result.update({
            "folds": folds,
            "positive_folds": positive_folds,
            "aggregate_net_pnl": str(aggregate_pnl),
            "aggregate_total_fees": str(aggregate_fees),
            "aggregate_turnover": str(aggregate_turnover),
            "worst_fold_drawdown": str(worst_drawdown),
            "max_abs_inventory": str(max_inventory),
            "max_inventory_limit": str(config.max_inventory),
            "total_fills": total_fills,
            "total_bars_processed": total_bars,
            "status": "WALKFORWARD STRUCTURAL VALIDATION PASS",
            "profitability_gate": "NOT_EVALUATED_AS_DEPLOYMENT_APPROVAL",
        })
        Path("apge_walkforward_validation.json").write_text(
            json.dumps(result, indent=2, sort_keys=True)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        result["status"] = "FAIL_CLOSED"
        result["error"] = str(exc)
        Path("apge_walkforward_validation.json").write_text(
            json.dumps(result, indent=2, sort_keys=True)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
