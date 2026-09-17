import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "apge03_controlled_grid.py"
spec = importlib.util.spec_from_file_location("apge03_controlled_grid", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_ceil_to_step_is_deterministic():
    assert module.ceil_to_step(Decimal("0.00101"), Decimal("0.001")) == Decimal("0.002")
    assert module.ceil_to_step(Decimal("0.001"), Decimal("0.001")) == Decimal("0.001")


def test_minimum_valid_base_size_respects_qty_and_notional():
    size = module.minimum_valid_base_size(
        Decimal("75000"), Decimal("0.001"), Decimal("0.001"), Decimal("5"))
    assert size == Decimal("0.001")

    size_low_price = module.minimum_valid_base_size(
        Decimal("1000"), Decimal("0.001"), Decimal("0.001"), Decimal("5"))
    assert size_low_price == Decimal("0.005")
    assert size_low_price > module.HARD_MAX_BASE_SIZE


def test_created_order_snapshot_requires_authoritative_executed_qty():
    class Adapter:
        def query_order(self, symbol, cid):
            assert symbol == module.SYMBOL
            return {
                "clientOrderId": cid,
                "status": "FILLED" if cid == "APGE_A" else "NEW",
                "executedQty": "0.001" if cid == "APGE_A" else "0",
                "orderId": 1,
            }

    snapshot = module._created_order_snapshot(Adapter(), {"APGE_A", "APGE_B"})
    assert snapshot["APGE_A"]["status"] == "FILLED"
    assert snapshot["APGE_A"]["executedQty"] == "0.001"
    assert snapshot["APGE_B"]["executedQty"] == "0"


@pytest.mark.parametrize("bad", [None, "NaN", "Infinity", "-0.001", "not-a-number"])
def test_created_order_snapshot_fails_closed_on_invalid_executed_qty(bad):
    class Adapter:
        def query_order(self, symbol, cid):
            return {"status": "CANCELED", "executedQty": bad, "orderId": 1}

    with pytest.raises(RuntimeError, match="invalid executedQty"):
        module._created_order_snapshot(Adapter(), {"APGE_A"})


def test_validator_fails_closed_without_credentials(monkeypatch):
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    assert module.main() == 2
