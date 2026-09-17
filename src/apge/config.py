import os
from dataclasses import dataclass
from decimal import Decimal


def _finite_decimal(name: str, raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except Exception as exc:
        raise ValueError(f"{name} must be a Decimal") from exc
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"{name} must be finite")
    return value


def _env_decimal(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    return default if raw is None else _finite_decimal(name, raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class RuntimeConfig:
    """Config-driven V1 runtime controls.

    Credentials are deliberately excluded. API keys/secrets are read only by the
    venue bootstrap from environment variables and never serialized here.
    """

    symbol: str = "BTCUSDT"
    db_path: str = "apge_state.sqlite3"
    position_limit: Decimal = Decimal("0.005")
    max_inventory: Decimal = Decimal("0.005")
    base_size: Decimal = Decimal("0.001")
    level_count: int = 1
    grid_spacing: Decimal = Decimal("100")
    cycle_interval_seconds: Decimal = Decimal("2")
    max_cycles: int = 5

    def validate(self) -> "RuntimeConfig":
        if not self.symbol or not self.symbol.replace("_", "").isalnum():
            raise ValueError("symbol must be a non-empty exchange symbol")
        if not self.db_path:
            raise ValueError("db_path must be non-empty")
        decimals = {
            "position_limit": self.position_limit,
            "max_inventory": self.max_inventory,
            "base_size": self.base_size,
            "grid_spacing": self.grid_spacing,
            "cycle_interval_seconds": self.cycle_interval_seconds,
        }
        for name, value in decimals.items():
            if not isinstance(value, Decimal) or value.is_nan() or value.is_infinite():
                raise ValueError(f"{name} must be a finite Decimal")
        if self.position_limit <= 0 or self.max_inventory <= 0:
            raise ValueError("position limits must be positive")
        if self.max_inventory > self.position_limit:
            raise ValueError("max_inventory cannot exceed position_limit")
        if self.base_size <= 0 or self.base_size > self.max_inventory:
            raise ValueError("base_size must be positive and <= max_inventory")
        if self.grid_spacing <= 0:
            raise ValueError("grid_spacing must be positive")
        if self.cycle_interval_seconds < 0:
            raise ValueError("cycle_interval_seconds cannot be negative")
        if self.level_count <= 0 or self.level_count > 10:
            raise ValueError("level_count must be in [1, 10]")
        if self.max_cycles <= 0:
            raise ValueError("max_cycles must be positive")
        return self

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        config = cls(
            symbol=os.environ.get("APGE_SYMBOL", cls.symbol),
            db_path=os.environ.get("APGE_DB_PATH", cls.db_path),
            position_limit=_env_decimal("APGE_POSITION_LIMIT", cls.position_limit),
            max_inventory=_env_decimal("APGE_MAX_INVENTORY", cls.max_inventory),
            base_size=_env_decimal("APGE_BASE_SIZE", cls.base_size),
            level_count=_env_int("APGE_LEVEL_COUNT", cls.level_count),
            grid_spacing=_env_decimal("APGE_GRID_SPACING", cls.grid_spacing),
            cycle_interval_seconds=_env_decimal(
                "APGE_CYCLE_INTERVAL_SECONDS", cls.cycle_interval_seconds
            ),
            max_cycles=_env_int("APGE_MAX_CYCLES", cls.max_cycles),
        )
        return config.validate()
