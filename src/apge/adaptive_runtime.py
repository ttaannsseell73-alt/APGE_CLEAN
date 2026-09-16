from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from apge.adaptive_policy import AdaptivePlan, AdaptivePolicyConfig, derive_adaptive_plan
from apge.grid_strategy import OrderProposal, generate_grid_proposals
from apge.simulator import SystemState

ZERO = Decimal("0")


@dataclass(frozen=True)
class ExchangeConstraints:
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_qty: Decimal

    def validate(self) -> None:
        if self.tick_size <= 0 or self.step_size <= 0:
            raise ValueError("tick_size and step_size must be positive")
        if self.min_qty <= 0 or self.min_notional <= 0 or self.max_qty <= 0:
            raise ValueError("exchange quantity/notional limits must be positive")
        if self.max_qty < self.min_qty:
            raise ValueError("max_qty must be >= min_qty")


@dataclass(frozen=True)
class AdaptiveCycleConfig:
    base_size: Decimal
    level_count: int
    max_inventory: Decimal
    min_history: int = 8
    policy: AdaptivePolicyConfig = AdaptivePolicyConfig()

    def validate(self) -> None:
        if self.base_size <= 0:
            raise ValueError("base_size must be positive")
        if self.level_count < 1:
            raise ValueError("level_count must be >= 1")
        if self.max_inventory <= 0:
            raise ValueError("max_inventory must be positive")
        if self.base_size > self.max_inventory:
            raise ValueError("base_size cannot exceed max_inventory")
        if self.min_history < 2:
            raise ValueError("min_history must be >= 2")
        self.policy.validate()


@dataclass(frozen=True)
class AdaptiveCycleResult:
    plan: AdaptivePlan | None
    desired_orders: int
    submitted_orders: int
    canceled_orders: int
    active_order_ids: tuple[str, ...]
    blocked_reason: str | None


class AdaptiveGridExecutor:
    """Adaptive proposal -> RiskEngine/ExecutionEngine bridge.

    This layer intentionally reuses the accepted execution/persistence/risk
    contracts. It only decides the deterministic desired grid and performs a
    minimal desired-vs-live diff. RiskEngine remains the final authority.
    """

    def __init__(
        self,
        *,
        symbol: str,
        constraints: ExchangeConstraints,
        config: AdaptiveCycleConfig,
    ):
        if not symbol:
            raise ValueError("symbol is required")
        constraints.validate()
        config.validate()
        self.symbol = symbol
        self.constraints = constraints
        self.config = config

    def _filter(self, proposals: Sequence[OrderProposal]) -> list[OrderProposal]:
        valid: list[OrderProposal] = []
        for proposal in proposals:
            if proposal.quantity < self.constraints.min_qty:
                continue
            if proposal.quantity > self.constraints.max_qty:
                continue
            if proposal.price * proposal.quantity < self.constraints.min_notional:
                continue
            valid.append(proposal)
        return valid

    def run_cycle(
        self,
        *,
        price_history: Sequence[Decimal],
        best_bid: Decimal,
        best_ask: Decimal,
        current_inventory: Decimal,
        funding_rate: Decimal,
        is_stale_data: bool,
        execution_engine,
    ) -> AdaptiveCycleResult:
        if len(price_history) < self.config.min_history:
            return AdaptiveCycleResult(None, 0, 0, 0, tuple(), "INSUFFICIENT_HISTORY")

        risk_state = execution_engine.risk_engine.system_state
        if risk_state in (SystemState.HALTED, SystemState.CONNECTION_LOST):
            return AdaptiveCycleResult(None, 0, 0, 0, tuple(), risk_state.name)

        plan = derive_adaptive_plan(
            prices=price_history,
            best_bid=best_bid,
            best_ask=best_ask,
            max_inventory=self.config.max_inventory,
            funding_rate=funding_rate,
            config=self.config.policy,
        )
        adjusted_size = self.config.base_size * plan.size_multiplier
        adjusted_size = (adjusted_size // self.constraints.step_size) * self.constraints.step_size

        proposals: list[OrderProposal] = []
        if adjusted_size > ZERO:
            proposals = generate_grid_proposals(
                best_bid=best_bid,
                best_ask=best_ask,
                current_inventory=current_inventory,
                grid_spacing=plan.grid_spacing,
                base_size=adjusted_size,
                level_count=self.config.level_count,
                max_inventory=self.config.max_inventory,
                tick_size=self.constraints.tick_size,
                step_size=self.constraints.step_size,
                system_state=risk_state,
                market_regime=plan.market_regime,
                is_stale_data=is_stale_data,
                inventory_target=plan.inventory_target,
            )
        proposals = self._filter(proposals)

        active = [
            row
            for row in execution_engine.persistence.get_active_intents()
            if row["symbol"] == self.symbol
        ]
        current_keys: dict[tuple[str, Decimal, Decimal], list[str]] = {}
        for row in active:
            key = (row["side"], Decimal(row["price"]), Decimal(row["quantity"]))
            current_keys.setdefault(key, []).append(row["client_order_id"])

        desired = {(p.side, p.price, p.quantity): p for p in proposals}
        current_set = set(current_keys)
        desired_set = set(desired)

        canceled = 0
        for key in sorted(current_set - desired_set, key=lambda x: (x[0], x[1], x[2])):
            for cid in sorted(current_keys[key]):
                if execution_engine.cancel_order(self.symbol, cid):
                    canceled += 1

        if execution_engine.risk_engine.system_state != SystemState.OPERATIONAL:
            remaining = tuple(
                sorted(
                    row["client_order_id"]
                    for row in execution_engine.persistence.get_active_intents()
                    if row["symbol"] == self.symbol
                )
            )
            return AdaptiveCycleResult(
                plan,
                len(proposals),
                0,
                canceled,
                remaining,
                execution_engine.risk_engine.system_state.name,
            )

        submitted = 0
        for key in sorted(desired_set - current_set, key=lambda x: (x[0], x[1], x[2])):
            if execution_engine.execute_proposal(self.symbol, desired[key]):
                submitted += 1

        final_active = tuple(
            sorted(
                row["client_order_id"]
                for row in execution_engine.persistence.get_active_intents()
                if row["symbol"] == self.symbol
            )
        )
        return AdaptiveCycleResult(
            plan,
            len(proposals),
            submitted,
            canceled,
            final_active,
            None,
        )
