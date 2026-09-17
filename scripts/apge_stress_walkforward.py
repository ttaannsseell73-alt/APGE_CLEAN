import csv
import io
import json
import sys
import zipfile
from dataclasses import replace
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

SCENARIOS = (
    ("baseline", Decimal("0"), Decimal("0.0002")),
    ("confirm_1bp", Decimal("1"), Decimal("0.0002")),
    ("confirm_2bp", Decimal("2"), Decimal("0.0002")),
    ("confirm_1bp_double_fee", Decimal("1"), Decimal("0.0004")),
)


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
        url = _url(day)
        response = requests.get(url, timeout=20)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        rows = _rows_from_zip(response.content)
        if rows:
            days.append((day, rows))
        if len(days) >= required_days:
            break
    if len(days) < required_days:
        raise RuntimeError(f"only {len(days)} daily archives found; required {required_days}")
    days.sort(key=lambda item: item[0])
    return days


def _fold_candles(daily, fold_index):
    chunk = daily[fold_index * FOLD_DAYS:(fold_index + 1) * FOLD_DAYS]
    rows = []
    for _, day_rows in chunk:
        rows.extend(day_rows)
    rows.sort(key=lambda row: int(row[0]))
    timed = parse_usdm_klines(rows, drop_last=False)
    candles = candles_only(timed)
    if len(candles) < 1000:
        raise RuntimeError(f"fold {fold_index + 1} has insufficient candles: {len(candles)}")
    return chunk, candles


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
        daily = _fetch_days(FOLD_DAYS * FOLD_COUNT)
        base = BacktestConfig()
        scenario_results = []

        for name, confirmation_bps, fee_rate in SCENARIOS:
            config = replace(
                base,
                fill_confirmation_bps=confirmation_bps,
                fee_rate=fee_rate,
            )
            folds = []
            aggregate_pnl = Decimal("0")
            positive_folds = 0
            worst_drawdown = Decimal("0")
            max_abs_inventory = Decimal("0")
            total_fills = 0

            for fold_index in range(FOLD_COUNT):
                chunk, candles = _fold_candles(daily, fold_index)
                backtest = run_backtest(candles, config=config)
                if backtest.max_abs_inventory > config.max_inventory:
                    raise RuntimeError(f"{name} fold {fold_index + 1} exceeded inventory limit")
                if backtest.net_pnl > 0:
                    positive_folds += 1
                aggregate_pnl += backtest.net_pnl
                worst_drawdown = max(worst_drawdown, backtest.max_drawdown)
                max_abs_inventory = max(max_abs_inventory, backtest.max_abs_inventory)
                total_fills += backtest.fill_count
                folds.append({
                    "fold": fold_index + 1,
                    "start_day": chunk[0][0].isoformat(),
                    "end_day": chunk[-1][0].isoformat(),
                    "net_pnl": str(backtest.net_pnl),
                    "max_drawdown": str(backtest.max_drawdown),
                    "fill_count": backtest.fill_count,
                })

            scenario_results.append({
                "name": name,
                "fill_confirmation_bps": str(confirmation_bps),
                "fee_rate": str(fee_rate),
                "positive_folds": positive_folds,
                "aggregate_net_pnl": str(aggregate_pnl),
                "worst_fold_drawdown": str(worst_drawdown),
                "max_abs_inventory": str(max_abs_inventory),
                "total_fills": total_fills,
                "folds": folds,
            })

        # This is a structural stress gate, not a profitability approval. Every
        # scenario must remain finite and inside the hard inventory limit.
        if any(Decimal(item["max_abs_inventory"]) > base.max_inventory for item in scenario_results):
            raise RuntimeError("stress scenario exceeded hard inventory limit")

        result["scenarios"] = scenario_results
        result["status"] = "STRESS WALKFORWARD STRUCTURAL PASS"
        result["profitability_gate"] = "NOT_PASSED_OR_FAILED_BY_THIS_STRUCTURAL_GATE"
        Path("apge_stress_walkforward.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        result["status"] = "FAIL_CLOSED"
        result["error"] = str(exc)
        Path("apge_stress_walkforward.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
