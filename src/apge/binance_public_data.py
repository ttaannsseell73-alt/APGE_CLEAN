from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from apge.market_regime import Candle


@dataclass(frozen=True)
class TimedCandle:
    open_time_ms: int
    close_time_ms: int
    candle: Candle


@dataclass(frozen=True)
class AdaptiveMarketSnapshot:
    candles: tuple[Candle, ...]
    funding_rate: Decimal
    last_close_time_ms: int
    premium_time_ms: int
    as_of_ms: int


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("market timestamp must be integer milliseconds")
    if isinstance(value, str) and not value.isascii():
        raise ValueError("invalid market timestamp")
    if isinstance(value, str) and not value.isdecimal():
        raise ValueError("invalid market timestamp")
    result = int(value)
    if result < 0:
        raise ValueError("market timestamp cannot be negative")
    return result


def kline_close_time_ms(open_time_ms: int, interval: str) -> int:
    """Expected close time, including calendar months rather than a 30-day guess."""
    if interval == "1M":
        opened = datetime.fromtimestamp(open_time_ms / 1000, timezone.utc)
        if (opened.day, opened.hour, opened.minute, opened.second, opened.microsecond) != (1, 0, 0, 0, 0):
            raise ValueError("monthly kline must open at the start of a UTC month")
        next_month = opened.replace(year=opened.year + 1, month=1) if opened.month == 12 else opened.replace(month=opened.month + 1)
        return int(next_month.timestamp() * 1000) - 1
    supported = {
        "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
        "8h", "12h", "1d", "3d", "1w",
    }
    if interval not in supported:
        raise ValueError("unsupported kline interval")
    unit_ms = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    return open_time_ms + int(interval[:-1]) * unit_ms[interval[-1]] - 1


def _finite_positive_decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError("invalid decimal field") from exc
    if result.is_nan() or result.is_infinite() or result <= 0:
        raise ValueError("market price must be finite and positive")
    return result


def parse_usdm_klines(rows: Iterable[Sequence[Any]], *, drop_last: bool = True) -> tuple[TimedCandle, ...]:
    """Parse Binance USD-M public kline rows without guessing malformed fields.

    Binance kline rows are expected to contain at least:
      open_time, open, high, low, close, volume, close_time, ...

    The most recent REST kline can still be open; callers should keep the default
    ``drop_last=True`` for deterministic closed-candle validation.
    """
    parsed: list[TimedCandle] = []
    previous_open_time: int | None = None

    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            raise ValueError("invalid Binance kline row")
        try:
            open_time = int(row[0])
            close_time = int(row[6])
        except Exception as exc:
            raise ValueError("invalid kline timestamps") from exc
        if open_time < 0 or close_time <= open_time:
            raise ValueError("invalid kline timestamp range")
        if previous_open_time is not None and open_time <= previous_open_time:
            raise ValueError("klines must be strictly ordered and unique")

        candle = Candle(
            open=_finite_positive_decimal(row[1]),
            high=_finite_positive_decimal(row[2]),
            low=_finite_positive_decimal(row[3]),
            close=_finite_positive_decimal(row[4]),
        )
        if candle.low > candle.high:
            raise ValueError("kline low exceeds high")
        if not (candle.low <= candle.open <= candle.high):
            raise ValueError("kline open outside high/low")
        if not (candle.low <= candle.close <= candle.high):
            raise ValueError("kline close outside high/low")

        parsed.append(TimedCandle(open_time, close_time, candle))
        previous_open_time = open_time

    if drop_last and parsed:
        parsed = parsed[:-1]
    return tuple(parsed)


def candles_only(items: Iterable[TimedCandle]) -> tuple[Candle, ...]:
    return tuple(item.candle for item in items)


def parse_adaptive_market_snapshot(
    rows: Iterable[Sequence[Any]],
    premium: dict,
    *,
    symbol: str,
    interval: str,
    lookback: int,
    as_of_ms: int,
    freshness_grace_ms: int = 5000,
) -> AdaptiveMarketSnapshot:
    """Validate live read-only input before a deterministic adaptive decision.

    An open candle is excluded by its close timestamp. Gaps, future bars, stale
    history and stale/mismatched funding fail closed; no zero-rate fallback or
    repeated last-good-data fallback is allowed.
    """
    as_of_ms = _timestamp_ms(as_of_ms)
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 3:
        raise ValueError("lookback must be an integer >= 3")
    if isinstance(freshness_grace_ms, bool) or not isinstance(freshness_grace_ms, int) or freshness_grace_ms < 0:
        raise ValueError("invalid freshness grace")
    items = list(rows)
    for row in items:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            raise ValueError("invalid Binance kline row")
        opened, closed = _timestamp_ms(row[0]), _timestamp_ms(row[6])
        if opened > as_of_ms:
            raise ValueError("future kline")
        if closed != kline_close_time_ms(opened, interval):
            raise ValueError("kline timestamp range does not match interval")
    parsed = parse_usdm_klines(items, drop_last=False)
    if any(previous.close_time_ms + 1 != current.open_time_ms for previous, current in zip(parsed, parsed[1:])):
        raise ValueError("kline history must be contiguous")
    closed = tuple(item for item in parsed if item.close_time_ms < as_of_ms)
    if len(closed) < lookback:
        raise ValueError("insufficient closed candles")
    window = closed[-lookback:]
    # The next bar may still be forming; allow one interval plus a small delivery
    # grace, with the actual UTC calendar used for monthly intervals.
    freshness_deadline = kline_close_time_ms(window[-1].close_time_ms + 1, interval) + freshness_grace_ms
    if as_of_ms > freshness_deadline:
        raise ValueError("stale closed-candle history")
    if not isinstance(premium, dict) or premium.get("symbol") != symbol:
        raise ValueError("premium index symbol mismatch")
    premium_time = _timestamp_ms(premium.get("time"))
    if premium_time > as_of_ms or as_of_ms - premium_time > freshness_grace_ms:
        raise ValueError("stale or future premium index")
    _finite_positive_decimal(premium.get("markPrice"))
    _finite_positive_decimal(premium.get("indexPrice"))
    try:
        funding = Decimal(str(premium["lastFundingRate"]))
    except Exception as exc:
        raise ValueError("invalid funding rate") from exc
    if not funding.is_finite():
        raise ValueError("funding rate must be finite")
    return AdaptiveMarketSnapshot(
        candles=candles_only(window),
        funding_rate=funding,
        last_close_time_ms=window[-1].close_time_ms,
        premium_time_ms=premium_time,
        as_of_ms=as_of_ms,
    )
