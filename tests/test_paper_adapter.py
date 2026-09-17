from decimal import Decimal

from apge.execution_engine import ExecutionEngine
from apge.grid_strategy import OrderProposal
from apge.market_regime import Candle
from apge.paper_adapter import PaperAdapter
from apge.persistence import Persistence
from apge.reconciliation import Reconciler
from apge.simulator import RiskEngine, SystemState


def _stack():
    adapter = PaperAdapter()
    persistence = Persistence(":memory:")
    risk = RiskEngine(Decimal("0.01"), require_explicit_side=True)
    risk.system_state = SystemState.OPERATIONAL
    execution = ExecutionEngine(persistence, adapter, risk)
    reconciler = Reconciler(persistence, adapter, risk)
    return adapter, persistence, risk, execution, reconciler


def test_paper_submit_and_authoritative_cancel():
    adapter, persistence, risk, execution, _ = _stack()
    cid = execution.execute_proposal(
        "BTCUSDT", OrderProposal("BUY", Decimal("100"), Decimal("0.001")))
    assert cid is not None
    assert len(adapter.get_open_orders("BTCUSDT")) == 1
    assert risk.reservations == Decimal("0.001")

    assert execution.cancel_order("BTCUSDT", cid)
    assert adapter.get_open_orders("BTCUSDT") == []
    assert persistence.get_intent(cid)["status"] == "CANCELED"
    assert risk.reservations == 0
    persistence.close()


def test_paper_fill_updates_exchange_persistence_and_signed_risk():
    adapter, persistence, risk, execution, reconciler = _stack()
    cid = execution.execute_proposal(
        "BTCUSDT", OrderProposal("BUY", Decimal("100"), Decimal("0.001")))
    assert cid is not None

    events = adapter.process_candle(
        Candle(Decimal("101"), Decimal("102"), Decimal("99"), Decimal("101.5")))
    assert len(events) == 1
    execution.handle_order_update(events[0])

    assert adapter.position == Decimal("0.001")
    assert risk.current_position == Decimal("0.001")
    assert risk.reservations == 0
    assert persistence.get_intent(cid)["status"] == "FILLED"
    assert persistence.get_intent(cid)["filled_quantity"] == "0.001"

    risk.restore_connection()
    assert reconciler.resolve_state("BTCUSDT")
    risk.complete_reconciliation()
    assert risk.system_state == SystemState.OPERATIONAL
    assert risk.current_position == adapter.position
    persistence.close()


def test_paper_dual_touch_is_conservative_and_one_sided():
    adapter, persistence, risk, execution, _ = _stack()
    buy = execution.execute_proposal(
        "BTCUSDT", OrderProposal("BUY", Decimal("99"), Decimal("0.001")))
    sell = execution.execute_proposal(
        "BTCUSDT", OrderProposal("SELL", Decimal("101"), Decimal("0.001")))
    assert buy and sell

    # Both levels are touched. For a bullish candle while flat, the conservative
    # rule fills only SELL; it never grants a same-bar two-sided round trip.
    events = adapter.process_candle(
        Candle(Decimal("100"), Decimal("102"), Decimal("98"), Decimal("101")))
    assert len(events) == 1
    assert events[0]["side"] == "SELL"
    execution.handle_order_update(events[0])
    assert adapter.position == Decimal("-0.001")
    assert risk.current_position == Decimal("-0.001")
    assert len(adapter.get_open_orders("BTCUSDT")) == 1
    persistence.close()


def test_paper_rejects_malformed_candle_without_mutating_orders():
    adapter, persistence, risk, execution, _ = _stack()
    cid = execution.execute_proposal(
        "BTCUSDT", OrderProposal("BUY", Decimal("99"), Decimal("0.001")))
    assert cid is not None

    try:
        adapter.process_candle(
            Candle(Decimal("100"), Decimal("90"), Decimal("95"), Decimal("96")))
        assert False, "expected malformed candle rejection"
    except ValueError:
        pass

    assert len(adapter.get_open_orders("BTCUSDT")) == 1
    assert risk.reservations == Decimal("0.001")
    persistence.close()
