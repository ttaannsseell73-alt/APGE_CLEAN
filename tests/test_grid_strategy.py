import pytest
from decimal import Decimal
from apge.simulator import SystemState
from apge.grid_strategy import generate_grid_proposals, MarketRegime, OrderProposal

def test_neutral_inventory():
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('0.0'),
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )

    assert len(proposals) == 6
    bids = [p for p in proposals if p.side == 'BUY']
    asks = [p for p in proposals if p.side == 'SELL']

    assert len(bids) == 3
    assert len(asks) == 3
    assert bids[0].price == Decimal('99.0')
    assert bids[1].price == Decimal('98.0')
    assert bids[2].price == Decimal('97.0')
    assert asks[0].price == Decimal('102.0')
    assert asks[1].price == Decimal('103.0')
    assert asks[2].price == Decimal('104.0')
    for p in proposals:
        assert p.quantity == Decimal('1.0')

def test_long_inventory_bias():
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('4.5'), # Limit is 5.0, so only 0.5 capacity left for bids
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )

    bids = [p for p in proposals if p.side == 'BUY']
    asks = [p for p in proposals if p.side == 'SELL']

    assert len(bids) == 1
    assert bids[0].quantity == Decimal('0.5')

    assert len(asks) == 3 # Should allow up to 9.5 (5.0 + 4.5) capacity
    for ask in asks:
        assert ask.quantity == Decimal('1.0')

def test_short_inventory_bias():
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('-4.5'), # Short 4.5. Limit is 5.0 short, so only 0.5 capacity left for asks
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )

    bids = [p for p in proposals if p.side == 'BUY']
    asks = [p for p in proposals if p.side == 'SELL']

    assert len(asks) == 1
    assert asks[0].quantity == Decimal('0.5')

    assert len(bids) == 3
    for bid in bids:
        assert bid.quantity == Decimal('1.0')

def test_max_inventory():
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('5.0'), # At max long inventory
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )
    bids = [p for p in proposals if p.side == 'BUY']
    asks = [p for p in proposals if p.side == 'SELL']
    assert len(bids) == 0
    assert len(asks) == 3

    proposals_short = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('-5.0'), # At max short inventory
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )
    bids_short = [p for p in proposals_short if p.side == 'BUY']
    asks_short = [p for p in proposals_short if p.side == 'SELL']
    assert len(bids_short) == 3
    assert len(asks_short) == 0

def test_stale_data_reduce_only():
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('2.0'), # Long 2.0. Can only sell.
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=True
    )
    bids = [p for p in proposals if p.side == 'BUY']
    asks = [p for p in proposals if p.side == 'SELL']
    assert len(bids) == 0
    assert len(asks) == 2 # 2 proposals of 1.0 each since current_inventory = 2.0

def test_halted_reconciling_states():
    # Halted -> no proposals
    proposals_halted = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('2.0'),
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.HALTED,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )
    assert len(proposals_halted) == 0

    # Reconciling -> reduce only
    proposals_reconciling = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('-2.0'), # Short 2.0 -> can only buy
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.RECONCILING,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )
    bids = [p for p in proposals_reconciling if p.side == 'BUY']
    asks = [p for p in proposals_reconciling if p.side == 'SELL']
    assert len(bids) == 2
    assert len(asks) == 0

def test_rounding():
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.01'),
        best_ask=Decimal('101.09'),
        current_inventory=Decimal('0.0'),
        grid_spacing=Decimal('0.97'),
        base_size=Decimal('1.25'),
        level_count=2,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.05'),
        step_size=Decimal('0.2'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )
    bids = [p for p in proposals if p.side == 'BUY']
    asks = [p for p in proposals if p.side == 'SELL']

    # Bids round down price (towards zero / tick floor) -> 100.01 - 0.97 = 99.04 -> 99.00
    assert bids[0].price == Decimal('99.00')
    # Bids round down price -> 100.01 - 1.94 = 98.07 -> 98.05
    assert bids[1].price == Decimal('98.05')

    # Asks round up price (towards infinity / tick ceiling) -> 101.09 + 0.97 = 102.06 -> 102.10
    assert asks[0].price == Decimal('102.10')
    # Asks round up price -> 101.09 + 1.94 = 103.03 -> 103.05
    assert asks[1].price == Decimal('103.05')

    # Quantity rounded down to step size: 1.25 -> 1.2
    for p in proposals:
        assert p.quantity == Decimal('1.2')

def test_deterministic_repeatability():
    kwargs = {
        'best_bid': Decimal('100.0'),
        'best_ask': Decimal('101.0'),
        'current_inventory': Decimal('1.5'),
        'grid_spacing': Decimal('0.5'),
        'base_size': Decimal('1.5'),
        'level_count': 5,
        'max_inventory': Decimal('5.0'),
        'tick_size': Decimal('0.1'),
        'step_size': Decimal('0.5'),
        'system_state': SystemState.OPERATIONAL,
        'market_regime': MarketRegime.SLIGHT_UP,
        'is_stale_data': False
    }

    run_1 = generate_grid_proposals(**kwargs)
    run_2 = generate_grid_proposals(**kwargs)

    assert len(run_1) == len(run_2)
    for p1, p2 in zip(run_1, run_2):
        assert p1 == p2

def test_invalid_bba():
    # Crossed
    assert len(generate_grid_proposals(
        best_bid=Decimal('101.0'),
        best_ask=Decimal('100.0'),
        current_inventory=Decimal('0.0'),
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )) == 0

    # Zero
    assert len(generate_grid_proposals(
        best_bid=Decimal('0.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('0.0'),
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )) == 0

def test_zero_negative_quantities():
    assert len(generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('0.0'),
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('-1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )) == 0

def test_strong_trend_breakout_shock_regimes():
    # Long inventory, only sells allowed
    for regime in [MarketRegime.STRONG_TREND, MarketRegime.BREAKOUT, MarketRegime.SHOCK]:
        proposals = generate_grid_proposals(
            best_bid=Decimal('100.0'),
            best_ask=Decimal('101.0'),
            current_inventory=Decimal('2.0'),
            grid_spacing=Decimal('1.0'),
            base_size=Decimal('1.0'),
            level_count=3,
            max_inventory=Decimal('5.0'),
            tick_size=Decimal('0.1'),
            step_size=Decimal('0.1'),
            system_state=SystemState.OPERATIONAL,
            market_regime=regime,
            is_stale_data=False
        )
        assert len([p for p in proposals if p.side == 'BUY']) == 0
        assert len([p for p in proposals if p.side == 'SELL']) == 2

    # Flat inventory, no orders allowed
    for regime in [MarketRegime.STRONG_TREND, MarketRegime.BREAKOUT, MarketRegime.SHOCK]:
        proposals = generate_grid_proposals(
            best_bid=Decimal('100.0'),
            best_ask=Decimal('101.0'),
            current_inventory=Decimal('0.0'),
            grid_spacing=Decimal('1.0'),
            base_size=Decimal('1.0'),
            level_count=3,
            max_inventory=Decimal('5.0'),
            tick_size=Decimal('0.1'),
            step_size=Decimal('0.1'),
            system_state=SystemState.OPERATIONAL,
            market_regime=regime,
            is_stale_data=False
        )
        assert len(proposals) == 0

def test_connection_lost_state():
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('0.0'),
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.CONNECTION_LOST,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )
    assert len(proposals) == 0

def test_duplicate_price_levels_dont_consume_capacity():
    # Grid spacing is smaller than tick size
    # Levels will round to the same tick, we must only get 1 proposal per unique price tick
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('0.0'),
        grid_spacing=Decimal('0.01'), # Grid spacing smaller than tick size
        base_size=Decimal('1.0'),
        level_count=10,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )

    bids = [p for p in proposals if p.side == 'BUY']
    asks = [p for p in proposals if p.side == 'SELL']

    # Due to tick_size 0.1, multiple grid intervals (0.01) will map to the same tick.
    # The first bid is 100.0 - 0.01 = 99.99 -> 99.9
    # The tenth bid is 100.0 - 0.10 = 99.90 -> 99.9
    # All 10 levels map to 99.9. Thus, only ONE proposal should be generated.
    assert len(bids) == 1
    assert bids[0].price == Decimal('99.9')
    assert bids[0].quantity == Decimal('1.0') # Still 1.0 because duplicates didn't consume capacity

    # Similarly for asks: 101.0 + 0.01 = 101.01 -> 101.1
    assert len(asks) == 1
    assert asks[0].price == Decimal('101.1')
    assert asks[0].quantity == Decimal('1.0')

def test_flat_inventory_reduce_only():
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('0.0'),
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.0'),
        level_count=3,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.RECONCILING, # Forces reduce-only
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )
    assert len(proposals) == 0

def test_max_inventory_bounds_never_exceeded():
    proposals = generate_grid_proposals(
        best_bid=Decimal('100.0'),
        best_ask=Decimal('101.0'),
        current_inventory=Decimal('1.2'),
        grid_spacing=Decimal('1.0'),
        base_size=Decimal('1.5'),
        level_count=10,
        max_inventory=Decimal('5.0'),
        tick_size=Decimal('0.1'),
        step_size=Decimal('0.1'),
        system_state=SystemState.OPERATIONAL,
        market_regime=MarketRegime.NEUTRAL,
        is_stale_data=False
    )

    bids = [p for p in proposals if p.side == 'BUY']
    total_buy_qty = sum(p.quantity for p in bids)
    # Long capacity: 5.0 - 1.2 = 3.8
    assert total_buy_qty <= Decimal('3.8')
    assert total_buy_qty == Decimal('3.8') # 1.5 + 1.5 + 0.8

    asks = [p for p in proposals if p.side == 'SELL']
    total_sell_qty = sum(p.quantity for p in asks)
    # Short capacity: 5.0 + 1.2 = 6.2
    assert total_sell_qty <= Decimal('6.2')
    assert total_sell_qty == Decimal('6.2') # 1.5 * 4 = 6.0, + 0.2
