import importlib.util
from decimal import Decimal
from pathlib import Path


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


def test_validator_fails_closed_without_credentials(monkeypatch):
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    assert module.main() == 2
