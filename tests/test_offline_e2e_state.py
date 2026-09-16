import pytest
from decimal import Decimal

from apge.testnet_runtime import TestnetRuntime
from apge.execution_engine import ExecutionEngine
from apge.persistence import Persistence
from apge.simulator import RiskEngine, SystemState
from apge.reconciliation import Reconciler

class MockAdapter:
    def __init__(self):
        self.positions = []
        self.open_orders = []
    def get_positions(self): return self.positions
    def get_open_orders(self, symbol=None): return self.open_orders
    def parse_book_ticker(self, payload):
        return {
            "symbol": payload["s"],
            "bid_price": Decimal(str(payload["b"])),
            "ask_price": Decimal(str(payload["a"]))
        }

def test_connection_loss_transitions_state():
    # If connection is lost, system transitions to stale data
    # Actually, in our architecture, runtime marks self.is_connected = False
    # and stale data becomes True. The Grid proposal evaluates is_stale_data and goes to reduce-only mode.
    # We test this logic.
    adapter = MockAdapter()
    runtime = TestnetRuntime(adapter)

    runtime.handle_book_ticker({"s": "BTCUSDT", "b": "100.0", "a": "101.0", "u": 1})
    assert not runtime.is_stale_data()

    runtime.handle_connection_loss()
    assert runtime.is_stale_data()

def test_reconnect_reconciliation():
    # Reconnection happens externally (e.g., bot_runner detects ws drop and restarts).
    # On restart, Reconciler must run.
    db = Persistence()
    adapter = MockAdapter()
    reconciler = Reconciler(db, adapter)

    # We simulate a restart loop by successfully reconciling
    assert reconciler.resolve_state("BTCUSDT") == True

    db.close()
