from decimal import Decimal
from dataclasses import dataclass
from enum import Enum, auto
from typing import List

from apge.simulator import SystemState


class MarketRegime(Enum):
    NEUTRAL = auto()
    SLIGHT_UP = auto()
    SLIGHT_DOWN = auto()
    STRONG_TREND = auto()
    BREAKOUT = auto()
    SHOCK = auto()


@dataclass(frozen=True)
class OrderProposal:
    side: str
    price: Decimal
    quantity: Decimal
    reduce_only: bool = False


def generate_grid_proposals(
    best_bid: Decimal,
    best_ask: Decimal,
    current_inventory: Decimal,
    grid_spacing: Decimal,
    base_size: Decimal,
    level_count: int,
    max_inventory: Decimal,
    tick_size: Decimal,
    step_size: Decimal,
    system_state: SystemState,
    market_regime: MarketRegime,
    is_stale_data: bool,
    inventory_target: Decimal = Decimal("0"),
) -> List[OrderProposal]:
    """Generate a deterministic bounded grid.

    ``inventory_target`` tilts normal-operation capacity around a target inventory
    without changing the hard absolute ``max_inventory`` bound. Defensive states
    ignore the target and only emit exchange-level reduce-only proposals that can
    move actual inventory toward zero.
    """

    proposals: List[OrderProposal] = []

    if best_bid >= best_ask or best_bid <= 0 or best_ask <= 0:
        return []
    if base_size <= 0 or grid_spacing <= 0 or level_count <= 0 or max_inventory <= 0:
        return []
    if tick_size <= 0 or step_size <= 0:
        return []
    if inventory_target.is_nan() or inventory_target.is_infinite():
        return []
    if abs(inventory_target) >= max_inventory:
        return []

    rounded_base_size = (base_size // step_size) * step_size
    if rounded_base_size <= 0:
        return []

    if system_state in (SystemState.HALTED, SystemState.CONNECTION_LOST):
        return []

    risk_reducing_only = False
    if system_state == SystemState.RECONCILING:
        risk_reducing_only = True
    elif is_stale_data:
        risk_reducing_only = True
    elif market_regime in (MarketRegime.STRONG_TREND, MarketRegime.BREAKOUT, MarketRegime.SHOCK):
        risk_reducing_only = True

    if risk_reducing_only:
        allowed_long_capacity = max(Decimal("0"), -current_inventory)
        allowed_short_capacity = max(Decimal("0"), current_inventory)
    else:
        allowed_long_capacity = max(Decimal("0"), max_inventory - current_inventory)
        allowed_short_capacity = max(Decimal("0"), max_inventory + current_inventory)

        if inventory_target > 0:
            allowed_short_capacity = max(
                Decimal("0"), allowed_short_capacity - inventory_target)
        elif inventory_target < 0:
            allowed_long_capacity = max(
                Decimal("0"), allowed_long_capacity + inventory_target)

        effective_inventory = current_inventory - inventory_target
        effective_limit = max_inventory - abs(inventory_target)
        allowed_long_capacity = min(
            allowed_long_capacity,
            max(Decimal("0"), effective_limit - effective_inventory),
        )
        allowed_short_capacity = min(
            allowed_short_capacity,
            max(Decimal("0"), effective_limit + effective_inventory),
        )

    seen_bids = set()
    seen_asks = set()

    for i in range(1, level_count + 1):
        target_price = best_bid - (grid_spacing * i)
        target_price = (target_price // tick_size) * tick_size
        if target_price >= best_bid:
            target_price = best_bid - tick_size
        if target_price <= 0 or target_price in seen_bids:
            continue

        amount = min(rounded_base_size, allowed_long_capacity)
        amount = (amount // step_size) * step_size
        if amount > 0:
            seen_bids.add(target_price)
            proposals.append(
                OrderProposal("BUY", target_price, amount, reduce_only=risk_reducing_only))
            allowed_long_capacity -= amount
            if allowed_long_capacity <= 0:
                break

    for i in range(1, level_count + 1):
        target_price = best_ask + (grid_spacing * i)
        target_price = (target_price // tick_size) * tick_size
        if target_price < (best_ask + (grid_spacing * i)):
            target_price += tick_size
        if target_price <= best_ask:
            target_price = best_ask + tick_size
        if target_price in seen_asks:
            continue

        amount = min(rounded_base_size, allowed_short_capacity)
        amount = (amount // step_size) * step_size
        if amount > 0:
            seen_asks.add(target_price)
            proposals.append(
                OrderProposal("SELL", target_price, amount, reduce_only=risk_reducing_only))
            allowed_short_capacity -= amount
            if allowed_short_capacity <= 0:
                break

    return proposals
