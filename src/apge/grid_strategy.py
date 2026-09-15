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
    is_stale_data: bool
) -> List[OrderProposal]:
    proposals = []

    # Invalid BBA (crossed or zero)
    if best_bid >= best_ask or best_bid <= 0 or best_ask <= 0:
        return []

    # Invalid sizes
    if base_size <= 0 or grid_spacing <= 0 or level_count <= 0:
        return []

    if tick_size <= 0 or step_size <= 0:
        return []

    # Round base size down to step size
    rounded_base_size = (base_size // step_size) * step_size
    if rounded_base_size <= 0:
        return []

    # Operational status check
    if system_state in (SystemState.HALTED, SystemState.CONNECTION_LOST):
        return []

    # Check if only risk-reducing orders are allowed
    risk_reducing_only = False
    if system_state == SystemState.RECONCILING:
        risk_reducing_only = True
    elif is_stale_data:
        risk_reducing_only = True
    elif market_regime in (MarketRegime.STRONG_TREND, MarketRegime.BREAKOUT, MarketRegime.SHOCK):
        risk_reducing_only = True

    if risk_reducing_only:
        # Long capability (buying to cover short)
        allowed_long_capacity = max(Decimal('0'), -current_inventory)
        # Short capability (selling to cover long)
        allowed_short_capacity = max(Decimal('0'), current_inventory)
    else:
        # Normal operation
        allowed_long_capacity = max(Decimal('0'), max_inventory - current_inventory)
        allowed_short_capacity = max(Decimal('0'), max_inventory + current_inventory)

    seen_bids = set()
    seen_asks = set()

    # Generate Bids
    for i in range(1, level_count + 1):
        target_price = best_bid - (grid_spacing * i)
        target_price = (target_price // tick_size) * tick_size

        # Ensure strict < best_ask (and < best_bid normally, but spacing * i > 0 so guaranteed unless tick_size rounds weirdly)
        if target_price >= best_bid:
            target_price = best_bid - tick_size

        if target_price <= 0:
            continue

        if target_price in seen_bids:
            continue

        amount = min(rounded_base_size, allowed_long_capacity)
        amount = (amount // step_size) * step_size

        if amount > 0:
            seen_bids.add(target_price)
            proposals.append(OrderProposal("BUY", target_price, amount))
            allowed_long_capacity -= amount
            if allowed_long_capacity <= 0:
                break # Reached long capacity

    # Generate Asks
    for i in range(1, level_count + 1):
        target_price = best_ask + (grid_spacing * i)

        # round price to tick size. we round up for asks
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
            proposals.append(OrderProposal("SELL", target_price, amount))
            allowed_short_capacity -= amount
            if allowed_short_capacity <= 0:
                break # Reached short capacity

    return proposals
