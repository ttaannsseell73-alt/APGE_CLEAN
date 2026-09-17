from decimal import Decimal

from apge.execution_engine import ExecutionEngine
from apge.grid_strategy import OrderProposal
from apge.persistence import Persistence
from apge.reconciliation import Reconciler
from apge.simulator import OrderState, RiskEngine, SystemState


D = Decimal


class RestartExchange:
    def __init__(self):
        self.orders = {}
        self.next_id = 1
        self.position = D("0")

    @staticmethod
    def _map_order_state(status):
        return {
            "NEW": OrderState.OPEN,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
        }.get(status, OrderState.UNKNOWN)

    def submit_limit_order(self, symbol, side, quantity, price, client_order_id, time_in_force="GTC"):
        order = {
            "symbol": symbol,
            "side": side,
            "origQty": str(quantity),
            "price": str(price),
            "clientOrderId": client_order_id,
            "orderId": self.next_id,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
        }
        self.next_id += 1
        self.orders[client_order_id] = order
        return dict(order)

    def cancel_order(self, symbol, orig_client_order_id):
        order = self.orders[orig_client_order_id]
        assert order["symbol"] == symbol
        order["status"] = "CANCELED"
        return dict(order)

    def get_positions(self):
        return [{"symbol": "BTCUSDT", "positionAmt": str(self.position), "entryPrice": "0"}]

    def get_open_orders(self, symbol=None):
        return [
            dict(order) for order in self.orders.values()
            if order["status"] in ("NEW", "PARTIALLY_FILLED")
            and (symbol is None or order["symbol"] == symbol)
        ]

    def query_order(self, symbol, orig_client_order_id):
        order = self.orders[orig_client_order_id]
        assert order["symbol"] == symbol
        return dict(order)

    def get_server_time(self):
        return {"serverTime": 1}

    def get_exchange_info(self):
        return {"symbols": []}


def test_file_backed_restart_rebuilds_risk_and_cancel_is_idempotently_reconciled(tmp_path):
    db_path = tmp_path / "restart.sqlite3"
    exchange = RestartExchange()

    first_db = Persistence(str(db_path))
    first_risk = RiskEngine(D("5"), require_explicit_side=True)
    first_exec = ExecutionEngine(first_db, exchange, first_risk)
    cid = first_exec.execute_proposal(
        "BTCUSDT", OrderProposal("BUY", D("100"), D("1"))
    )
    assert cid is not None
    assert first_risk.reservations == D("1")
    first_db.close()

    restarted_db = Persistence(str(db_path))
    restarted_risk = RiskEngine(D("5"), require_explicit_side=True)
    restarted_risk.system_state = SystemState.RECONCILING
    reconciler = Reconciler(restarted_db, exchange, restarted_risk)

    assert reconciler.resolve_state("BTCUSDT") is True
    restarted_risk.complete_reconciliation()
    assert restarted_risk.system_state == SystemState.OPERATIONAL
    assert restarted_risk.reservations == D("1")
    assert cid in restarted_risk.orders

    # A second identical authoritative snapshot must not duplicate reservation.
    restarted_risk.system_state = SystemState.RECONCILING
    assert reconciler.resolve_state("BTCUSDT") is True
    restarted_risk.complete_reconciliation()
    assert restarted_risk.reservations == D("1")

    restarted_exec = ExecutionEngine(restarted_db, exchange, restarted_risk)
    assert restarted_exec.cancel_order("BTCUSDT", cid) is True
    assert restarted_risk.reservations == D("0")
    assert restarted_db.get_active_intents() == []

    restarted_risk.system_state = SystemState.RECONCILING
    assert reconciler.resolve_state("BTCUSDT") is True
    restarted_risk.complete_reconciliation()
    assert restarted_risk.system_state == SystemState.OPERATIONAL
    assert restarted_risk.reservations == D("0")
    restarted_db.close()
