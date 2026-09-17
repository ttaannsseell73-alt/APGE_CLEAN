from dataclasses import replace
from decimal import Decimal

from apge.backtest import BacktestConfig, run_backtest
from apge.market_regime import Candle


D = Decimal


def _range_candles(count=80):
    bars = []
    price = D("100")
    for index in range(count):
        change = D("0.0004") if index % 2 == 0 else D("-0.0004")
        close = price * (D("1") + change)
        high = max(price, close) * D("1.02")
        low = min(price, close) * D("0.98")
        bars.append(Candle(price, high, low, close))
        price = close
    return bars


def _strong_uptrend(count=60):
    bars = []
    price = D("100")
    for _ in range(count):
        close = price * D("1.001")
        high = close * D("1.001")
        low = price * D("0.999")
        bars.append(Candle(price, high, low, close))
        price = close
    return bars


def _config(**kwargs):
    base = BacktestConfig(
        initial_equity=D("10000"),
        nominal_base_size=D("0.001"),
        level_count=1,
        max_inventory=D("0.005"),
        tick_size=D("0.1"),
        step_size=D("0.001"),
        min_qty=D("0.001"),
        min_notional=D("0.01"),
        synthetic_spread_bps=D("2"),
        fee_rate=D("0.0002"),
        conservative_dual_touch=True,
    )
    return replace(base, **kwargs)


def test_backtest_is_deterministic_and_respects_hard_inventory_limit():
    candles = _range_candles()
    first = run_backtest(candles, config=_config())
    second = run_backtest(candles, config=_config())

    assert first == second
    assert first.bars_processed == 60
    assert first.fill_count > 0
    assert first.max_abs_inventory <= D("0.005")
    assert all(abs(point.position) <= D("0.005") for point in first.equity_curve)


def test_conservative_dual_touch_does_not_grant_two_sided_same_bar_round_trip():
    result = run_backtest(_range_candles(50), config=_config(level_count=1))
    fills_by_bar = {}
    for fill in result.fills:
        fills_by_bar.setdefault(fill.bar_index, []).append(fill)
    assert fills_by_bar
    assert all(len(items) <= 1 for items in fills_by_bar.values())


def test_fees_never_improve_equity_for_identical_fill_path():
    candles = _range_candles()
    no_fee = run_backtest(candles, config=_config(fee_rate=D("0")))
    with_fee = run_backtest(candles, config=_config(fee_rate=D("0.001")))

    assert with_fee.fill_count == no_fee.fill_count
    assert with_fee.turnover == no_fee.turnover
    assert with_fee.final_equity <= no_fee.final_equity
    assert with_fee.total_fees == with_fee.turnover * D("0.001")


def test_strong_trend_from_flat_does_not_open_new_risk():
    result = run_backtest(_strong_uptrend(), config=_config())
    assert result.fill_count == 0
    assert result.max_abs_inventory == 0
    assert result.final_equity == result.initial_equity


def test_funding_is_applied_with_linear_perpetual_sign_convention():
    candles = _range_candles()
    rates = [D("0.0001")] * len(candles)
    result = run_backtest(candles, funding_rates=rates, config=_config(fee_rate=D("0")))

    expected = sum(
        (point.position * point.close * D("0.0001") for point in result.equity_curve),
        D("0"),
    )
    assert result.total_funding == expected


def test_fill_confirmation_never_increases_fill_count_on_same_bars():
    candles = _range_candles()
    touch = run_backtest(candles, config=_config(fill_confirmation_bps=D("0")))
    confirmed = run_backtest(candles, config=_config(fill_confirmation_bps=D("25")))
    assert confirmed.fill_count <= touch.fill_count


def test_invalid_fill_confirmation_fails_closed():
    candles = _range_candles()
    for value in (D("-1"), D("10000"), D("Infinity")):
        try:
            run_backtest(candles, config=_config(fill_confirmation_bps=value))
        except ValueError:
            pass
        else:
            raise AssertionError("invalid fill confirmation must fail closed")


def test_backtest_rejects_malformed_inputs():
    candles = _range_candles(25)
    bad = list(candles)
    bad[-1] = Candle(D("100"), D("99"), D("101"), D("100"))

    try:
        run_backtest(bad, config=_config())
    except ValueError:
        pass
    else:
        raise AssertionError("malformed OHLC must fail closed")

    try:
        run_backtest(candles, funding_rates=[D("0")], config=_config())
    except ValueError:
        pass
    else:
        raise AssertionError("funding length mismatch must fail closed")
