from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Sequence

from apge.adaptive_policy import AdaptivePolicyConfig, BPS, derive_adaptive_plan
from apge.grid_strategy import OrderProposal, generate_grid_proposals
from apge.risk_guard import GuardLimits, PortfolioGuard
from apge.simulator import SystemState

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    funding_rate: Decimal = ZERO

    def validate(self) -> None:
        if self.timestamp_ms < 0:
            raise ValueError("timestamp_ms must be non-negative")
        values = (self.open, self.high, self.low, self.close)
        if any(v <= 0 or v.is_nan() or v.is_infinite() for v in values):
            raise ValueError("OHLC values must be finite and positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is inconsistent with OHLC")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is inconsistent with OHLC")
        if self.funding_rate.is_nan() or self.funding_rate.is_infinite():
            raise ValueError("funding_rate must be finite")


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: Decimal = Decimal("10000")
    maker_fee_rate: Decimal = Decimal("0.0002")
    synthetic_spread_bps: Decimal = Decimal("2")
    base_size: Decimal = Decimal("0.001")
    level_count: int = 3
    max_inventory: Decimal = Decimal("0.01")
    tick_size: Decimal = Decimal("0.1")
    step_size: Decimal = Decimal("0.001")
    min_history: int = 8
    adaptive: AdaptivePolicyConfig = AdaptivePolicyConfig()
    guard: GuardLimits = GuardLimits()

    def validate(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.maker_fee_rate < 0 or self.maker_fee_rate >= ONE:
            raise ValueError("maker_fee_rate must be in [0, 1)")
        if self.synthetic_spread_bps <= 0:
            raise ValueError("synthetic_spread_bps must be positive")
        if self.base_size <= 0 or self.max_inventory <= 0:
            raise ValueError("base_size/max_inventory must be positive")
        if self.level_count < 1 or self.min_history < 2:
            raise ValueError("level_count >= 1 and min_history >= 2 required")
        if self.tick_size <= 0 or self.step_size <= 0:
            raise ValueError("tick_size/step_size must be positive")
        if self.base_size > self.max_inventory:
            raise ValueError("base_size cannot exceed max_inventory")
        self.adaptive.validate()
        self.guard.validate()


@dataclass(frozen=True)
class FillRecord:
    timestamp_ms: int
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    regime: str


@dataclass(frozen=True)
class BacktestResult:
    start_equity: Decimal
    final_equity: Decimal
    pnl: Decimal
    return_fraction: Decimal
    max_drawdown_fraction: Decimal
    fills: int
    buy_fills: int
    sell_fills: int
    fees_paid: Decimal
    funding_net_cost: Decimal
    final_inventory: Decimal
    max_abs_inventory: Decimal
    halted: bool
    halt_reason: str
    observations: int

    def to_dict(self) -> dict:
        raw = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in raw.items()}


class AdaptiveBacktester:
    """Deterministic OHLC research harness for APGE V1.

    Orders are generated from information available through the previous close.
    The next candle decides whether a passive limit price was crossed. This is a
    research approximation, not an exchange fill/queue simulator and not a
    profitability claim.
    """

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()
        self.config.validate()

    @staticmethod
    def _day_key(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()

    def _synthetic_bba(self, mid: Decimal) -> tuple[Decimal, Decimal]:
        half = mid * self.config.synthetic_spread_bps / BPS / Decimal("2")
        return mid - half, mid + half

    @staticmethod
    def _crossed(proposal: OrderProposal, candle: Candle) -> bool:
        if proposal.side == "BUY":
            return candle.low <= proposal.price
        if proposal.side == "SELL":
            return candle.high >= proposal.price
        return False

    @staticmethod
    def _fill_priority(proposal: OrderProposal, inventory: Decimal) -> tuple[int, Decimal]:
        # When both sides cross in the same OHLC bar, process the side that
        # increases absolute inventory first. This is deliberately conservative.
        if inventory > 0:
            adverse = proposal.side == "BUY"
        elif inventory < 0:
            adverse = proposal.side == "SELL"
        else:
            adverse = True
        return (0 if adverse else 1, proposal.price)

    def run(self, candles: Sequence[Candle]) -> tuple[BacktestResult, list[FillRecord]]:
        if len(candles) < self.config.min_history + 1:
            raise ValueError("not enough candles for deterministic backtest")
        for candle in candles:
            candle.validate()

        cash = self.config.initial_cash
        inventory = ZERO
        fees_paid = ZERO
        funding_net_cost = ZERO
        max_abs_inventory = ZERO
        fills: list[FillRecord] = []
        closes: list[Decimal] = []

        guard = PortfolioGuard(self.config.guard)
        first = candles[0]
        guard.reset(self.config.initial_cash, self._day_key(first.timestamp_ms))

        peak_equity = self.config.initial_cash
        max_drawdown = ZERO
        observations = 0

        for idx, candle in enumerate(candles):
            if idx == 0:
                closes.append(candle.close)
                continue

            # Strategy observes only closes available before this candle.
            history = closes[-self.config.adaptive.lookback :]
            if len(history) >= self.config.min_history and not guard.halted:
                reference_mid = closes[-1]
                bid, ask = self._synthetic_bba(reference_mid)
                plan = derive_adaptive_plan(
                    prices=history,
                    best_bid=bid,
                    best_ask=ask,
                    max_inventory=self.config.max_inventory,
                    funding_rate=candle.funding_rate,
                    config=self.config.adaptive,
                )
                adjusted_size = self.config.base_size * plan.size_multiplier
                adjusted_size = (adjusted_size // self.config.step_size) * self.config.step_size

                if adjusted_size > 0:
                    proposals = generate_grid_proposals(
                        best_bid=bid,
                        best_ask=ask,
                        current_inventory=inventory,
                        grid_spacing=plan.grid_spacing,
                        base_size=adjusted_size,
                        level_count=self.config.level_count,
                        max_inventory=self.config.max_inventory,
                        tick_size=self.config.tick_size,
                        step_size=self.config.step_size,
                        system_state=SystemState.OPERATIONAL,
                        market_regime=plan.market_regime,
                        is_stale_data=False,
                        inventory_target=plan.inventory_target,
                    )
                    crossed = [p for p in proposals if self._crossed(p, candle)]
                    crossed.sort(key=lambda p: self._fill_priority(p, inventory))

                    for proposal in crossed:
                        delta = proposal.quantity if proposal.side == "BUY" else -proposal.quantity
                        next_inventory = inventory + delta
                        if abs(next_inventory) > self.config.max_inventory:
                            continue
                        notional = proposal.price * proposal.quantity
                        fee = notional * self.config.maker_fee_rate
                        if proposal.side == "BUY":
                            cash -= notional + fee
                        else:
                            cash += notional - fee
                        inventory = next_inventory
                        fees_paid += fee
                        max_abs_inventory = max(max_abs_inventory, abs(inventory))
                        fills.append(
                            FillRecord(
                                timestamp_ms=candle.timestamp_ms,
                                side=proposal.side,
                                price=proposal.price,
                                quantity=proposal.quantity,
                                fee=fee,
                                regime=plan.market_regime.name,
                            )
                        )

            # Positive funding means long pays short. Signed inventory naturally
            # makes short funding receipts negative cost.
            funding_cost = inventory * candle.close * candle.funding_rate
            cash -= funding_cost
            funding_net_cost += funding_cost

            equity = cash + inventory * candle.close
            observations += 1
            if equity > peak_equity:
                peak_equity = equity
            drawdown = max(ZERO, (peak_equity - equity) / peak_equity)
            max_drawdown = max(max_drawdown, drawdown)
            guard.observe_equity(equity, self._day_key(candle.timestamp_ms))
            closes.append(candle.close)

        final_equity = cash + inventory * candles[-1].close
        pnl = final_equity - self.config.initial_cash
        result = BacktestResult(
            start_equity=self.config.initial_cash,
            final_equity=final_equity,
            pnl=pnl,
            return_fraction=pnl / self.config.initial_cash,
            max_drawdown_fraction=max_drawdown,
            fills=len(fills),
            buy_fills=sum(1 for f in fills if f.side == "BUY"),
            sell_fills=sum(1 for f in fills if f.side == "SELL"),
            fees_paid=fees_paid,
            funding_net_cost=funding_net_cost,
            final_inventory=inventory,
            max_abs_inventory=max_abs_inventory,
            halted=guard.halted,
            halt_reason=guard.reason.value,
            observations=observations,
        )
        return result, fills


def parse_binance_klines(rows: Iterable[Sequence[object]]) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows:
        if len(row) < 5:
            raise ValueError("Binance kline row must contain at least 5 fields")
        candles.append(
            Candle(
                timestamp_ms=int(row[0]),
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
            )
        )
    return candles
