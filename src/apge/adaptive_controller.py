from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from apge.adaptive_policy import AdaptiveGridDecision, AdaptivePolicyConfig, build_adaptive_grid_decision
from apge.grid_lifecycle import GridLifecycleResult, apply_grid_diff
from apge.grid_strategy import OrderProposal, generate_grid_proposals
from apge.market_regime import Candle, RegimeAssessment, RegimeConfig, assess_market_regime


@dataclass(frozen=True)
class AdaptiveCycleResult:
    assessment: RegimeAssessment | None
    decision: AdaptiveGridDecision | None
    lifecycle: GridLifecycleResult
    proposal_count: int


def _filter_exchange_limits(runtime, proposals: Iterable[OrderProposal]) -> list[OrderProposal]:
    required = (runtime.tick_size, runtime.step_size)
    if any(value is None for value in required):
        raise ValueError("exchange tick/step filters are not loaded")
    if "minQty" not in runtime.filters or "minNotional" not in runtime.filters:
        raise ValueError("exchange quantity/notional filters are not loaded")

    minimum_qty = Decimal(str(runtime.filters["minQty"]))
    minimum_notional = Decimal(str(runtime.filters["minNotional"]))
    maximum_qty = runtime.filters.get("maxQty")
    maximum_qty = Decimal(str(maximum_qty)) if maximum_qty is not None else None

    valid = []
    for proposal in proposals:
        if proposal.quantity < minimum_qty:
            continue
        if maximum_qty is not None and proposal.quantity > maximum_qty:
            continue
        if proposal.price * proposal.quantity < minimum_notional:
            continue
        valid.append(proposal)
    return valid


def run_adaptive_grid_cycle(
    *,
    runtime,
    execution_engine,
    candles: Iterable[Candle],
    funding_rate: Decimal,
    nominal_base_size: Decimal,
    level_count: int,
    max_inventory: Decimal,
    regime_config: RegimeConfig = RegimeConfig(),
    policy_config: AdaptivePolicyConfig = AdaptivePolicyConfig(),
) -> AdaptiveCycleResult:
    """Run one deterministic adaptive cycle without bypassing RiskEngine.

    The controller deliberately separates market regime classification from data
    health. Invalid/insufficient candles or missing exchange filters fail closed
    into RECONCILING and no new order is submitted.
    """
    risk = execution_engine.risk_engine
    try:
        if runtime.best_bid is None or runtime.best_ask is None:
            raise ValueError("best bid/ask unavailable")
        if runtime.tick_size is None or runtime.step_size is None:
            raise ValueError("exchange filters unavailable")

        assessment = assess_market_regime(candles, regime_config)
        decision = build_adaptive_grid_decision(
            best_bid=Decimal(str(runtime.best_bid)),
            best_ask=Decimal(str(runtime.best_ask)),
            current_inventory=Decimal(str(runtime.current_inventory)),
            max_inventory=max_inventory,
            nominal_base_size=nominal_base_size,
            step_size=Decimal(str(runtime.step_size)),
            assessment=assessment,
            funding_rate=funding_rate,
            config=policy_config,
        )

        proposals = generate_grid_proposals(
            best_bid=Decimal(str(runtime.best_bid)),
            best_ask=Decimal(str(runtime.best_ask)),
            current_inventory=Decimal(str(runtime.current_inventory)),
            grid_spacing=decision.grid_spacing,
            base_size=decision.base_size,
            level_count=level_count,
            max_inventory=max_inventory,
            tick_size=Decimal(str(runtime.tick_size)),
            step_size=Decimal(str(runtime.step_size)),
            system_state=risk.system_state,
            market_regime=decision.regime,
            is_stale_data=runtime.is_stale_data(),
            inventory_target=decision.target_inventory,
            inventory_skew_strength=decision.inventory_skew_strength,
        )
        proposals = _filter_exchange_limits(runtime, proposals)
        lifecycle = apply_grid_diff(runtime.symbol, proposals, execution_engine)
        return AdaptiveCycleResult(assessment, decision, lifecycle, len(proposals))
    except Exception as exc:
        risk.restore_connection()
        return AdaptiveCycleResult(
            assessment=None,
            decision=None,
            lifecycle=GridLifecycleResult((), (), f"adaptive cycle fail-closed: {exc}"),
            proposal_count=0,
        )
