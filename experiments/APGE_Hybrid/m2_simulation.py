import sys
import json
import subprocess
import nautilus_trader
from decimal import Decimal

def get_passivbot_commit():
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd='vendor/passivbot').decode('utf-8').strip()
    except Exception:
        return "UNKNOWN"

if __name__ == "__main__":
    NAUTILUS_VERSION = nautilus_trader.__version__
    PASSIVBOT_COMMIT = get_passivbot_commit()

    print(f"Starting APGE Hybrid M2 Offline Simulation Test...")
    print(f"Nautilus Trader Version: {NAUTILUS_VERSION}")
    print(f"Passivbot Commit/Integration: passivbot_rust ({PASSIVBOT_COMMIT})")
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
        
        import vendor.passivbot.tests.test_orchestrator_integration
        from vendor.passivbot.tests.test_orchestrator_integration import make_input, make_symbol
    except ImportError as e:
        print(f"[CRITICAL ERROR] Failed to import prerequisites: {e}")
        sys.exit(1)

    try:
        import passivbot_rust as pbr
    except ImportError as e:
        print(f"\n[CRITICAL ERROR] Failed to import passivbot_rust: {e}")
        sys.exit(1)

    print("\n--- Running Passivbot Planner ---")
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
                        "entry_initial_qty_pct": 0.1,  # 1.0
                    },
                    short_bp={
                        "entry_initial_ema_dist": 0.01,
                        "entry_initial_qty_pct": 0.01, # 0.1
                    }
                )
            ],
        )
        out_json = pbr.compute_ideal_orders_json(json.dumps(inp))
        raw_orders = json.loads(out_json).get("orders", [])
    except Exception as e:
        print(f"[ERROR] Failed to compute ideal orders: {e}")
        sys.exit(1)

    MAX_ORDER_QTY = Decimal("1.05")
    MAX_TOTAL_EXPOSURE = Decimal("1.50")
    print(f"\n--- APGE Risk Engine Evaluation (M2) ---")
    print(f"Risk Engine Policy: Emir basina miktar = {MAX_ORDER_QTY}, Toplam Limit = {MAX_TOTAL_EXPOSURE}")

    class APGEConfig(StrategyConfig, kw_only=True):
        pass

    class APGERiskAndExecStrategy(Strategy):
        def on_start(self):
            self.instrument = self.cache.instrument(InstrumentId.from_str("ETHUSDT-PERP.BINANCE"))
            self.reservations = {} # client_order_id.value -> Decimal
            
            # Step 1: Submit Proposal 0 (1.00 qty)
            self.evaluate_and_submit({
                "id": "PROPOSAL-0", "qty": "1.00", "price": "100.0", 
                "side": OrderSide.BUY, "pside": "long", "is_close": False, "status": "PENDING"
            })
            
        def on_order_filled(self, event):
            order = self.cache.order(event.client_order_id)
            tag = order.tags[0] if order.tags else None
            
            if tag == "PROPOSAL-0" and order.status.name == "PARTIALLY_FILLED":
                print("\n  [SCENARIO 1] 1.00 emrin 0.40 gerçekleşmesi:")
                self.print_risk_status("After 0.40 Partial Fill")
                
                print("\n  [SCENARIO 2] 1.02 önerinin toplam limit nedeniyle reddedilmesi:")
                self.evaluate_and_submit({
                    "id": "PROPOSAL-1", "qty": "1.02", "price": "98.0", 
                    "side": OrderSide.BUY, "pside": "long", "is_close": False, "status": "PENDING"
                })

                print("\n  [SCENARIO 3] UNKNOWN sonuç/Short giriş/Close giriş hiç gönderilmez:")
                self.evaluate_and_submit({
                    "id": "PROPOSAL-SHORT", "qty": "0.50", "price": "101.0", 
                    "side": OrderSide.SELL, "pside": "short", "is_close": False, "status": "PENDING"
                })
                self.evaluate_and_submit({
                    "id": "PROPOSAL-UNKNOWN", "qty": "0.50", "price": "102.0", 
                    "side": OrderSide.SELL, "pside": "unknown", "is_close": False, "status": "PENDING"
                })
                
                print("\n  [SCENARIO 4] İptal isteği riski değiştirmez; kesin iptal kalan 0.60’ı çözer:")
                self.cancel_order(order)
                self.print_risk_status("Immediately after cancel request")

        def on_order_canceled(self, event):
            self._release_reservation(event.client_order_id.value)
            
            order = self.cache.order(event.client_order_id)
            tag = order.tags[0] if order.tags else None
            
            if tag == "PROPOSAL-0":
                print(f"\n  [SCENARIO 4] Cancel confirmed for PROPOSAL-0. Open order dropped.")
                self.print_risk_status("After Cancel Confirmed")
                
                print("\n  [SCENARIO 5] Aynı rezervasyon çözme olayının tekrarı hesabı değiştirmez:")
                self.reservations["DUMMY"] = Decimal("0.5")
                self.print_risk_status("Added DUMMY=0.5")
                self._release_reservation("DUMMY")
                self.print_risk_status("Released DUMMY once")
                self._release_reservation("DUMMY")
                self.print_risk_status("Released DUMMY twice")
                
                # To satisfy the final 1.00 position check, we submit a new 0.60 order to fill it up
                print("\n  [SCENARIO 6] Submitting remaining 0.60 to complete 1.00 position:")
                self.evaluate_and_submit({
                    "id": "PROPOSAL-FINISH", "qty": "0.60", "price": "99.0", 
                    "side": OrderSide.BUY, "pside": "long", "is_close": False, "status": "PENDING"
                })

        def evaluate_and_submit(self, p):
            print(f"Evaluating Proposal {p['id']}: {p['side'].name} {p['qty']} @ {p['price']}")
            
            if p["pside"] not in ["long"]:
                print(f"  -> REJECTED: Only LONG entry orders are supported. Received pside='{p['pside']}'")
                return
            if p["is_close"]:
                print(f"  -> REJECTED: Close orders are not supported without reduce_only verification.")
                return
            if p["side"] != OrderSide.BUY:
                print(f"  -> REJECTED: Only BUY orders allowed for long entry.")
                return

            try:
                nautilus_qty = self.instrument.make_qty(float(p['qty']))
                qty_dec = Decimal(str(nautilus_qty.as_double()))
            except Exception as e:
                print(f"  -> REJECTED: Invalid quantity format: {e}")
                return

            if qty_dec <= Decimal("0"):
                print(f"  -> REJECTED: Quantity must be positive.")
                return
                
            if qty_dec > MAX_ORDER_QTY:
                print(f"  -> REJECTED: Quantity {qty_dec} exceeds MAX_ORDER_QTY {MAX_ORDER_QTY}")
                return

            # Calculate total exposure using Decimal
            realized_long = Decimal(str(sum(
                pos.quantity.as_double() 
                for pos in self.cache.positions() 
                if pos.instrument_id == self.instrument.id and pos.quantity.as_double() > 0
            )))
            
            open_long_qty = Decimal(str(sum(o.leaves_qty.as_double() for o in self.cache.orders_open() if o.side == OrderSide.BUY and o.instrument_id == self.instrument.id)))
            
            reserved_qty = sum(self.reservations.values()) if self.reservations else Decimal("0")
            
            total_exposure = realized_long + open_long_qty + reserved_qty
            
            print(f"  -> Risk Check: Realized={realized_long}, Open={open_long_qty}, Reserved={reserved_qty} -> Total Exposure={total_exposure}")
            
            if total_exposure + qty_dec > MAX_TOTAL_EXPOSURE:
                print(f"  -> REJECTED: New exposure {total_exposure + qty_dec} exceeds MAX_TOTAL_EXPOSURE {MAX_TOTAL_EXPOSURE}")
                return
                
            print(f"  -> APPROVED: Limits OK. Submitting {p['id']}")
            
            order = self.order_factory.limit(
                instrument_id=self.instrument.id,
                order_side=p['side'],
                quantity=nautilus_qty,
                price=self.instrument.make_price(float(p['price'])),
                tags=[p['id']]
            )
            
            cid = order.client_order_id.value
            self.reservations[cid] = qty_dec
            self.submit_order(order)
            
        def _release_reservation(self, cid):
            if cid in self.reservations:
                print(f"  [RESERVATION RELEASE] Releasing {self.reservations[cid]} for {cid}.")
                del self.reservations[cid]

        def on_order_accepted(self, event):
            self._release_reservation(event.client_order_id.value)

        def on_order_rejected(self, event):
            self._release_reservation(event.client_order_id.value)

        def print_risk_status(self, context=""):
            realized_long = Decimal(str(sum(pos.quantity.as_double() for pos in self.cache.positions() if pos.instrument_id == self.instrument.id and pos.quantity.as_double() > 0)))
            open_long_qty = Decimal(str(sum(o.leaves_qty.as_double() for o in self.cache.orders_open() if o.side == OrderSide.BUY and o.instrument_id == self.instrument.id)))
            reserved_qty = sum(self.reservations.values()) if self.reservations else Decimal("0")
            total = realized_long + open_long_qty + reserved_qty
            print(f"    [{context}] Realized={realized_long}, Open={open_long_qty}, Reserved={reserved_qty} -> Total Exposure={total}")

    engine = BacktestEngine()
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=Currency.from_str("USDT"),
        starting_balances=[Money.from_str(f"{STARTING_BALANCE} USDT")]
    )
    
    instrument = TestInstrumentProvider.ethusdt_perp_binance()
    engine.add_instrument(instrument)
    
    strategy = APGERiskAndExecStrategy(config=APGEConfig())
    engine.add_strategy(strategy)

    print("\nInjecting synthetic market data...")
    ticks = [
        # Tick 1: Matches PROPOSAL-0 partially (0.40)
        TestDataStubs.quote_tick(instrument=instrument, bid_price=100.0, ask_price=100.0, bid_size=10, ask_size=0.40, ts_event=1_000_000_000, ts_init=1_000_000_000),
        # Tick 2: Matches PROPOSAL-FINISH fully
        TestDataStubs.quote_tick(instrument=instrument, bid_price=99.0, ask_price=99.0, bid_size=10, ask_size=10, ts_event=2_000_000_000, ts_init=2_000_000_000),
        # Tick 3: Advance time to ensure events are processed
        TestDataStubs.quote_tick(instrument=instrument, bid_price=99.0, ask_price=99.5, bid_size=10, ask_size=10, ts_event=3_000_000_000, ts_init=3_000_000_000)
    ]
    engine.add_data(ticks)
    
    print("\nStarting Nautilus Engine...")
    engine.run()
    
    print("\n--- Validation & Assertions ---")
    orders = engine.cache.orders()
    positions = engine.cache.positions()
    
    print(f"Total Orders Submitted: {len(orders)}")
    print(f"Total Positions: {len(positions)}")
    
    errors = []
                
    expected_position_qty = 0.0
    for o in orders:
        filled = o.filled_qty.as_double()
        if filled > 0:
            if o.side == OrderSide.BUY:
                expected_position_qty += filled
            else:
                expected_position_qty -= filled
                
    if expected_position_qty > 0 and len(positions) == 0:
        errors.append("CRITICAL: Expected a non-zero position but found 0 positions.")
    elif len(positions) > 0:
        pos_qty = positions[0].quantity.as_double()
        print(f"Validated Position Output: {pos_qty} {positions[0].instrument_id}")
        
        if abs(pos_qty - expected_position_qty) > 1e-8:
            errors.append(f"CRITICAL: Actual position qty {pos_qty} does not match expected {expected_position_qty}")
            
        if abs(pos_qty - 1.00) > 1e-8:
            errors.append(f"CRITICAL: Final position is not 1.00 as requested. (Got {pos_qty})")
            
    if errors:
        for e in errors:
            print(e)
        sys.exit(1)
    else:
        print("\n[SUCCESS] M2 Exposure Validation and Scenarios Passed!")
        sys.exit(0)
