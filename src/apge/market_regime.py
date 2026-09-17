from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

from apge.grid_strategy import MarketRegime


@dataclass(frozen=True)
class Candle:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class RegimeConfig:
    lookback: int = 20
    slight_trend_return: Decimal = Decimal("0.002")
    strong_trend_return: Decimal = Decimal("0.008")
    slight_persistence: Decimal = Decimal("0.60")
    strong_persistence: Decimal = Decimal("0.75")
    breakout_buffer: Decimal = Decimal("0.0015")
    shock_multiplier: Decimal = Decimal("4")
    shock_min_return: Decimal = Decimal("0.01")


@dataclass(frozen=True)
class RegimeAssessment:
    regime: MarketRegime
    cumulative_return: Decimal
    average_abs_return: Decimal
    latest_abs_return: Decimal
    directional_persistence: Decimal


def _validate_candle(candle: Candle) -> None:
    values = (candle.open, candle.high, candle.low, candle.close)
    if any((not isinstance(v, Decimal) or v.is_nan() or v.is_infinite() or v <= 0) for v in values):
        raise ValueError("candle prices must be finite positive Decimals")
    if candle.low > candle.high:
        raise ValueError("candle low cannot exceed high")
    if candle.open < candle.low or candle.open > candle.high:
        raise ValueError("candle open must be within high/low")
    if candle.close < candle.low or candle.close > candle.high:
        raise ValueError("candle close must be within high/low")


def _fractional_returns(candles: Sequence[Candle]) -> list[Decimal]:
    returns: list[Decimal] = []
    for previous, current in zip(candles, candles[1:]):
        returns.append((current.close - previous.close) / previous.close)
    return returns


def assess_market_regime(
    candles: Iterable[Candle],
    config: RegimeConfig = RegimeConfig(),
) -> RegimeAssessment:
    """Classify one deterministic V1 market regime from closed candles only.

    Data-health uncertainty is deliberately not mapped to a market regime. Invalid
    or insufficient input raises ValueError so the caller can fail closed through
    the separate connection/data-health state machine.
    """
    items = list(candles)
    if config.lookback < 3:
        raise ValueError("lookback must be at least 3")
    if len(items) < config.lookback:
        raise ValueError("insufficient closed candles for regime assessment")

    window = items[-config.lookback:]
    for candle in window:
        _validate_candle(candle)

    returns = _fractional_returns(window)
    if not returns:
        raise ValueError("insufficient returns")

    cumulative_return = (window[-1].close - window[0].close) / window[0].close
    average_abs_return = sum((abs(r) for r in returns), Decimal("0")) / Decimal(len(returns))
    latest_abs_return = abs(returns[-1])

    direction = Decimal("1") if cumulative_return > 0 else Decimal("-1") if cumulative_return < 0 else Decimal("0")
    if direction == 0:
        directional_persistence = Decimal("0")
    else:
        aligned = sum(1 for r in returns if (r > 0 and direction > 0) or (r < 0 and direction < 0))
        directional_persistence = Decimal(aligned) / Decimal(len(returns))

    prior = window[:-1]
    prior_high = max(c.high for c in prior)
    prior_low = min(c.low for c in prior)
    last_close = window[-1].close

    shock_threshold = max(
        config.shock_min_return,
        average_abs_return * config.shock_multiplier,
    )
    if latest_abs_return >= shock_threshold:
        regime = MarketRegime.SHOCK
    elif last_close > prior_high * (Decimal("1") + config.breakout_buffer):
        regime = MarketRegime.BREAKOUT
    elif last_close < prior_low * (Decimal("1") - config.breakout_buffer):
        regime = MarketRegime.BREAKOUT
    elif abs(cumulative_return) >= config.strong_trend_return and directional_persistence >= config.strong_persistence:
        regime = MarketRegime.STRONG_TREND
    elif cumulative_return >= config.slight_trend_return and directional_persistence >= config.slight_persistence:
        regime = MarketRegime.SLIGHT_UP
    elif cumulative_return <= -config.slight_trend_return and directional_persistence >= config.slight_persistence:
        regime = MarketRegime.SLIGHT_DOWN
    else:
        regime = MarketRegime.NEUTRAL

    return RegimeAssessment(
        regime=regime,
        cumulative_return=cumulative_return,
        average_abs_return=average_abs_return,
        latest_abs_return=latest_abs_return,
        directional_persistence=directional_persistence,
    )
