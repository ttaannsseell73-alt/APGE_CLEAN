import csv
import io
import json
import sys
import zipfile
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from apge.adaptive_policy import AdaptivePolicyConfig
from apge.backtest import BacktestConfig, run_backtest
from apge.binance_public_data import candles_only, parse_usdm_klines

D = Decimal
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly/klines"
TRAIN_START = date(2026, 4, 1)
TRAIN_END = date(2026, 6, 30)
HOLDOUT_START = date(2026, 7, 1)
HOLDOUT_END = date(2026, 7, 21)
FOLD_DAYS = 7

# Pre-declared before this data window is evaluated. Holdout is never consulted
# by selection. The ranges are intentionally coarse to reduce overfitting.
BASE_SPACING_BPS = tuple(map(D, ("8", "12", "16", "20", "24", "32", "40")))
VOL_MULTIPLIERS = tuple(map(D, ("1.0", "1.5", "2.0", "3.0")))

SELECTION_CONFIG = BacktestConfig(
    fee_rate=D("0.0002"),
    fill_confirmation_bps=D("1"),
    slippage_bps=D("0.5"),
    decision_latency_bars=1,
)
BASELINE_CONFIG = BacktestConfig(fee_rate=D("0.0002"))
STRESS_CONFIG = BacktestConfig(
    fee_rate=D("0.0002"),
    fill_confirmation_bps=D("1"),
    slippage_bps=D("0.5"),
    decision_latency_bars=1,
)
SEVERE_CONFIG = BacktestConfig(
    fee_rate=D("0.0004"),
    fill_confirmation_bps=D("2"),
    slippage_bps=D("1"),
    decision_latency_bars=1,
)


def _archive_url(year: int, month: int) -> str:
    ym = f"{year:04d}-{month:02d}"
    return f"{ARCHIVE_ROOT}/{SYMBOL}/{INTERVAL}/{SYMBOL}-{INTERVAL}-{ym}.zip"


def _read_rows(content: bytes):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError("unexpected Binance Vision archive layout")
        text = archive.read(names[0]).decode("utf-8")
    result = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        try:
            int(row[0])
        except Exception:
            if str(row[0]).lower() in {"open_time", "opentime"}:
                continue
            raise RuntimeError("invalid Binance Vision CSV row")
        result.append(row)
    return result


def _load_rows():
    import requests

    rows = []
    sources = []
    for month in (4, 5, 6, 7):
        url = _archive_url(2026, month)
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        rows.extend(_read_rows(response.content))
        sources.append(url)
    rows.sort(key=lambda row: int(row[0]))
    return rows, sources


def _utc_day(open_time_ms: int) -> date:
    return datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date()


def _filter_rows(rows, start: date, end: date):
    return [row for row in rows if start <= _utc_day(int(row[0])) <= end]


def _daily_groups(rows, start: date, end: date):
    groups = []
    day = start
    while day <= end:
        day_rows = [row for row in rows if _utc_day(int(row[0])) == day]
        if len(day_rows) < 250:
            raise RuntimeError(f"insufficient rows for {day}: {len(day_rows)}")
        groups.append((day, day_rows))
        day = date.fromordinal(day.toordinal() + 1)
    return groups


def _folds(groups):
    if len(groups) % FOLD_DAYS:
        raise RuntimeError("date range must contain complete weekly folds")
    result = []
    for start in range(0, len(groups), FOLD_DAYS):
        chunk = groups[start:start + FOLD_DAYS]
        rows = []
        for _, day_rows in chunk:
            rows.extend(day_rows)
        rows.sort(key=lambda row: int(row[0]))
        candles = candles_only(parse_usdm_klines(rows, drop_last=False))
        if len(candles) < 1900:
            raise RuntimeError("weekly fold has insufficient closed candles")
        result.append((chunk[0][0], chunk[-1][0], candles))
    return result


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / D("2")


def _evaluate_folds(folds, policy, config):
    details = []
    for index, (start, end, candles) in enumerate(folds, 1):
        result = run_backtest(candles, policy_config=policy, config=config)
        if result.max_abs_inventory > config.max_inventory:
            raise RuntimeError("hard inventory limit exceeded")
        details.append({
            "fold": index,
            "start_day": start.isoformat(),
            "end_day": end.isoformat(),
            "net_pnl": str(result.net_pnl),
            "max_drawdown": str(result.max_drawdown),
            "fill_count": result.fill_count,
            "total_fees": str(result.total_fees),
            "total_slippage": str(result.total_slippage),
            "max_abs_inventory": str(result.max_abs_inventory),
        })
    pnls = [D(item["net_pnl"]) for item in details]
    drawdowns = [D(item["max_drawdown"]) for item in details]
    return {
        "folds": details,
        "positive_folds": sum(pnl > 0 for pnl in pnls),
        "aggregate_pnl": str(sum(pnls, D("0"))),
        "median_pnl": str(_median(pnls)),
        "worst_drawdown": str(max(drawdowns)),
    }


def main() -> int:
    evidence = {
        "status": "FAIL_CLOSED",
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "read_only": True,
        "orders_submitted": 0,
        "selection_uses_holdout": False,
        "train_window": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
        "holdout_window": [HOLDOUT_START.isoformat(), HOLDOUT_END.isoformat()],
        "candidate_space": {
            "base_spacing_bps": [str(value) for value in BASE_SPACING_BPS],
            "volatility_spacing_multiplier": [str(value) for value in VOL_MULTIPLIERS],
        },
        "selection_execution_model": {
            "fee_rate": str(SELECTION_CONFIG.fee_rate),
            "fill_confirmation_bps": str(SELECTION_CONFIG.fill_confirmation_bps),
            "slippage_bps": str(SELECTION_CONFIG.slippage_bps),
            "decision_latency_bars": SELECTION_CONFIG.decision_latency_bars,
        },
    }
    try:
        rows, sources = _load_rows()
        train_groups = _daily_groups(_filter_rows(rows, TRAIN_START, TRAIN_END), TRAIN_START, TRAIN_END)
        holdout_groups = _daily_groups(_filter_rows(rows, HOLDOUT_START, HOLDOUT_END), HOLDOUT_START, HOLDOUT_END)
        train_folds = _folds(train_groups)
        holdout_folds = _folds(holdout_groups)

        candidates = []
        for spacing in BASE_SPACING_BPS:
            for multiplier in VOL_MULTIPLIERS:
                policy = replace(
                    AdaptivePolicyConfig(),
                    base_spacing_bps=spacing,
                    volatility_spacing_multiplier=multiplier,
                )
                result = _evaluate_folds(train_folds, policy, SELECTION_CONFIG)
                score = (
                    result["positive_folds"],
                    D(result["median_pnl"]),
                    D(result["aggregate_pnl"]),
                    -D(result["worst_drawdown"]),
                )
                candidates.append({
                    "base_spacing_bps": str(spacing),
                    "volatility_spacing_multiplier": str(multiplier),
                    "training": result,
                    "_score": score,
                })

        selected = max(candidates, key=lambda item: item["_score"])
        selected_policy = replace(
            AdaptivePolicyConfig(),
            base_spacing_bps=D(selected["base_spacing_bps"]),
            volatility_spacing_multiplier=D(selected["volatility_spacing_multiplier"]),
        )

        baseline = _evaluate_folds(holdout_folds, selected_policy, BASELINE_CONFIG)
        stress = _evaluate_folds(holdout_folds, selected_policy, STRESS_CONFIG)
        severe = _evaluate_folds(holdout_folds, selected_policy, SEVERE_CONFIG)

        baseline_pass = baseline["positive_folds"] >= 2 and D(baseline["aggregate_pnl"]) > 0
        stress_pass = stress["positive_folds"] >= 2 and D(stress["aggregate_pnl"]) > 0
        robustness_pass = baseline_pass and stress_pass

        evidence.update({
            "sources": sources,
            "train_fold_count": len(train_folds),
            "holdout_fold_count": len(holdout_folds),
            "candidates": [
                {key: value for key, value in item.items() if key != "_score"}
                for item in candidates
            ],
            "selected": {key: value for key, value in selected.items() if key != "_score"},
            "holdout_baseline": baseline,
            "holdout_stress": stress,
            "holdout_severe_non_gate": severe,
            "baseline_gate": baseline_pass,
            "stress_gate": stress_pass,
            "robustness_gate": "PASS" if robustness_pass else "NOT_PASSED",
            "status": "FIXED HISTORICAL ROBUSTNESS COMPLETE",
        })
        Path("apge_fixed_historical_robustness.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True)
        )
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        evidence["error"] = str(exc)
        Path("apge_fixed_historical_robustness.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True)
        )
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
