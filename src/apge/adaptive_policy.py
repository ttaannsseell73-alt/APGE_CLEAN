from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from apge.grid_strategy import MarketRegime

BPS = Decimal("10000")
ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class AdaptivePolicyConfig:
    """Deterministic V1 policy knobs.

    All thresholds are explicit and auditable. No AI/LLM/RL decision is used.
    Rates are decimal fractions unless the field name ends in ``_bps``.
    """

    lookback: int = 30
    slight_trend_bps: Decimal = Decimal("8")
    strong_trend_bps: Decimal = Decimal("35")
    breakout_bps: Decimal = Decimal("20")
    shock_return_bps: Decimal = Decimal("75")
    shock_cooldown_bars: int = 6
    min_spacing_bps: Decimal = Decimal("4")
    max_spacing_bps: Decimal = Decimal("60")
    spread_multiplier: Decimal = Decimal("1.50")
    volatility_multiplier: Decimal = Decimal("2.00")
    trend_bias_fraction: Decimal = Decimal("0.25")
    funding_bias_fraction: Decimal = Decimal("0.15")
    funding_reference_rate: Decimal = Decimal("0.0001")
    max_target_fraction: Decimal = Decimal("0.40")
    slight_trend_size_multiplier: Decimal = Decimal("0.75")
    defensive_size_multiplier: Decimal = Decimal("0.50")

    def validate(self) -> None:
        if self.lookback < 2:
            raise ValueError("lookback must be >= 2")
        if self.shock_cooldown_bars < 1:
            raise ValueError("shock_cooldown_bars must be >= 1")
        if self.shock_cooldown_bars >= self.lookback:
            raise ValueError("shock_cooldown_bars must be < lookback")
        non_negative = (
            self.slight_trend_bps,
            self.strong_trend_bps,
            self.breakout_bps,
            self.shock_return_bps,
            self.min_spacing_bps,
            self.max_spacing_bps,
            self.trend_bias_fraction,
            self.funding_bias_fraction,
            self.max_target_fraction,
        )
        if any(v < 0 for v in non_negative):
            raise ValueError("adaptive thresholds must be non-negative")
        if self.strong_trend_bps < self.slight_trend_bps:
            raise ValueError("strong_trend_bps must be >= slight_trend_bps")
        if self.max_spacing_bps < self.min_spacing_bps:
            raise ValueError("max_spacing_bps must be >= min_spacing_bps")
        if self.funding_reference_rate <= 0:
            raise ValueError("funding_reference_rate must be positive")
        if not (ZERO <= self.max_target_fraction < ONE):
            raise ValueError("max_target_fraction must be in [0, 1)")
        if self.trend_bias_fraction + self.funding_bias_fraction > self.max_target_fraction:
            raise ValueError("combined bias fractions exceed max_target_fraction")
        if self.spread_multiplier <= 0 or self.volatility_multiplier <= 0:
            raise ValueError("spacing multipliers must be positive")
        if not (ZERO < self.slight_trend_size_multiplier <= ONE):
            raise ValueError("slight_trend_size_multiplier must be in (0, 1]")
        if not (ZERO < self.defensive_size_multiplier <= ONE):
            raise ValueError("defensive_size_multiplier must be in (0, 1]")


@dataclass(frozen=True)
class AdaptivePlan:
    market_regime: MarketRegime
    spacing_bps: Decimal
    grid_spacing: Decimal
    inventory_target: Decimal
    size_multiplier: Decimal
    trend_bps: Decimal
    realized_vol_bps: Decimal
    spread_bps: Decimal
    last_return_bps: Decimal
    funding_rate: Decimal


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return min(max(value, lower), upper)


def _returns_bps(prices: Sequence[Decimal]) -> list[Decimal]:
    out: list[Decimal] = []
    for prev, cur in zip(prices, prices[1:]):
        if prev <= 0 or cur <= 0:
            raise ValueError("prices must be positive")
        out.append(((cur / prev) - ONE) * BPS)
    return out


def _mean_abs(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return ZERO
    return sum((abs(v) for v in values), ZERO) / Decimal(len(values))


def classify_regime(
    prices: Sequence[Decimal],
    config: AdaptivePolicyConfig,
) -> tuple[MarketRegime, Decimal, Decimal, Decimal]:
    """Return regime, trend_bps, mean-absolute realized vol bps, last return bps.

    Shock detection is deliberately restart-safe: a shock remains active while
    any return inside the configured trailing cooldown window exceeds the shock
    threshold. No hidden in-memory timer is required.
    """

    config.validate()
    if len(prices) < 2:
        return MarketRegime.NEUTRAL, ZERO, ZERO, ZERO

    window = list(prices[-config.lookback :])
    if any(p <= 0 for p in window):
        raise ValueError("prices must be positive")

    returns = _returns_bps(window)
    trend_bps = ((window[-1] / window[0]) - ONE) * BPS
    realized_vol_bps = _mean_abs(returns)
    last_return_bps = returns[-1]

    shock_window = returns[-config.shock_cooldown_bars :]
    if any(abs(value) >= config.shock_return_bps for value in shock_window):
        return MarketRegime.SHOCK, trend_bps, realized_vol_bps, last_return_bps

    if len(window) >= 3:
        current = window[-1]
        prior = window[:-1]
        prior_high = max(prior)
        prior_low = min(prior)
        breakout_fraction = config.breakout_bps / BPS
        if current >= prior_high * (ONE + breakout_fraction):
            return MarketRegime.BREAKOUT, trend_bps, realized_vol_bps, last_return_bps
        if current <= prior_low * (ONE - breakout_fraction):
            return MarketRegime.BREAKOUT, trend_bps, realized_vol_bps, last_return_bps

    if abs(trend_bps) >= config.strong_trend_bps:
        return MarketRegime.STRONG_TREND, trend_bps, realized_vol_bps, last_return_bps
    if trend_bps >= config.slight_trend_bps:
        return MarketRegime.SLIGHT_UP, trend_bps, realized_vol_bps, last_return_bps
    if trend_bps <= -config.slight_trend_bps:
        return MarketRegime.SLIGHT_DOWN, trend_bps, realized_vol_bps, last_return_bps
    return MarketRegime.NEUTRAL, trend_bps, realized_vol_bps, last_return_bps


def derive_adaptive_plan(
    *,
    prices: Sequence[Decimal],
    best_bid: Decimal,
    best_ask: Decimal,
    max_inventory: Decimal,
    funding_rate: Decimal = ZERO,
    config: AdaptivePolicyConfig | None = None,
) -> AdaptivePlan:
    """Build a deterministic grid plan from observable market state.

    Trend changes the inventory target rather than directly opening a directional
    position. Strong trend/breakout/shock regimes target zero inventory and rely
    on the core grid strategy's risk-reducing behavior.
    """

    cfg = config or AdaptivePolicyConfig()
    cfg.validate()
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ValueError("best bid/ask must be positive and uncrossed")
    if max_inventory <= 0:
        raise ValueError("max_inventory must be positive")
    if funding_rate.is_nan() or funding_rate.is_infinite():
        raise ValueError("funding_rate must be finite")

    regime, trend_bps, realized_vol_bps, last_return_bps = classify_regime(prices, cfg)
    mid = (best_bid + best_ask) / Decimal("2")
    spread_bps = ((best_ask - best_bid) / mid) * BPS

    spacing_bps = max(
        cfg.min_spacing_bps,
        spread_bps * cfg.spread_multiplier,
        realized_vol_bps * cfg.volatility_multiplier,
    )
    spacing_bps = _clamp(spacing_bps, cfg.min_spacing_bps, cfg.max_spacing_bps)
    grid_spacing = mid * spacing_bps / BPS

    trend_target = ZERO
    size_multiplier = ONE
    if regime == MarketRegime.SLIGHT_UP:
        trend_target = max_inventory * cfg.trend_bias_fraction
        size_multiplier = cfg.slight_trend_size_multiplier
    elif regime == MarketRegime.SLIGHT_DOWN:
        trend_target = -max_inventory * cfg.trend_bias_fraction
        size_multiplier = cfg.slight_trend_size_multiplier
    elif regime in (MarketRegime.STRONG_TREND, MarketRegime.BREAKOUT, MarketRegime.SHOCK):
        trend_target = ZERO
        size_multiplier = cfg.defensive_size_multiplier

    funding_target = ZERO
    if regime not in (MarketRegime.STRONG_TREND, MarketRegime.BREAKOUT, MarketRegime.SHOCK):
        normalized_funding = _clamp(
            funding_rate / cfg.funding_reference_rate,
            Decimal("-1"),
            Decimal("1"),
        )
        funding_target = -max_inventory * cfg.funding_bias_fraction * normalized_funding

    target_limit = max_inventory * cfg.max_target_fraction
    inventory_target = _clamp(trend_target + funding_target, -target_limit, target_limit)

    return AdaptivePlan(
        market_regime=regime,
        spacing_bps=spacing_bps,
        grid_spacing=grid_spacing,
        inventory_target=inventory_target,
        size_multiplier=size_multiplier,
        trend_bps=trend_bps,
        realized_vol_bps=realized_vol_bps,
        spread_bps=spread_bps,
        last_return_bps=last_return_bps,
        funding_rate=funding_rate,
    )
