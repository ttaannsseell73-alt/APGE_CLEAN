from decimal import Decimal

from apge.backtest import BacktestConfig, Candle
from apge.research_gate import ResearchGateConfig, evaluate_research_gate

D = Decimal


def _range_candles(count: int):
    base_ts = 1_700_000_000_000
    return [
        Candle(
            timestamp_ms=base_ts + i * 60_000,
            open=D("100"),
            high=D("100.30"),
            low=D("99.70"),
            close=D("100"),
        )
        for i in range(count)
    ]


def test_research_gate_passes_reproducible_range_sample_under_fee_stress():
    gate = ResearchGateConfig(
        window_size=60,
        min_windows=4,
        min_total_fills=40,
        min_positive_window_fraction=D("0.75"),
        max_drawdown_fraction=D("0.10"),
        fee_stress_multiplier=D("2"),
    )
    bt = BacktestConfig(min_history=4, level_count=2)
    result = evaluate_research_gate(
        _range_candles(240), backtest_config=bt, gate_config=gate)
    assert result.passed
    assert result.windows == 4
    assert result.positive_windows == 4
    assert result.total_pnl > 0
    assert result.stressed_total_pnl > 0
    assert result.total_fills >= gate.min_total_fills


def test_research_gate_fails_closed_on_insufficient_evidence():
    gate = ResearchGateConfig(window_size=60, min_windows=4, min_total_fills=40)
    bt = BacktestConfig(min_history=4)
    result = evaluate_research_gate(
        _range_candles(120), backtest_config=bt, gate_config=gate)
    assert not result.passed
    assert "INSUFFICIENT_WINDOWS" in result.reasons


def test_research_gate_rejects_unreasonable_fee_stress_config():
    gate = ResearchGateConfig(fee_stress_multiplier=D("0.5"))
    try:
        evaluate_research_gate(_range_candles(500), gate_config=gate)
    except ValueError as exc:
        assert "fee_stress_multiplier" in str(exc)
    else:
        raise AssertionError("invalid fee stress multiplier must fail")
