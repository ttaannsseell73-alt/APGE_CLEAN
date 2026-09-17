from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

from apge.market_regime import Candle


@dataclass(frozen=True)
class TimedCandle:
    open_time_ms: int
    close_time_ms: int
    candle: Candle


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
