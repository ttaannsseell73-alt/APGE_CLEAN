from decimal import Decimal

from apge.risk_guard import GuardLimits, GuardReason, PortfolioGuard

D = Decimal


def test_daily_loss_halts_and_does_not_auto_resume_next_day():
    guard = PortfolioGuard(GuardLimits(max_daily_loss_fraction=D("0.03"), max_drawdown_fraction=D("0.20")))
    guard.reset(D("100"), "2026-01-01")
    decision = guard.observe_equity(D("96.9"), "2026-01-01")
    assert decision.halted
    assert decision.reason == GuardReason.DAILY_LOSS

    next_day = guard.observe_equity(D("110"), "2026-01-02")
    assert next_day.halted
    assert next_day.reason == GuardReason.DAILY_LOSS


def test_drawdown_halts_from_global_peak():
    guard = PortfolioGuard(GuardLimits(max_daily_loss_fraction=D("0.50"), max_drawdown_fraction=D("0.05")))
    guard.reset(D("100"), "2026-01-01")
    guard.observe_equity(D("110"), "2026-01-01")
    decision = guard.observe_equity(D("104"), "2026-01-01")
    assert decision.halted
    assert decision.reason == GuardReason.DRAWDOWN


def test_consecutive_error_breaker_and_success_reset():
    guard = PortfolioGuard(GuardLimits(max_consecutive_errors=3))
    guard.reset(D("100"), "2026-01-01")
    guard.record_error()
    guard.record_error()
    guard.record_success()
    assert guard.consecutive_errors == 0
    guard.record_error()
    guard.record_error()
    decision = guard.record_error()
    assert decision.halted
    assert decision.reason == GuardReason.CONSECUTIVE_ERRORS


def test_manual_halt_requires_explicit_reset():
    guard = PortfolioGuard()
    guard.reset(D("100"), "2026-01-01")
    assert guard.manual_halt().halted
    assert guard.observe_equity(D("120"), "2026-01-02").halted
    reset = guard.reset(D("120"), "2026-01-02")
    assert not reset.halted
    assert reset.reason == GuardReason.OK


def test_invalid_equity_fails_closed():
    guard = PortfolioGuard()
    guard.reset(D("100"), "2026-01-01")
    decision = guard.observe_equity(D("NaN"), "2026-01-01")
    assert decision.halted
    assert decision.reason == GuardReason.INVALID_EQUITY
