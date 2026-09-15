import sys
import os
import json
import uuid

# Set up paths so we can import the vendored passivbot
current_dir = os.path.dirname(os.path.abspath(__file__))
passivbot_src_path = os.path.join(current_dir, 'vendor', 'passivbot', 'src')
passivbot_test_path = os.path.join(current_dir, 'vendor', 'passivbot', 'tests')
sys.path.insert(0, passivbot_src_path)
sys.path.insert(0, passivbot_test_path)

# Define versions for reporting
NAUTILUS_VERSION = "1.231.0"
PASSIVBOT_COMMIT = "e.g., specific commit hash used in APGE"

print(f"Starting APGE Hybrid M1 Offline Simulation Test...")
print(f"Nautilus Trader Version: {NAUTILUS_VERSION}")
print(f"Passivbot Commit/Integration: passivbot_rust via compile")
print(f"Planner Function: pbr.compute_ideal_orders_json")

try:
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.model.objects import Price, Quantity, Currency, Money
    from nautilus_trader.model.enums import OrderSide, OmsType, AccountType
    from nautilus_trader.trading.strategy import Strategy
    from nautilus_trader.model.identifiers import Venue, InstrumentId
    from nautilus_trader.trading.config import StrategyConfig
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.test_kit.stubs.data import TestDataStubs
    
    # Internal passivbot test helpers for mock payload
    import vendor.passivbot.tests.test_orchestrator_integration
    from vendor.passivbot.tests.test_orchestrator_integration import make_input, make_symbol
    print("[SUCCESS] Nautilus Trader and test helpers imported successfully.")
except ImportError as e:
    print(f"[CRITICAL ERROR] Failed to import prerequisites: {e}")
    sys.exit(1)

print("Attempting to import passivbot and its rust planner module...")
try:
    import passivbot_rust as pbr
    print("[SUCCESS] passivbot_rust imported successfully.")
except ImportError as e:
    print(f"\n[CRITICAL ERROR] Failed to import passivbot_rust: {e}")
    sys.exit(1)

print("\n--- Running Passivbot Planner ---")
# Make planner balance match simulation balance
STARTING_BALANCE = 1_000.0
try:
    inp = make_input(
        balance=STARTING_BALANCE,
        symbols=[
            make_symbol(
                0,
                bid=100.0,
                ask=100.0,
                long_bp={
                    "entry_initial_ema_dist": -0.01,
                    "entry_initial_qty_pct": 0.1,  # Creates a large order (qty=1.0)
                },
                short_bp={
                    "entry_initial_ema_dist": 0.01,
                    "entry_initial_qty_pct": 0.01, # Creates a small order (qty=0.1)
                }
            )
        ],
    )
    
    out_json = pbr.compute_ideal_orders_json(json.dumps(inp))
    out = json.loads(out_json)
    raw_orders = out.get("orders", [])
    print(f"Passivbot planner returned {len(raw_orders)} orders.")
except Exception as e:
    print(f"[ERROR] Failed to compute ideal orders: {e}")
    sys.exit(1)

print("\n--- APGE Risk Engine Evaluation ---")
# Translated proposal records
proposals = []
for i, ro in enumerate(raw_orders):
    o_type = ro.get("order_type", "")
    pside = ro.get("pside", "")
    
    # 1. Translate Direction (Giriş/Kapanış ayrımını koru)
    side = None
    if "entry" in o_type:
        side = OrderSide.BUY if pside == "long" else OrderSide.SELL
    elif "close" in o_type:
        side = OrderSide.SELL if pside == "long" else OrderSide.BUY
    else:
        print(f"[REJECTED] Unknown order_type in proposal: {ro}")
        continue
    
    prop_id = f"PROPOSAL-{i}"
    proposals.append({
        "id": prop_id,
        "qty": abs(ro["qty"]),
        "price": ro["price"],
        "side": side,
        "raw_type": o_type,
        "pside": pside,
        "is_accepted": False
    })

# 4. Emir Başına Miktar Filtresi
MAX_ORDER_QTY = 1.05
print(f"Risk Engine Policy: Emir başına miktar filtresi = {MAX_ORDER_QTY}")

accepted_proposals = []
rejected_proposals = []

for p in proposals:
    print(f"Evaluating Proposal {p['id']}: {p['side'].name} {p['qty']} @ {p['price']} (Raw Type: {p['raw_type']})")
    
    # APGE check
    if p["qty"] > MAX_ORDER_QTY:
        print(f"  -> REJECTED: Quantity {p['qty']} exceeds MAX_ORDER_QTY {MAX_ORDER_QTY}")
        rejected_proposals.append(p)
    else:
        print(f"  -> APPROVED: Quantity {p['qty']} is within limits")
        p["is_accepted"] = True
        accepted_proposals.append(p)

print("\n--- Mapping to Nautilus Trader ---")

class APGEConfig(StrategyConfig, kw_only=True):
    approved_proposals: list = []

class APGEMockStrategy(Strategy):
    def on_start(self):
        for p in self.config.approved_proposals:
            instrument = self.cache.instrument(InstrumentId.from_str("ETHUSDT-PERP.BINANCE"))
            
            # Re-check quantity after make_qty precision conversion
            final_qty_obj = instrument.make_qty(p["qty"])
            final_qty_float = final_qty_obj.as_double()
            
            if final_qty_float > MAX_ORDER_QTY:
                print(f"[Nautilus Strategy] Post-conversion precision error. Rejecting {p['id']}")
                continue
                
            print(f"[Nautilus Strategy] Submitting Limit Order for Proposal {p['id']}: {p['side']} {final_qty_float} @ {p['price']}")
            order = self.order_factory.limit(
                instrument_id=instrument.id,
                order_side=p['side'],
                quantity=final_qty_obj,
                price=instrument.make_price(p['price']),
                tags=[p['id']]
            )
            self.submit_order(order)
            print(f"[Nautilus Strategy] Order Record Created: {order}")

# Create engine with Perpetual Netting Setup
engine = BacktestEngine()
engine.add_venue(
    venue=Venue("BINANCE"),
    oms_type=OmsType.NETTING,          # NETTING mode
    account_type=AccountType.MARGIN,
    base_currency=Currency.from_str("USDT"),
    starting_balances=[Money.from_str(f"{STARTING_BALANCE} USDT")]
)

# Use Perpetual test instrument
instrument = TestInstrumentProvider.ethusdt_perp_binance()
engine.add_instrument(instrument)

strategy = APGEMockStrategy(config=APGEConfig(approved_proposals=accepted_proposals))
engine.add_strategy(strategy)

# Add synthetic market data to trigger execution of the accepted limit orders
# Specifically we expect some orders around price 100.0 and 98.0
print("\nInjecting synthetic market data...")
ticks = [
    TestDataStubs.quote_tick(instrument=instrument, bid_price=100.5, ask_price=101.0, ts_event=1_000_000_000, ts_init=1_000_000_000),
    TestDataStubs.quote_tick(instrument=instrument, bid_price=99.0, ask_price=99.5, ts_event=2_000_000_000, ts_init=2_000_000_000),
    TestDataStubs.quote_tick(instrument=instrument, bid_price=97.0, ask_price=97.5, ts_event=3_000_000_000, ts_init=3_000_000_000)
]
engine.add_data(ticks)

print("\nStarting Nautilus Engine...")
engine.run()

print("\n--- Validation & Assertions ---")
orders = engine.cache.orders()
positions = engine.cache.positions()

# Get client order IDs to trace back to proposals
submitted_proposal_ids = []
for order in orders:
    if order.tags:
        submitted_proposal_ids.extend(order.tags)

print(f"Total Orders Submitted: {len(orders)}")
print(f"Total Positions: {len(positions)}")

# Assertions
errors = []

# Assert 1: Only accepted proposals were submitted
for p in rejected_proposals:
    if p["id"] in submitted_proposal_ids:
        errors.append(f"CRITICAL: Rejected proposal {p['id']} was submitted to Nautilus!")

# Assert 2: All accepted proposals were submitted
for p in accepted_proposals:
    if p["id"] not in submitted_proposal_ids:
        errors.append(f"CRITICAL: Accepted proposal {p['id']} was NEVER submitted to Nautilus!")

# Assert 3: A valid order was filled and a position was created
if len(positions) == 0:
    errors.append("CRITICAL: No positions were created, simulation didn't fill the accepted orders.")
else:
    pos = positions[0]
    print(f"Validated Position Output: {pos.quantity} {pos.instrument_id}")
    if pos.quantity.as_double() == 0:
        errors.append("CRITICAL: Position was created but quantity is 0.")

if errors:
    for e in errors:
        print(e)
    sys.exit(1)
else:
    print("\n[SUCCESS] M1 Integration Test Complete and Validated!")
    sys.exit(0)
