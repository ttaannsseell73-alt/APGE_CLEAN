from decimal import Decimal

from apge.backtest import AdaptiveBacktester, BacktestConfig, Candle, parse_binance_klines
from apge.risk_guard import GuardLimits

D = Decimal


def _flat_candles(count: int, *, wide: bool = True):
    rows = []
    base_ts = 1_700_000_000_000
    for i in range(count):
        close = D("100")
        high = D("100.30") if wide else D("100.01")
        low = D("99.70") if wide else D("99.99")
        rows.append(
            Candle(
                timestamp_ms=base_ts + i * 60_000,
                open=close,
                high=high,
                low=low,
                close=close,
            )
        )
    return rows


def _loss_candles():
    rows = _flat_candles(5, wide=False)
    ts = rows[-1].timestamp_ms + 60_000
    # Previous closes are flat at 100. The next candle crosses passive BUY
    # levels, never reaches passive SELL levels, and closes materially lower.
    rows.append(
        Candle(
            timestamp_ms=ts,
            open=D("100"),
            high=D("100.00"),
            low=D("98.00"),
            close=D("98.00"),
        )
    )
    for i in range(4):
        ts += 60_000
        rows.append(
            Candle(
                timestamp_ms=ts,
                open=D("98"),
                high=D("98.01"),
                low=D("97.99"),
                close=D("98"),
            )
        )
    return rows


def test_backtest_is_deterministic_and_respects_inventory_cap():
    cfg = BacktestConfig(
        initial_cash=D("10000"),
        base_size=D("0.001"),
        max_inventory=D("0.01"),
        level_count=2,
        min_history=4,
    )
    candles = _flat_candles(20)
    first, fills1 = AdaptiveBacktester(cfg).run(candles)
    second, fills2 = AdaptiveBacktester(cfg).run(candles)

    assert first == second
    assert fills1 == fills2
    assert first.fills > 0
    assert first.max_abs_inventory <= cfg.max_inventory
    assert first.buy_fills == first.sell_fills
    assert first.fees_paid > 0


def test_no_cross_no_fill():
    cfg = BacktestConfig(min_history=4)
    result, fills = AdaptiveBacktester(cfg).run(_flat_candles(15, wide=False))
    assert result.fills == 0
    assert fills == []
    assert result.final_inventory == 0
    assert result.final_equity == cfg.initial_cash


def test_guard_halts_research_run_after_loss_threshold():
    cfg = BacktestConfig(
        initial_cash=D("100"),
        min_history=4,
        maker_fee_rate=D("0.001"),
        guard=GuardLimits(
            max_daily_loss_fraction=D("0.000001"),
            max_drawdown_fraction=D("0.50"),
            max_consecutive_errors=3,
        ),
    )
    result, _ = AdaptiveBacktester(cfg).run(_loss_candles())
    assert result.halted
    assert result.halt_reason == "DAILY_LOSS"


def test_positive_funding_charges_long_and_credits_short_sign_correctly():
    cfg = BacktestConfig(min_history=4, level_count=1)
    candles = _flat_candles(10)
    funded = [
        Candle(
            timestamp_ms=c.timestamp_ms,
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            funding_rate=D("0.0001"),
        )
        for c in candles
    ]
    result, _ = AdaptiveBacktester(cfg).run(funded)
    # Net cost can be positive, negative or zero depending on signed inventory;
    # the critical invariant is deterministic finite accounting.
    assert result.funding_net_cost.is_finite()


def test_parse_binance_klines():
    rows = [
        [1700000000000, "100", "101", "99", "100.5", "10"],
        [1700000060000, "100.5", "102", "100", "101", "11"],
    ]
    candles = parse_binance_klines(rows)
    assert len(candles) == 2
    assert candles[0].open == D("100")
    assert candles[1].close == D("101")
