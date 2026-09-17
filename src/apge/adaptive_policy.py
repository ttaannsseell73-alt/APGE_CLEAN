from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from apge.grid_strategy import MarketRegime
from apge.market_regime import RegimeAssessment


@dataclass(frozen=True)
class AdaptivePolicyConfig:
    base_spacing_bps: Decimal = Decimal("8")
    min_spacing_bps: Decimal = Decimal("5")
    max_spacing_bps: Decimal = Decimal("60")
    volatility_spacing_multiplier: Decimal = Decimal("1.5")
    min_size_scale: Decimal = Decimal("0.40")
    size_decay_bps: Decimal = Decimal("35")
    slight_trend_inventory_bias_fraction: Decimal = Decimal("0.20")
    funding_bias_cap_fraction: Decimal = Decimal("0.08")
    funding_reference_rate: Decimal = Decimal("0.0001")
    target_inventory_cap_fraction: Decimal = Decimal("0.30")
    inventory_skew_strength: Decimal = Decimal("1.0")


@dataclass(frozen=True)
class AdaptiveGridDecision:
    regime: MarketRegime
    grid_spacing: Decimal
    base_size: Decimal
    target_inventory: Decimal
    inventory_skew_strength: Decimal
    risk_increasing_allowed: bool


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(high, max(low, value))


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def build_adaptive_grid_decision(
    *,
    best_bid: Decimal,
    best_ask: Decimal,
    current_inventory: Decimal,
    max_inventory: Decimal,
    nominal_base_size: Decimal,
    step_size: Decimal,
    assessment: RegimeAssessment,
    funding_rate: Decimal = Decimal("0"),
    config: AdaptivePolicyConfig = AdaptivePolicyConfig(),
) -> AdaptiveGridDecision:
    """Translate deterministic market state into bounded grid parameters.

    Funding is intentionally a small modifier. It can tilt the inventory target,
    but cannot independently enable risk in STRONG_TREND, BREAKOUT or SHOCK.
    """
    scalars = (
        best_bid,
        best_ask,
        current_inventory,
        max_inventory,
        nominal_base_size,
        step_size,
        funding_rate,
        assessment.average_abs_return,
    )
    if any((not isinstance(v, Decimal) or v.is_nan() or v.is_infinite()) for v in scalars):
        raise ValueError("adaptive policy inputs must be finite Decimals")
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        raise ValueError("invalid best bid/ask")
    if max_inventory <= 0 or nominal_base_size <= 0 or step_size <= 0:
        raise ValueError("inventory, base size and step size must be positive")
    if abs(current_inventory) > max_inventory:
        raise ValueError("current inventory exceeds configured maximum")

    midpoint = (best_bid + best_ask) / Decimal("2")
    volatility_bps = assessment.average_abs_return * Decimal("10000")
    spacing_bps = config.base_spacing_bps + volatility_bps * config.volatility_spacing_multiplier
    spacing_bps = _clamp(spacing_bps, config.min_spacing_bps, config.max_spacing_bps)
    grid_spacing = midpoint * spacing_bps / Decimal("10000")

    size_scale = Decimal("1") / (Decimal("1") + volatility_bps / config.size_decay_bps)
    size_scale = max(config.min_size_scale, min(Decimal("1"), size_scale))
    base_size = _floor_to_step(nominal_base_size * size_scale, step_size)
    if base_size <= 0:
        base_size = step_size
    base_size = min(base_size, nominal_base_size)

    regime_bias = Decimal("0")
    if assessment.regime == MarketRegime.SLIGHT_UP:
        regime_bias = max_inventory * config.slight_trend_inventory_bias_fraction
    elif assessment.regime == MarketRegime.SLIGHT_DOWN:
        regime_bias = -max_inventory * config.slight_trend_inventory_bias_fraction

    if config.funding_reference_rate <= 0:
        raise ValueError("funding reference rate must be positive")
    normalized_funding = _clamp(
        funding_rate / config.funding_reference_rate,
        Decimal("-1"),
        Decimal("1"),
    )
    # Positive funding makes long inventory more expensive; negative funding does
    # the opposite. The contribution is capped so funding stays a modifier only.
    funding_bias = -normalized_funding * max_inventory * config.funding_bias_cap_fraction

    target_cap = max_inventory * config.target_inventory_cap_fraction
    target_inventory = _clamp(regime_bias + funding_bias, -target_cap, target_cap)

    risk_increasing_allowed = assessment.regime not in (
        MarketRegime.STRONG_TREND,
        MarketRegime.BREAKOUT,
        MarketRegime.SHOCK,
    )
    if not risk_increasing_allowed:
        # In risk-reducing regimes the GridStrategy uses current inventory to
        # generate only exposure-reducing proposals. Do not let a target tilt
        # interfere with that fail-closed path.
        target_inventory = Decimal("0")

    return AdaptiveGridDecision(
        regime=assessment.regime,
        grid_spacing=grid_spacing,
        base_size=base_size,
        target_inventory=target_inventory,
        inventory_skew_strength=config.inventory_skew_strength,
        risk_increasing_allowed=risk_increasing_allowed,
    )
