from decimal import Decimal

import pytest

from apge.adaptive_policy import (
    AdaptivePolicyConfig,
    classify_regime,
    derive_adaptive_plan,
)
from apge.grid_strategy import MarketRegime, generate_grid_proposals
from apge.simulator import SystemState

D = Decimal


def test_regime_classifier_neutral_slight_strong_breakout_shock():
    cfg = AdaptivePolicyConfig()

    neutral, *_ = classify_regime([D("100"), D("100.02"), D("100.01")], cfg)
    assert neutral == MarketRegime.NEUTRAL

    slight, *_ = classify_regime([D("100"), D("100.05"), D("100.10"), D("100.16")], cfg)
    assert slight == MarketRegime.SLIGHT_UP

    strong, *_ = classify_regime(
        [D("100"), D("100.10"), D("100.20"), D("100.30"), D("100.40")], cfg)
    assert strong == MarketRegime.STRONG_TREND

    breakout, *_ = classify_regime([D("100"), D("100.05"), D("100.04"), D("100.30")], cfg)
    assert breakout == MarketRegime.BREAKOUT

    shock, *_ = classify_regime([D("100"), D("100.1"), D("101.0")], cfg)
    assert shock == MarketRegime.SHOCK


def test_shock_cooldown_is_restart_safe_and_expires_after_calm_bars():
    cfg = AdaptivePolicyConfig(
        lookback=12,
        shock_return_bps=D("75"),
        shock_cooldown_bars=3,
        strong_trend_bps=D("1000"),
        breakout_bps=D("1000"),
    )
    # 100 -> 101 is a shock. Two calm returns afterwards still retain SHOCK.
    retained, *_ = classify_regime(
        [D("100"), D("101"), D("101.01"), D("101.02")], cfg)
    assert retained == MarketRegime.SHOCK

    # Once the shock return is outside the trailing three-return cooldown
    # window, classification may return to normal without hidden state.
    cleared, *_ = classify_regime(
        [D("100"), D("101"), D("101.01"), D("101.02"), D("101.03")], cfg)
    assert cleared != MarketRegime.SHOCK


def test_adaptive_spacing_expands_with_volatility_and_is_bounded():
    cfg = AdaptivePolicyConfig(min_spacing_bps=D("4"), max_spacing_bps=D("20"))
    quiet = derive_adaptive_plan(
        prices=[D("100"), D("100.01"), D("99.99"), D("100")],
        best_bid=D("99.99"),
        best_ask=D("100.01"),
        max_inventory=D("1"),
        config=cfg,
    )
    volatile = derive_adaptive_plan(
        prices=[D("100"), D("100.2"), D("99.8"), D("100.2")],
        best_bid=D("99.99"),
        best_ask=D("100.01"),
        max_inventory=D("1"),
        config=cfg,
    )
    assert volatile.spacing_bps >= quiet.spacing_bps
    assert quiet.spacing_bps >= cfg.min_spacing_bps
    assert volatile.spacing_bps <= cfg.max_spacing_bps


def test_slight_trend_and_funding_tilt_inventory_target_without_exceeding_cap():
    cfg = AdaptivePolicyConfig()
    up = derive_adaptive_plan(
        prices=[D("100"), D("100.05"), D("100.10"), D("100.16")],
        best_bid=D("100.15"),
        best_ask=D("100.17"),
        max_inventory=D("2"),
        funding_rate=D("0"),
        config=cfg,
    )
    assert up.market_regime == MarketRegime.SLIGHT_UP
    assert up.inventory_target > 0

    costly_long = derive_adaptive_plan(
        prices=[D("100"), D("100.05"), D("100.10"), D("100.16")],
        best_bid=D("100.15"),
        best_ask=D("100.17"),
        max_inventory=D("2"),
        funding_rate=D("0.0001"),
        config=cfg,
    )
    assert costly_long.inventory_target < up.inventory_target
    assert abs(costly_long.inventory_target) <= D("2") * cfg.max_target_fraction


def test_inventory_target_biases_capacity_but_never_breaks_absolute_limit():
    common = dict(
        best_bid=D("100"),
        best_ask=D("101"),
        current_inventory=D("0"),
        grid_spacing=D("1"),
        base_size=D("0.25"),
        level_count=4,
        max_inventory=D("1"),
        tick_size=D("0.1"),
        step_size=D("0.01"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.SLIGHT_UP,
        is_stale_data=False,
    )
    neutral_target = generate_grid_proposals(**common, inventory_target=D("0"))
    long_target = generate_grid_proposals(**common, inventory_target=D("0.25"))

    neutral_sell = sum((p.quantity for p in neutral_target if p.side == "SELL"), D("0"))
    biased_sell = sum((p.quantity for p in long_target if p.side == "SELL"), D("0"))
    biased_buy = sum((p.quantity for p in long_target if p.side == "BUY"), D("0"))

    assert biased_sell < neutral_sell
    assert biased_buy <= D("1")
    assert biased_sell <= D("1")


def test_defensive_regime_ignores_inventory_target_and_only_reduces_actual_position():
    proposals = generate_grid_proposals(
        best_bid=D("100"),
        best_ask=D("101"),
        current_inventory=D("0.5"),
        grid_spacing=D("1"),
        base_size=D("0.25"),
        level_count=4,
        max_inventory=D("1"),
        tick_size=D("0.1"),
        step_size=D("0.01"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.SHOCK,
        is_stale_data=False,
        inventory_target=D("0.25"),
    )
    assert proposals
    assert {p.side for p in proposals} == {"SELL"}
    assert sum((p.quantity for p in proposals), D("0")) <= D("0.5")


def test_invalid_adaptive_config_fails_closed():
    with pytest.raises(ValueError):
        AdaptivePolicyConfig(
            slight_trend_bps=D("50"), strong_trend_bps=D("10")
        ).validate()
    with pytest.raises(ValueError):
        AdaptivePolicyConfig(lookback=5, shock_cooldown_bars=5).validate()
