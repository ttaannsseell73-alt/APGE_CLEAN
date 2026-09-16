from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Sequence

from apge.backtest import AdaptiveBacktester, BacktestConfig, BacktestResult, Candle

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class ResearchGateConfig:
    """Engineering promotion gate for fixed-policy historical evidence.

    Passing this gate is not a profitability guarantee. It only means that a
    fixed deterministic configuration met explicit minimum evidence thresholds
    on the supplied sequential segments.
    """

    window_size: int = 120
    min_windows: int = 4
    min_total_fills: int = 40
    min_positive_window_fraction: Decimal = Decimal("0.55")
    max_drawdown_fraction: Decimal = Decimal("0.10")
    require_positive_total_pnl: bool = True
    require_positive_median_window_pnl: bool = True
    fee_stress_multiplier: Decimal = Decimal("2")

    def validate(self) -> None:
        if self.window_size < 20:
            raise ValueError("window_size must be >= 20")
        if self.min_windows < 2:
            raise ValueError("min_windows must be >= 2")
        if self.min_total_fills < 1:
            raise ValueError("min_total_fills must be >= 1")
        if not (ZERO <= self.min_positive_window_fraction <= ONE):
            raise ValueError("min_positive_window_fraction must be in [0,1]")
        if not (ZERO < self.max_drawdown_fraction < ONE):
            raise ValueError("max_drawdown_fraction must be in (0,1)")
        if self.fee_stress_multiplier < ONE:
            raise ValueError("fee_stress_multiplier must be >= 1")


@dataclass(frozen=True)
class ResearchGateResult:
    passed: bool
    reasons: tuple[str, ...]
    windows: int
    positive_windows: int
    positive_window_fraction: Decimal
    total_pnl: Decimal
    median_window_pnl: Decimal
    total_fills: int
    worst_drawdown_fraction: Decimal
    stressed_total_pnl: Decimal
    stressed_worst_drawdown_fraction: Decimal
    stressed_total_fills: int

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "windows": self.windows,
            "positive_windows": self.positive_windows,
            "positive_window_fraction": str(self.positive_window_fraction),
            "total_pnl": str(self.total_pnl),
            "median_window_pnl": str(self.median_window_pnl),
            "total_fills": self.total_fills,
            "worst_drawdown_fraction": str(self.worst_drawdown_fraction),
            "stressed_total_pnl": str(self.stressed_total_pnl),
            "stressed_worst_drawdown_fraction": str(self.stressed_worst_drawdown_fraction),
            "stressed_total_fills": self.stressed_total_fills,
        }


def _segment(candles: Sequence[Candle], window_size: int) -> list[Sequence[Candle]]:
    return [
        candles[i : i + window_size]
        for i in range(0, len(candles), window_size)
        if len(candles[i : i + window_size]) == window_size
    ]


def _aggregate(results: Sequence[BacktestResult]) -> tuple[Decimal, Decimal, int, Decimal, int]:
    if not results:
        return ZERO, ZERO, 0, ZERO, 0
    pnls = [r.pnl for r in results]
    positive = sum(1 for pnl in pnls if pnl > ZERO)
    return (
        sum(pnls, ZERO),
        Decimal(str(median(pnls))),
        sum(r.fills for r in results),
        max((r.max_drawdown_fraction for r in results), default=ZERO),
        positive,
    )


def evaluate_research_gate(
    candles: Sequence[Candle],
    *,
    backtest_config: BacktestConfig | None = None,
    gate_config: ResearchGateConfig | None = None,
) -> ResearchGateResult:
    """Evaluate one fixed strategy configuration across sequential windows.

    No parameter optimization occurs inside this function. The same policy is
    used in every window, which makes the gate suitable as a reproducible
    promotion check rather than an in-sample optimizer.
    """

    bt_cfg = backtest_config or BacktestConfig()
    gate = gate_config or ResearchGateConfig()
    bt_cfg.validate()
    gate.validate()

    windows = _segment(candles, gate.window_size)
    reasons: list[str] = []
    if len(windows) < gate.min_windows:
        reasons.append("INSUFFICIENT_WINDOWS")

    normal_results: list[BacktestResult] = []
    stress_results: list[BacktestResult] = []

    stressed_cfg = BacktestConfig(
        initial_cash=bt_cfg.initial_cash,
        maker_fee_rate=bt_cfg.maker_fee_rate * gate.fee_stress_multiplier,
        synthetic_spread_bps=bt_cfg.synthetic_spread_bps,
        base_size=bt_cfg.base_size,
        level_count=bt_cfg.level_count,
        max_inventory=bt_cfg.max_inventory,
        tick_size=bt_cfg.tick_size,
        step_size=bt_cfg.step_size,
        min_history=bt_cfg.min_history,
        adaptive=bt_cfg.adaptive,
        guard=bt_cfg.guard,
    )
    stressed_cfg.validate()

    for window in windows:
        normal, _ = AdaptiveBacktester(bt_cfg).run(window)
        stressed, _ = AdaptiveBacktester(stressed_cfg).run(window)
        normal_results.append(normal)
        stress_results.append(stressed)

    total_pnl, median_pnl, total_fills, worst_dd, positive_windows = _aggregate(normal_results)
    stressed_pnl, _, stressed_fills, stressed_worst_dd, _ = _aggregate(stress_results)
    fraction = (
        Decimal(positive_windows) / Decimal(len(normal_results))
        if normal_results else ZERO
    )

    if total_fills < gate.min_total_fills:
        reasons.append("INSUFFICIENT_FILLS")
    if fraction < gate.min_positive_window_fraction:
        reasons.append("POSITIVE_WINDOW_FRACTION")
    if worst_dd > gate.max_drawdown_fraction:
        reasons.append("MAX_DRAWDOWN")
    if gate.require_positive_total_pnl and total_pnl <= ZERO:
        reasons.append("TOTAL_PNL_NOT_POSITIVE")
    if gate.require_positive_median_window_pnl and median_pnl <= ZERO:
        reasons.append("MEDIAN_WINDOW_PNL_NOT_POSITIVE")
    if stressed_pnl <= ZERO:
        reasons.append("FEE_STRESS_PNL_NOT_POSITIVE")
    if stressed_worst_dd > gate.max_drawdown_fraction:
        reasons.append("FEE_STRESS_MAX_DRAWDOWN")
    if any(result.halted for result in normal_results):
        reasons.append("RISK_GUARD_HALTED")
    if any(result.halted for result in stress_results):
        reasons.append("FEE_STRESS_RISK_GUARD_HALTED")

    return ResearchGateResult(
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        windows=len(normal_results),
        positive_windows=positive_windows,
        positive_window_fraction=fraction,
        total_pnl=total_pnl,
        median_window_pnl=median_pnl,
        total_fills=total_fills,
        worst_drawdown_fraction=worst_dd,
        stressed_total_pnl=stressed_pnl,
        stressed_worst_drawdown_fraction=stressed_worst_dd,
        stressed_total_fills=stressed_fills,
    )
