import os
from dataclasses import dataclass
from decimal import Decimal


_KLINE_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"
}


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
    """Config-driven deterministic V1 runtime controls.

    Credentials are deliberately excluded. API keys/secrets are read only by the
    venue bootstrap from environment variables and never serialized here.
    """

    symbol: str = "BTCUSDT"
    db_path: str = "apge_state.sqlite3"
    position_limit: Decimal = Decimal("0.005")
    max_inventory: Decimal = Decimal("0.005")
    base_size: Decimal = Decimal("0.001")
    level_count: int = 1
    # Retained for the controlled/static validation path. The normal V1 runner
    # derives live spacing from AdaptivePolicyConfig.
    grid_spacing: Decimal = Decimal("100")
    cycle_interval_seconds: Decimal = Decimal("2")
    max_cycles: int = 5

    adaptive_interval: str = "5m"
    regime_lookback: int = 20
    base_spacing_bps: Decimal = Decimal("8")
    min_spacing_bps: Decimal = Decimal("5")
    max_spacing_bps: Decimal = Decimal("60")
    volatility_spacing_multiplier: Decimal = Decimal("1.5")
    min_size_scale: Decimal = Decimal("0.40")
    size_decay_bps: Decimal = Decimal("35")
    slight_trend_inventory_bias_fraction: Decimal = Decimal("0.20")
    funding_bias_cap_fraction: Decimal = Decimal("0.08")
    funding_reference_rate: Decimal = Decimal("0.0001")
    target_inventory_cap_fraction: Decimal = Decimal("0.30")
    inventory_skew_strength: Decimal = Decimal("1.0")

    def validate(self) -> "RuntimeConfig":
        if not self.symbol or not self.symbol.replace("_", "").isalnum():
            raise ValueError("symbol must be a non-empty exchange symbol")
        if not self.db_path:
            raise ValueError("db_path must be non-empty")
        if self.adaptive_interval not in _KLINE_INTERVALS:
            raise ValueError("adaptive_interval is unsupported")

        decimals = {
            "position_limit": self.position_limit,
            "max_inventory": self.max_inventory,
            "base_size": self.base_size,
            "grid_spacing": self.grid_spacing,
            "cycle_interval_seconds": self.cycle_interval_seconds,
            "base_spacing_bps": self.base_spacing_bps,
            "min_spacing_bps": self.min_spacing_bps,
            "max_spacing_bps": self.max_spacing_bps,
            "volatility_spacing_multiplier": self.volatility_spacing_multiplier,
            "min_size_scale": self.min_size_scale,
            "size_decay_bps": self.size_decay_bps,
            "slight_trend_inventory_bias_fraction": self.slight_trend_inventory_bias_fraction,
            "funding_bias_cap_fraction": self.funding_bias_cap_fraction,
            "funding_reference_rate": self.funding_reference_rate,
            "target_inventory_cap_fraction": self.target_inventory_cap_fraction,
            "inventory_skew_strength": self.inventory_skew_strength,
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
        if self.regime_lookback < 3 or self.regime_lookback > 500:
            raise ValueError("regime_lookback must be in [3, 500]")

        if self.min_spacing_bps <= 0 or self.max_spacing_bps <= 0:
            raise ValueError("adaptive spacing bounds must be positive")
        if not (self.min_spacing_bps <= self.base_spacing_bps <= self.max_spacing_bps):
            raise ValueError("base_spacing_bps must be within spacing bounds")
        if self.volatility_spacing_multiplier < 0:
            raise ValueError("volatility_spacing_multiplier cannot be negative")
        if not (Decimal("0") < self.min_size_scale <= Decimal("1")):
            raise ValueError("min_size_scale must be in (0, 1]")
        if self.size_decay_bps <= 0:
            raise ValueError("size_decay_bps must be positive")
        for name, value in (
            ("slight_trend_inventory_bias_fraction", self.slight_trend_inventory_bias_fraction),
            ("funding_bias_cap_fraction", self.funding_bias_cap_fraction),
            ("target_inventory_cap_fraction", self.target_inventory_cap_fraction),
        ):
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.funding_reference_rate <= 0:
            raise ValueError("funding_reference_rate must be positive")
        if self.inventory_skew_strength < 0:
            raise ValueError("inventory_skew_strength cannot be negative")
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
            adaptive_interval=os.environ.get("APGE_ADAPTIVE_INTERVAL", cls.adaptive_interval),
            regime_lookback=_env_int("APGE_REGIME_LOOKBACK", cls.regime_lookback),
            base_spacing_bps=_env_decimal("APGE_BASE_SPACING_BPS", cls.base_spacing_bps),
            min_spacing_bps=_env_decimal("APGE_MIN_SPACING_BPS", cls.min_spacing_bps),
            max_spacing_bps=_env_decimal("APGE_MAX_SPACING_BPS", cls.max_spacing_bps),
            volatility_spacing_multiplier=_env_decimal(
                "APGE_VOLATILITY_SPACING_MULTIPLIER", cls.volatility_spacing_multiplier
            ),
            min_size_scale=_env_decimal("APGE_MIN_SIZE_SCALE", cls.min_size_scale),
            size_decay_bps=_env_decimal("APGE_SIZE_DECAY_BPS", cls.size_decay_bps),
            slight_trend_inventory_bias_fraction=_env_decimal(
                "APGE_SLIGHT_TREND_INVENTORY_BIAS_FRACTION",
                cls.slight_trend_inventory_bias_fraction,
            ),
            funding_bias_cap_fraction=_env_decimal(
                "APGE_FUNDING_BIAS_CAP_FRACTION", cls.funding_bias_cap_fraction
            ),
            funding_reference_rate=_env_decimal(
                "APGE_FUNDING_REFERENCE_RATE", cls.funding_reference_rate
            ),
            target_inventory_cap_fraction=_env_decimal(
                "APGE_TARGET_INVENTORY_CAP_FRACTION", cls.target_inventory_cap_fraction
            ),
            inventory_skew_strength=_env_decimal(
                "APGE_INVENTORY_SKEW_STRENGTH", cls.inventory_skew_strength
            ),
        )
        return config.validate()
