from decimal import Decimal

import pytest

from apge.adaptive_policy import AdaptivePolicyConfig, build_adaptive_grid_decision
from apge.grid_strategy import MarketRegime, generate_grid_proposals
from apge.market_regime import Candle, RegimeAssessment, RegimeConfig, assess_market_regime
from apge.simulator import SystemState


D = Decimal


def _candle(open_price: Decimal, close_price: Decimal, pad: Decimal = D("0.002")) -> Candle:
    high = max(open_price, close_price) * (D("1") + pad)
    low = min(open_price, close_price) * (D("1") - pad)
    return Candle(open=open_price, high=high, low=low, close=close_price)


def _trend(start: str, per_step: str, count: int = 20, pad: str = "0.002"):
    price = D(start)
    candles = []
    step = D(per_step)
    for _ in range(count):
        close = price * (D("1") + step)
        candles.append(_candle(price, close, D(pad)))
        price = close
    return candles


def test_regime_neutral_slight_and_strong_trend():
    neutral = []
    price = D("100")
    for i in range(20):
        step = D("0.0002") if i % 2 == 0 else D("-0.0002")
        close = price * (D("1") + step)
        neutral.append(_candle(price, close))
        price = close
    assert assess_market_regime(neutral).regime == MarketRegime.NEUTRAL

    slight_up = _trend("100", "0.0003")
    slight = assess_market_regime(slight_up)
    assert slight.regime == MarketRegime.SLIGHT_UP
    assert slight.directional_persistence == D("1")

    strong_up = _trend("100", "0.001")
    assert assess_market_regime(strong_up).regime == MarketRegime.STRONG_TREND

    strong_down = _trend("100", "-0.001")
    assert assess_market_regime(strong_down).regime == MarketRegime.STRONG_TREND


def test_regime_breakout_and_shock_are_distinct():
    flat = [_candle(D("100"), D("100"), D("0.001")) for _ in range(19)]
    breakout = flat + [Candle(D("100"), D("100.55"), D("99.95"), D("100.5"))]
    assert assess_market_regime(breakout).regime == MarketRegime.BREAKOUT

    shock = flat + [Candle(D("100"), D("105.5"), D("99.9"), D("105"))]
    assert assess_market_regime(shock).regime == MarketRegime.SHOCK


def test_regime_invalid_data_does_not_masquerade_as_market_state():
    with pytest.raises(ValueError, match="insufficient"):
        assess_market_regime([_candle(D("100"), D("100"))] * 5)

    bad = [_candle(D("100"), D("100")) for _ in range(20)]
    bad[-1] = Candle(D("100"), D("99"), D("101"), D("100"))
    with pytest.raises(ValueError):
        assess_market_regime(bad)


def _assessment(regime: MarketRegime, avg_abs: str = "0.001") -> RegimeAssessment:
    return RegimeAssessment(
        regime=regime,
        cumulative_return=D("0"),
        average_abs_return=D(avg_abs),
        latest_abs_return=D(avg_abs),
        directional_persistence=D("0.5"),
    )


def test_adaptive_policy_trend_and_funding_bias_are_bounded():
    up = build_adaptive_grid_decision(
        best_bid=D("99990"),
        best_ask=D("100010"),
        current_inventory=D("0"),
        max_inventory=D("0.01"),
        nominal_base_size=D("0.001"),
        step_size=D("0.001"),
        assessment=_assessment(MarketRegime.SLIGHT_UP),
        funding_rate=D("0"),
    )
    assert up.target_inventory > 0
    assert up.target_inventory <= D("0.003")
    assert up.risk_increasing_allowed is True

    funded = build_adaptive_grid_decision(
        best_bid=D("99990"),
        best_ask=D("100010"),
        current_inventory=D("0"),
        max_inventory=D("0.01"),
        nominal_base_size=D("0.001"),
        step_size=D("0.001"),
        assessment=_assessment(MarketRegime.NEUTRAL),
        funding_rate=D("0.0001"),
    )
    assert funded.target_inventory < 0
    assert abs(funded.target_inventory) <= D("0.0008")


def test_adaptive_policy_strong_breakout_shock_cannot_enable_new_risk():
    for regime in (MarketRegime.STRONG_TREND, MarketRegime.BREAKOUT, MarketRegime.SHOCK):
        decision = build_adaptive_grid_decision(
            best_bid=D("99990"),
            best_ask=D("100010"),
            current_inventory=D("0.002"),
            max_inventory=D("0.01"),
            nominal_base_size=D("0.001"),
            step_size=D("0.001"),
            assessment=_assessment(regime),
            funding_rate=D("-0.0001"),
        )
        assert decision.risk_increasing_allowed is False
        assert decision.target_inventory == 0


def test_adaptive_policy_volatility_widens_spacing_and_never_upsizes_base():
    low = build_adaptive_grid_decision(
        best_bid=D("99990"), best_ask=D("100010"), current_inventory=D("0"),
        max_inventory=D("0.01"), nominal_base_size=D("0.01"), step_size=D("0.001"),
        assessment=_assessment(MarketRegime.NEUTRAL, "0.0002"),
    )
    high = build_adaptive_grid_decision(
        best_bid=D("99990"), best_ask=D("100010"), current_inventory=D("0"),
        max_inventory=D("0.01"), nominal_base_size=D("0.01"), step_size=D("0.001"),
        assessment=_assessment(MarketRegime.NEUTRAL, "0.004"),
    )
    assert high.grid_spacing > low.grid_spacing
    assert high.base_size <= low.base_size <= D("0.01")


def test_inventory_target_skews_only_the_side_moving_away_from_target():
    proposals = generate_grid_proposals(
        best_bid=D("100"),
        best_ask=D("101"),
        current_inventory=D("0"),
        grid_spacing=D("1"),
        base_size=D("1"),
        level_count=1,
        max_inventory=D("4"),
        tick_size=D("0.1"),
        step_size=D("0.1"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.SLIGHT_UP,
        is_stale_data=False,
        inventory_target=D("1"),
        inventory_skew_strength=D("1"),
    )
    by_side = {p.side: p for p in proposals}
    assert by_side["BUY"].quantity == D("1")
    assert by_side["SELL"].quantity == D("0.7")


def test_risk_reducing_regime_ignores_target_and_never_adds_exposure():
    proposals = generate_grid_proposals(
        best_bid=D("100"),
        best_ask=D("101"),
        current_inventory=D("2"),
        grid_spacing=D("1"),
        base_size=D("1"),
        level_count=2,
        max_inventory=D("4"),
        tick_size=D("0.1"),
        step_size=D("0.1"),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.SHOCK,
        is_stale_data=False,
        inventory_target=D("1"),
        inventory_skew_strength=D("1"),
    )
    assert proposals
    assert all(p.side == "SELL" for p in proposals)
    assert sum((p.quantity for p in proposals), D("0")) <= D("2")
