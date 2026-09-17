import inspect

import apge.execution_engine as execution_engine
import apge.reconciliation as reconciliation
import apge.simulator as simulator


def test_core_risk_execution_reconciliation_do_not_import_venue_adapter():
    for module in (simulator, execution_engine, reconciliation):
        source = inspect.getsource(module)
        assert "apge.binance_adapter" not in source
        assert "BinanceAdapter" not in source


def test_core_execution_depends_on_exchange_protocol_boundary():
    source = inspect.getsource(execution_engine)
    assert "from apge.exchange import ExchangeAdapter" in source
    source = inspect.getsource(reconciliation)
    assert "from apge.exchange import ExchangeAdapter" in source
