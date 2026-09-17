import csv
import io
import json
import statistics
import sys
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from apge.adaptive_policy import AdaptivePolicyConfig
from apge.backtest import BacktestConfig, run_backtest
from apge.binance_public_data import candles_only, parse_usdm_klines

ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/daily/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
FOLD_DAYS = 7
TRAIN_FOLDS = 6
HOLDOUT_FOLDS = 2
TOTAL_DAYS = FOLD_DAYS * (TRAIN_FOLDS + HOLDOUT_FOLDS)
MAX_LOOKBACK_DAYS = 100

# Deliberately small, pre-declared search space. Only spacing parameters are
# searched; holdout data is never used to select a candidate.
BASE_SPACING_BPS = (Decimal("8"), Decimal("12"), Decimal("16"), Decimal("24"))
VOL_MULTIPLIERS = (Decimal("1.0"), Decimal("1.5"), Decimal("2.0"))


def _url(day) -> str:
    d = day.isoformat()
    return f"{ARCHIVE_ROOT}/{SYMBOL}/{INTERVAL}/{SYMBOL}-{INTERVAL}-{d}.zip"


def _read_rows(content: bytes):
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
        rows = _read_rows(response.content)
        if rows:
            days.append((day, rows))
        if len(days) >= required_days:
            break
    if len(days) < required_days:
        raise RuntimeError(f"only {len(days)} daily archives found; required {required_days}")
    days.sort(key=lambda item: item[0])
    return days


def _fold(daily, index: int):
    chunk = daily[index * FOLD_DAYS:(index + 1) * FOLD_DAYS]
    rows = []
    for _, day_rows in chunk:
        rows.extend(day_rows)
    rows.sort(key=lambda row: int(row[0]))
    candles = candles_only(parse_usdm_klines(rows, drop_last=False))
    if len(candles) < 1000:
        raise RuntimeError(f"fold {index + 1} has insufficient candles")
    return chunk, candles


def _evaluate(candles, policy, backtest_config):
    result = run_backtest(candles, policy_config=policy, config=backtest_config)
    if result.max_abs_inventory > backtest_config.max_inventory:
        raise RuntimeError("candidate exceeded hard inventory limit")
    return result


def _median_decimal(values):
    ordered = sorted(values)
    n = len(ordered)
    if n % 2:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / Decimal("2")


def main() -> int:
    evidence = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "train_folds": TRAIN_FOLDS,
        "holdout_folds": HOLDOUT_FOLDS,
        "fold_days": FOLD_DAYS,
        "read_only": True,
        "orders_submitted": 0,
        "selection_uses_holdout": False,
        "source": "Binance Vision USD-M Futures public daily archives",
    }
    try:
        daily = _fetch_days(TOTAL_DAYS)
        folds = [_fold(daily, i) for i in range(TRAIN_FOLDS + HOLDOUT_FOLDS)]
        base_backtest = BacktestConfig()
        candidates = []

        for spacing in BASE_SPACING_BPS:
            for multiplier in VOL_MULTIPLIERS:
                policy = replace(
                    AdaptivePolicyConfig(),
                    base_spacing_bps=spacing,
                    volatility_spacing_multiplier=multiplier,
                )
                fold_results = []
                for fold_index in range(TRAIN_FOLDS):
                    chunk, candles = folds[fold_index]
                    bt = _evaluate(candles, policy, base_backtest)
                    fold_results.append({
                        "fold": fold_index + 1,
                        "start_day": chunk[0][0].isoformat(),
                        "end_day": chunk[-1][0].isoformat(),
                        "net_pnl": str(bt.net_pnl),
                        "max_drawdown": str(bt.max_drawdown),
                        "fill_count": bt.fill_count,
                    })

                pnls = [Decimal(item["net_pnl"]) for item in fold_results]
                drawdowns = [Decimal(item["max_drawdown"]) for item in fold_results]
                positive = sum(1 for pnl in pnls if pnl > 0)
                aggregate = sum(pnls, Decimal("0"))
                median = _median_decimal(pnls)
                worst_dd = max(drawdowns)
                # Selection is deterministic and train-only. Prioritize consistency,
                # then median weekly PnL, aggregate PnL, then smaller worst drawdown.
                score = (positive, median, aggregate, -worst_dd)
                candidates.append({
                    "base_spacing_bps": str(spacing),
                    "volatility_spacing_multiplier": str(multiplier),
                    "positive_train_folds": positive,
                    "median_train_pnl": str(median),
                    "aggregate_train_pnl": str(aggregate),
                    "worst_train_drawdown": str(worst_dd),
                    "train_folds_detail": fold_results,
                    "_score": score,
                })

        selected = max(candidates, key=lambda item: item["_score"])
        selected_policy = replace(
            AdaptivePolicyConfig(),
            base_spacing_bps=Decimal(selected["base_spacing_bps"]),
            volatility_spacing_multiplier=Decimal(selected["volatility_spacing_multiplier"]),
        )

        holdout = []
        for fold_index in range(TRAIN_FOLDS, TRAIN_FOLDS + HOLDOUT_FOLDS):
            chunk, candles = folds[fold_index]
            bt = _evaluate(candles, selected_policy, base_backtest)
            holdout.append({
                "fold": fold_index - TRAIN_FOLDS + 1,
                "start_day": chunk[0][0].isoformat(),
                "end_day": chunk[-1][0].isoformat(),
                "net_pnl": str(bt.net_pnl),
                "max_drawdown": str(bt.max_drawdown),
                "max_abs_inventory": str(bt.max_abs_inventory),
                "fill_count": bt.fill_count,
            })

        # One pre-declared execution-friction stress on the untouched holdout.
        stress_config = replace(base_backtest, fill_confirmation_bps=Decimal("1"))
        holdout_stress = []
        for fold_index in range(TRAIN_FOLDS, TRAIN_FOLDS + HOLDOUT_FOLDS):
            chunk, candles = folds[fold_index]
            bt = _evaluate(candles, selected_policy, stress_config)
            holdout_stress.append({
                "fold": fold_index - TRAIN_FOLDS + 1,
                "start_day": chunk[0][0].isoformat(),
                "end_day": chunk[-1][0].isoformat(),
                "net_pnl": str(bt.net_pnl),
                "max_drawdown": str(bt.max_drawdown),
                "fill_count": bt.fill_count,
            })

        clean_candidates = []
        for item in candidates:
            clean = dict(item)
            clean.pop("_score")
            clean_candidates.append(clean)

        holdout_pnls = [Decimal(item["net_pnl"]) for item in holdout]
        stress_pnls = [Decimal(item["net_pnl"]) for item in holdout_stress]
        evidence.update({
            "train_start_day": folds[0][0][0][0].isoformat(),
            "train_end_day": folds[TRAIN_FOLDS - 1][0][-1][0].isoformat(),
            "holdout_start_day": folds[TRAIN_FOLDS][0][0][0].isoformat(),
            "holdout_end_day": folds[-1][0][-1][0].isoformat(),
            "candidates": clean_candidates,
            "selected": {
                key: value for key, value in selected.items() if key != "_score"
            },
            "holdout": holdout,
            "holdout_positive_folds": sum(1 for pnl in holdout_pnls if pnl > 0),
            "holdout_aggregate_pnl": str(sum(holdout_pnls, Decimal("0"))),
            "holdout_stress_1bp": holdout_stress,
            "holdout_stress_positive_folds": sum(1 for pnl in stress_pnls if pnl > 0),
            "holdout_stress_aggregate_pnl": str(sum(stress_pnls, Decimal("0"))),
            "status": "TRAIN_HOLDOUT RESEARCH COMPLETE",
        })
        Path("apge_train_holdout_research.json").write_text(json.dumps(evidence, indent=2, sort_keys=True))
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        evidence["status"] = "FAIL_CLOSED"
        evidence["error"] = str(exc)
        Path("apge_train_holdout_research.json").write_text(json.dumps(evidence, indent=2, sort_keys=True))
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
