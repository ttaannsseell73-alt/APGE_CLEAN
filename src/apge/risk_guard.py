from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

ZERO = Decimal("0")
ONE = Decimal("1")


class GuardReason(str, Enum):
    OK = "OK"
    DAILY_LOSS = "DAILY_LOSS"
    DRAWDOWN = "DRAWDOWN"
    CONSECUTIVE_ERRORS = "CONSECUTIVE_ERRORS"
    INVALID_EQUITY = "INVALID_EQUITY"
    MANUAL_HALT = "MANUAL_HALT"


@dataclass(frozen=True)
class GuardLimits:
    max_daily_loss_fraction: Decimal = Decimal("0.03")
    max_drawdown_fraction: Decimal = Decimal("0.05")
    max_consecutive_errors: int = 3

    def validate(self) -> None:
        if not (ZERO < self.max_daily_loss_fraction < ONE):
            raise ValueError("max_daily_loss_fraction must be in (0, 1)")
        if not (ZERO < self.max_drawdown_fraction < ONE):
            raise ValueError("max_drawdown_fraction must be in (0, 1)")
        if self.max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be >= 1")


@dataclass(frozen=True)
class GuardDecision:
    allow_new_risk: bool
    halted: bool
    reason: GuardReason
    daily_loss_fraction: Decimal
    drawdown_fraction: Decimal


class PortfolioGuard:
    """Deterministic PnL/error circuit breaker.

    Once halted, the guard stays halted until ``reset`` is called explicitly.
    A date change never silently re-enables risk after a hard stop.
    """

    def __init__(self, limits: GuardLimits | None = None):
        self.limits = limits or GuardLimits()
        self.limits.validate()
        self.day_key: str | None = None
        self.day_start_equity: Decimal | None = None
        self.peak_equity: Decimal | None = None
        self.last_equity: Decimal | None = None
        self.consecutive_errors = 0
        self.halted = False
        self.reason = GuardReason.OK

    @staticmethod
    def _valid_equity(equity: Decimal) -> bool:
        return (
            isinstance(equity, Decimal)
            and not equity.is_nan()
            and not equity.is_infinite()
            and equity > ZERO
        )

    def reset(self, equity: Decimal, day_key: str) -> GuardDecision:
        if not self._valid_equity(equity):
            raise ValueError("equity must be finite and positive")
        if not day_key:
            raise ValueError("day_key is required")
        self.day_key = day_key
        self.day_start_equity = equity
        self.peak_equity = equity
        self.last_equity = equity
        self.consecutive_errors = 0
        self.halted = False
        self.reason = GuardReason.OK
        return self.decision()

    def manual_halt(self) -> GuardDecision:
        self.halted = True
        self.reason = GuardReason.MANUAL_HALT
        return self.decision()

    def record_error(self) -> GuardDecision:
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.limits.max_consecutive_errors:
            self.halted = True
            self.reason = GuardReason.CONSECUTIVE_ERRORS
        return self.decision()

    def record_success(self) -> GuardDecision:
        self.consecutive_errors = 0
        return self.decision()

    def observe_equity(self, equity: Decimal, day_key: str) -> GuardDecision:
        if self.halted:
            return self.decision()
        if not self._valid_equity(equity):
            self.halted = True
            self.reason = GuardReason.INVALID_EQUITY
            return self.decision()
        if not day_key:
            self.halted = True
            self.reason = GuardReason.INVALID_EQUITY
            return self.decision()

        if self.day_key is None:
            self.day_key = day_key
            self.day_start_equity = equity
            self.peak_equity = equity
        elif day_key != self.day_key:
            # A new day resets only the daily-loss anchor, not the global equity peak.
            self.day_key = day_key
            self.day_start_equity = equity

        assert self.day_start_equity is not None
        assert self.peak_equity is not None

        if equity > self.peak_equity:
            self.peak_equity = equity
        self.last_equity = equity

        daily_loss = max(
            ZERO,
            (self.day_start_equity - equity) / self.day_start_equity,
        )
        drawdown = max(ZERO, (self.peak_equity - equity) / self.peak_equity)

        if daily_loss >= self.limits.max_daily_loss_fraction:
            self.halted = True
            self.reason = GuardReason.DAILY_LOSS
        elif drawdown >= self.limits.max_drawdown_fraction:
            self.halted = True
            self.reason = GuardReason.DRAWDOWN

        return GuardDecision(
            allow_new_risk=not self.halted,
            halted=self.halted,
            reason=self.reason,
            daily_loss_fraction=daily_loss,
            drawdown_fraction=drawdown,
        )

    def decision(self) -> GuardDecision:
        daily_loss = ZERO
        drawdown = ZERO
        if self.last_equity is not None and self.day_start_equity is not None:
            daily_loss = max(
                ZERO,
                (self.day_start_equity - self.last_equity) / self.day_start_equity,
            )
        if self.last_equity is not None and self.peak_equity is not None:
            drawdown = max(ZERO, (self.peak_equity - self.last_equity) / self.peak_equity)
        return GuardDecision(
            allow_new_risk=not self.halted,
            halted=self.halted,
            reason=self.reason,
            daily_loss_fraction=daily_loss,
            drawdown_fraction=drawdown,
        )
