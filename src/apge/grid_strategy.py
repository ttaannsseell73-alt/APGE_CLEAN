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


def _finite_decimal(value: Decimal) -> bool:
    return isinstance(value, Decimal) and not value.is_nan() and not value.is_infinite()


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
    inventory_skew_strength: Decimal = Decimal("0"),
) -> List[OrderProposal]:
    proposals: List[OrderProposal] = []

    decimal_inputs = (
        best_bid,
        best_ask,
        current_inventory,
        grid_spacing,
        base_size,
        max_inventory,
        tick_size,
        step_size,
        inventory_target,
        inventory_skew_strength,
    )
    if any(not _finite_decimal(value) for value in decimal_inputs):
        return []

    # Invalid BBA (crossed or zero)
    if best_bid >= best_ask or best_bid <= 0 or best_ask <= 0:
        return []

    # Invalid sizes
    if base_size <= 0 or grid_spacing <= 0 or level_count <= 0 or max_inventory <= 0:
        return []
    if tick_size <= 0 or step_size <= 0:
        return []
    if inventory_skew_strength < 0 or abs(inventory_target) > max_inventory:
        return []

    # Round base size down to step size
    rounded_base_size = (base_size // step_size) * step_size
    if rounded_base_size <= 0:
        return []

    # Operational status check
    if system_state in (SystemState.HALTED, SystemState.CONNECTION_LOST):
        return []

    # Check if only risk-reducing orders are allowed.
    risk_reducing_only = False
    if system_state == SystemState.RECONCILING:
        risk_reducing_only = True
    elif is_stale_data:
        risk_reducing_only = True
    elif market_regime in (MarketRegime.STRONG_TREND, MarketRegime.BREAKOUT, MarketRegime.SHOCK):
        risk_reducing_only = True

    buy_order_size = rounded_base_size
    sell_order_size = rounded_base_size

    if risk_reducing_only:
        # Long capability (buying to cover short)
        allowed_long_capacity = max(Decimal("0"), -current_inventory)
        # Short capability (selling to cover long)
        allowed_short_capacity = max(Decimal("0"), current_inventory)
    else:
        # Normal operation always remains bounded by the hard inventory envelope.
        allowed_long_capacity = max(Decimal("0"), max_inventory - current_inventory)
        allowed_short_capacity = max(Decimal("0"), max_inventory + current_inventory)

        # Inventory targeting never increases the configured base order size. It
        # only down-scales the side that would move inventory farther away from
        # the deterministic target. This keeps trend/funding bias subordinate to
        # the hard max_inventory limit.
        if inventory_skew_strength > 0:
            normalized_deviation = (current_inventory - inventory_target) / max_inventory
            if normalized_deviation > 0:
                penalty = min(Decimal("1"), normalized_deviation * inventory_skew_strength)
                buy_order_size = ((rounded_base_size * (Decimal("1") - penalty)) // step_size) * step_size
            elif normalized_deviation < 0:
                penalty = min(Decimal("1"), abs(normalized_deviation) * inventory_skew_strength)
                sell_order_size = ((rounded_base_size * (Decimal("1") - penalty)) // step_size) * step_size

    seen_bids = set()
    seen_asks = set()

    # Generate Bids
    for i in range(1, level_count + 1):
        target_price = best_bid - (grid_spacing * i)
        target_price = (target_price // tick_size) * tick_size

        if target_price >= best_bid:
            target_price = best_bid - tick_size
        if target_price <= 0:
            continue
        if target_price in seen_bids:
            continue

        amount = min(buy_order_size, allowed_long_capacity)
        amount = (amount // step_size) * step_size

        if amount > 0:
            seen_bids.add(target_price)
            proposals.append(OrderProposal("BUY", target_price, amount))
            allowed_long_capacity -= amount
            if allowed_long_capacity <= 0:
                break

    # Generate Asks
    for i in range(1, level_count + 1):
        raw_price = best_ask + (grid_spacing * i)
        target_price = (raw_price // tick_size) * tick_size
        if target_price < raw_price:
            target_price += tick_size
        if target_price <= best_ask:
            target_price = best_ask + tick_size
        if target_price in seen_asks:
            continue

        amount = min(sell_order_size, allowed_short_capacity)
        amount = (amount // step_size) * step_size

        if amount > 0:
            seen_asks.add(target_price)
            proposals.append(OrderProposal("SELL", target_price, amount))
            allowed_short_capacity -= amount
            if allowed_short_capacity <= 0:
                break

    return proposals
